"""LeadIntakeOrchestratedWorkflow — the Workflow owns the control flow, with no agent
framework at all, plus the human lead-review gate.

The workflow fetches the lead, checks the CRM, asks the model exactly one judgement
question (is there enough here to act on?), gets a deterministic score, and then applies
the routing thresholds **itself** (CLAUDE.md rule 4). The model never picks the branch.
Two consequences the demo depends on:

* The escalation is deterministic. A lead below the qualify threshold ALWAYS parks at the
  human gate — that beat gets one shot on stage.
* The history is short and every step is a named business step.

Two Temporal mechanics carry the two big beats:

* **Human review = a Temporal Update** (``lead_review``), not a signal. The update's
  validator rejects a review for a lead that isn't parked, so the operator gets real
  synchronous feedback in the UI. The workflow parks on ``wait_condition`` with a long
  timeout — a visible timer in the event history, and free to sit in for a week.
* **The slow email-verification provider is a durable wait**: submit the request, park on
  a workflow timer, then collect the result. No polling loop, no second pipeline, and a
  worker crash mid-wait does not restart the wait.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import app.config as config
    from app.activities.reasoning import assess_lead_quality, draft_routing_plan
    from app.activities.tool_activities import (
        archive_lead,
        check_duplicate_contact,
        enrich_contact,
        fetch_email_verification,
        fetch_lead_details,
        notify_slack,
        route_to_marketing,
        route_to_nurture,
        route_to_sales,
        score_lead,
        submit_email_verification,
        update_crm,
    )
    from app.shared.models import IntakeOutcome, Lead, LeadReviewDecision, TokenBuckets
    from app.shared import stages

_CHANNEL = "#lead-routing"
_ACT_TIMEOUT = timedelta(seconds=60)
_LLM_TIMEOUT = timedelta(minutes=2)
_ACT_RETRY = RetryPolicy(maximum_attempts=3)
_HEARTBEAT = timedelta(seconds=2.0)


@workflow.defn
class LeadIntakeOrchestratedWorkflow:
    def __init__(self) -> None:
        self._stage = stages.INTAKE
        self._tokens = TokenBuckets()
        self._outcome: IntakeOutcome | None = None
        self._bypassed: list[str] = []
        # Human review state.
        self._awaiting_review = False
        self._decision: Optional[LeadReviewDecision] = None
        self._review_context: dict = {}

    # --- Human review: a Temporal UPDATE (with a validator) -----------------------

    @workflow.update
    async def lead_review(self, decision: LeadReviewDecision) -> dict:
        """Deliver the human's review decision.

        Returns an acknowledgement to the caller, so the UI can confirm the decision was
        accepted rather than hoping it landed.
        """
        self._decision = decision
        tier = "" if decision.confirm_archive else (decision.route_to or "sales")
        # The score the gauge should read once a person has overruled the model. Returned
        # synchronously in the ack so the UI can stop showing the rejected number the
        # instant the reviewer clicks, without waiting for the run to finish.
        effective = config.override_score_for(tier) if tier else None
        if effective is not None:
            self._review_context["override_score"] = effective
        return {
            "accepted": True,
            "lead_id": self._review_context.get("lead_id", ""),
            "outcome": "archive" if decision.confirm_archive else f"route_to_{tier}",
            "route_to": tier or None,
            "effective_score": effective,
        }

    @lead_review.validator
    def _validate_lead_review(self, decision: LeadReviewDecision) -> None:
        # Rejected synchronously — the operator sees why, and the workflow state is
        # untouched. This is the practical reason to prefer an Update over a Signal here.
        if self._decision is not None:
            raise ValueError("this lead has already been reviewed")
        if not self._awaiting_review:
            raise ValueError("this lead is not awaiting human review")
        if not decision.confirm_archive and decision.route_to not in ("sales", "marketing", "nurture"):
            raise ValueError(
                "an override must name a destination: sales, marketing or nurture"
            )

    @workflow.query
    def get_review(self) -> dict:
        """What the review modal needs: whether we're parked, and the lead's numbers."""
        return {"awaiting": self._awaiting_review, **self._review_context}

    # --- helpers ------------------------------------------------------------------

    async def _act(self, fn, *args, timeout=_ACT_TIMEOUT):
        return await workflow.execute_activity(
            fn, args=list(args), start_to_close_timeout=timeout,
            heartbeat_timeout=_HEARTBEAT, retry_policy=_ACT_RETRY,
        )

    async def _llm(self, fn, *args):
        """Run a model activity; bank its tokens; return the dict."""
        r = await self._act(fn, *args, timeout=_LLM_TIMEOUT)
        self._tokens.new += int(r.get("tokens", 0))
        if r.get("estimated"):
            self._tokens.estimated = True
        return r

    async def _notify(self, text: str) -> None:
        await self._act(notify_slack, _CHANNEL, text)

    @workflow.run
    async def run(self, lead: Lead) -> IntakeOutcome:
        cfg = config.get_config()

        # 1. Intake: pull the record, check whether we already know this contact.
        self._stage = stages.INTAKE
        await self._act(fetch_lead_details, lead.id)
        contact_query = " ".join(x for x in (lead.from_name, lead.company, lead.from_email) if x)
        contacts = await self._act(check_duplicate_contact, contact_query or lead.subject)
        matched = contacts[0]["id"] if contacts else None

        # 2. Completeness — the one judgement call worth a model here.
        self._stage = stages.QUALIFY
        quality = await self._llm(assess_lead_quality, lead, contacts)
        if not quality.get("has_enough_info"):
            self._stage = stages.ROUTE
            missing = ", ".join(quality.get("missing_info") or []) or "contact and company details"
            note = f"Not enough information to act on. Missing: {missing}."
            await self._act(update_crm, lead.id, "Needs Info", None, note)
            await self._notify(f"[{lead.id}] needs info — {missing}.")
            return self._finish(
                lead, "needs_info", f"{lead.id} lacks the detail needed to route it.",
                branch="needs_info", detail=note, matched_contact=matched,
                bypass=[stages.SCORING, stages.REVIEW, stages.ENRICH, stages.VERIFY],
            )

        # 3. Score (deterministic service) — then the WORKFLOW applies the thresholds.
        self._stage = stages.SCORING
        score_data = await self._act(score_lead, lead.id)
        score = int(score_data.get("score", 0))
        tier = config.tier_for_score(score)
        human_override = False
        override_score: int | None = None

        # 4. Below the qualify threshold: park for a human. Cheap, durable, and it can sit
        #    here for a week — the timer below is what makes the wait visible in the UI.
        if tier == "archive":
            self._stage = stages.REVIEW
            self._review_context = {
                "lead_id": lead.id,
                "subject": lead.subject,
                "from_name": lead.from_name,
                "from_email": lead.from_email,
                "company": lead.company,
                "body": lead.body,
                "score": score,
                "signals": score_data.get("signals", []),
                "qualify_threshold": cfg.qualify_threshold,
                "buying_intent": quality.get("buying_intent", ""),
                "matched_contact": matched,
            }
            await self._notify(
                f"[{lead.id}] scored {score}, below the qualifying threshold of "
                f"{cfg.qualify_threshold} — holding for human review before archiving."
            )
            self._awaiting_review = True
            timed_out = False
            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None,
                    timeout=timedelta(seconds=cfg.review_timeout_seconds),
                )
            except asyncio.TimeoutError:
                timed_out = True
            self._awaiting_review = False

            if timed_out or (self._decision and self._decision.confirm_archive):
                reason = (
                    "no reviewer responded within the review window"
                    if timed_out
                    else (self._decision.note or "confirmed by human review")
                )
                self._stage = stages.ROUTE
                await self._act(archive_lead, lead.id, reason)
                await self._act(
                    update_crm, lead.id, "Archived", None,
                    f"Archived at score {score} (threshold {cfg.qualify_threshold}). {reason}.",
                )
                await self._notify(f"[{lead.id}] archived — not worth pursuing ({reason}).")
                return self._finish(
                    lead, "archive", f"{lead.id} archived at score {score}.",
                    branch="archive", score=score, detail=reason, matched_contact=matched,
                    bypass=[stages.ENRICH, stages.VERIFY],
                )

            # The human disagreed with the score. They also chose the destination, so
            # route there rather than assuming sales; everything downstream then runs
            # exactly as it would for a lead that scored into that band on its own.
            # No Slack post here on purpose — every branch ends in exactly ONE terminal
            # notification, and it names the override. Keeping this history short is what
            # makes "count the activities" land on stage.
            human_override = True
            tier = self._decision.route_to or "sales"
            override_score = config.override_score_for(tier)

        # 5. Enrich. We only pay the enrichment vendor for leads we are actually pursuing.
        self._stage = stages.ENRICH
        enrichment = await self._act(enrich_contact, lead.id)

        # 6. Verify the address with the slow provider — as a DURABLE WAIT. Submit the
        #    request, park on a workflow timer, then collect. The provider can take an hour;
        #    this workflow is then just a workflow that is slow in the middle. No polling
        #    loop, and a worker crash during the wait does not restart it.
        self._stage = stages.VERIFY
        submitted = await self._act(submit_email_verification, lead.id)
        await workflow.sleep(timedelta(seconds=float(submitted.get("eta_seconds", 0) or 0)))
        verification = await self._act(
            fetch_email_verification, lead.id, submitted.get("request_id", "")
        )

        # 7. Route: the destination is already decided, so the model only writes it up.
        self._stage = stages.ROUTE
        owner = config.owner_for(tier, enrichment.get("industry"))
        plan = await self._llm(
            draft_routing_plan, lead, tier, owner, score_data, enrichment, verification
        )
        note = plan.get("crm_note") or f"Routed to {tier} at score {score}."
        if not verification.get("deliverable", True):
            note = f"{note} (email verification: {verification.get('result')})"

        if tier == "sales":
            await self._act(route_to_sales, lead.id, owner, plan.get("rationale", ""))
            await self._act(update_crm, lead.id, "Routed to Sales", owner, note)
            await self._notify(
                f"[{lead.id}] routed to SALES — score {score}, owner {owner}"
                + (" (human override)" if human_override else "")
            )
            return self._finish(
                lead, "route_to_sales",
                f"{lead.id} routed to sales ({owner}) at score {score}.",
                branch="sales", score=score, owner=owner, detail=note,
                override_score=override_score,
                matched_contact=matched, human_override=human_override, bypass=[],
            )

        if tier == "marketing":
            await self._act(route_to_marketing, lead.id, owner, plan.get("rationale", ""))
            await self._act(update_crm, lead.id, "Marketing Qualified", owner, note)
            await self._notify(f"[{lead.id}] routed to MARKETING — score {score}, campaign {owner}")
            return self._finish(
                lead, "route_to_marketing",
                f"{lead.id} routed to marketing ({owner}) at score {score}.",
                branch="marketing", score=score, owner=owner, detail=note,
                override_score=override_score,
                matched_contact=matched, bypass=[stages.REVIEW],
            )

        await self._act(route_to_nurture, lead.id, owner, plan.get("rationale", ""))
        await self._act(update_crm, lead.id, "Nurture", owner, note)
        await self._notify(f"[{lead.id}] routed to NURTURE — score {score}, campaign {owner}")
        return self._finish(
            lead, "route_to_nurture",
            f"{lead.id} routed to nurture ({owner}) at score {score}.",
            branch="nurture", score=score, owner=owner, detail=note,
                override_score=override_score,
            matched_contact=matched, bypass=[stages.REVIEW],
        )

    def _finish(
        self, lead, action, summary, *, branch, bypass, score=None, owner=None,
        matched_contact=None, detail="", human_override=False, override_score=None,
    ) -> IntakeOutcome:
        self._bypassed = bypass
        self._outcome = IntakeOutcome(
            lead_id=lead.id, action=action, summary=summary, score=score, owner=owner,
            matched_contact=matched_contact, branch=branch, detail=detail,
            human_override=human_override, override_score=override_score,
            tokens=self._tokens, stage=stages.DONE,
        )
        self._stage = stages.DONE
        return self._outcome

    # --- Queries ------------------------------------------------------------------

    @workflow.query
    def get_stage(self) -> str:
        return self._stage

    @workflow.query
    def get_tokens(self) -> dict:
        return self._tokens.to_api()

    @workflow.query
    def get_outcome(self) -> dict | None:
        return self._outcome.to_api() if self._outcome else None

    @workflow.query
    def get_bypassed(self) -> list[str]:
        return list(self._bypassed)

    @workflow.query
    def get_branch(self) -> str:
        return self._outcome.branch if self._outcome else ""
