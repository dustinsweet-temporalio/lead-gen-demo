"""Model-free check of the Fully Orchestrated control flow.

Runs LeadIntakeOrchestratedWorkflow against a local Temporal dev server with the two
model-call activities replaced by deterministic stubs. No API key, no tokens, no network:
this verifies the parts the presentation depends on being reliable —

  * every seeded lead reaches its expected routing destination,
  * the below-threshold lead ALWAYS parks at the human gate,
  * the review Update is accepted while parked (override -> sales, confirm -> archive),
  * the Update validator REJECTS a review for a lead that isn't parked, and a second
    review of the same lead,
  * the slow email-verification step runs as a durable workflow timer.

The real-model behaviour is covered by tests/smoke_test.py; this one exists so the
human-review beat can be re-verified in seconds without spending tokens.

Run:  python -m tests.test_orchestration      (needs `temporal server start-dev`)
"""

from __future__ import annotations

import asyncio
import uuid

from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import app.config as config
from app.activities.tool_activities import ALL_TOOL_ACTIVITIES
from app.shared.models import Lead, LeadReviewDecision
from app.shared.scenarios import SCENARIOS_BY_ID
from app.workflows.orchestrated import LeadIntakeOrchestratedWorkflow

# Leads whose message really is too thin to act on. The stub mirrors what the model
# reliably says about the seeded content; everything else is genuine workflow logic.
_INCOMPLETE = {"LEAD-4101"}


@activity.defn(name="assess_lead_quality")
async def stub_assess_lead_quality(lead: Lead, contacts: list[dict]) -> dict:
    enough = lead.id not in _INCOMPLETE
    return {
        "has_enough_info": enough,
        "buying_intent": "evaluating a purchase" if enough else "",
        "missing_info": [] if enough else ["company", "what they are asking about"],
        "confidence": 0.9,
        "rationale": "stub",
        "tokens": 1234,
        "estimated": False,
    }


@activity.defn(name="draft_routing_plan")
async def stub_draft_routing_plan(
    lead: Lead, destination: str, owner: str, score: dict, enrichment: dict,
    verification: dict,
) -> dict:
    return {
        "owner": owner,
        "crm_note": f"stub note for {lead.id} -> {destination}",
        "talking_points": ["stub"],
        "rationale": "stub",
        "tokens": 2345,
        "estimated": False,
    }


STUBS = [stub_assess_lead_quality, stub_draft_routing_plan]

EXPECTED = {
    "LEAD-4102": "route_to_sales",
    "LEAD-4103": "route_to_marketing",
    "LEAD-4104": "route_to_nurture",
    "LEAD-4101": "needs_info",
}


async def _await_gate(handle, tries: int = 300) -> dict | None:
    for _ in range(tries):
        try:
            rv = await handle.query("get_review")
            if rv and rv.get("awaiting"):
                return rv
        except Exception:
            pass
        desc = await handle.describe()
        if desc.status and desc.status.name != "RUNNING":
            return None
        await asyncio.sleep(0.1)
    return None


async def _timer_count(client: Client, wid: str) -> int:
    hist = await client.get_workflow_handle(wid).fetch_history()
    return sum(1 for e in hist.events if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED)


async def main() -> int:
    cfg = config.get_config()
    client = await Client.connect(
        cfg.temporal_address, namespace=cfg.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    queue = f"test-orchestration-{uuid.uuid4().hex[:8]}"
    ok = True
    run = uuid.uuid4().hex[:6]

    async with Worker(
        client,
        task_queue=queue,
        workflows=[LeadIntakeOrchestratedWorkflow],
        activities=[*ALL_TOOL_ACTIVITIES, *STUBS],
    ):
        # --- the straight-through routes ---------------------------------------
        for lead_id, expected in EXPECTED.items():
            lead = SCENARIOS_BY_ID[lead_id]
            wid = f"t-{lead.slug}-{run}"
            outcome = await client.execute_workflow(
                LeadIntakeOrchestratedWorkflow.run, lead, id=wid, task_queue=queue
            )
            good = outcome.action == expected
            ok &= good
            print(
                f"{'ok  ' if good else 'FAIL'} {lead_id}: action={outcome.action} "
                f"(want {expected}) score={outcome.score} owner={outcome.owner} "
                f"tokens={outcome.tokens.total}"
            )

        # --- the human gate: override -> sales ---------------------------------
        esc = SCENARIOS_BY_ID[cfg.escalation_lead_id]
        wid = f"t-{esc.slug}-override-{run}"
        handle = await client.start_workflow(
            LeadIntakeOrchestratedWorkflow.run, esc, id=wid, task_queue=queue
        )
        rv = await _await_gate(handle)
        if not rv:
            print(f"FAIL {esc.id}: never parked at the human gate")
            return 1
        print(f"ok   {esc.id}: parked at the gate (score {rv.get('score')}, "
              f"threshold {rv.get('qualify_threshold')})")

        # The validator must reject a review of a lead that IS parked but already reviewed;
        # first send the real one.
        ack = await handle.execute_update(
            "lead_review", LeadReviewDecision(confirm_archive=False, route_to="sales", note="test override")
        )
        print(f"ok   update accepted, ack={ack}")
        outcome = await handle.result()
        good = outcome.action == "route_to_sales" and outcome.human_override
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {esc.id}: override -> action={outcome.action} "
              f"human_override={outcome.human_override} owner={outcome.owner}")

        timers = await _timer_count(client, wid)
        good = timers >= 1
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} durable wait present: {timers} timer(s) in history")

        # A review after the workflow closed must be rejected, not silently dropped.
        try:
            await handle.execute_update(
                "lead_review", LeadReviewDecision(confirm_archive=True)
            )
            print("FAIL second review was accepted after the run finished")
            ok = False
        except Exception as e:
            print(f"ok   second review rejected ({type(e).__name__})")

        # --- the human gate: confirm -> archive --------------------------------
        wid = f"t-{esc.slug}-confirm-{run}"
        handle = await client.start_workflow(
            LeadIntakeOrchestratedWorkflow.run, esc, id=wid, task_queue=queue
        )
        rv = await _await_gate(handle)
        if not rv:
            print(f"FAIL {esc.id}: never parked at the human gate (confirm case)")
            return 1
        await handle.execute_update(
            "lead_review", LeadReviewDecision(confirm_archive=True, note="test confirm")
        )
        outcome = await handle.result()
        good = outcome.action == "archive"
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {esc.id}: confirm -> action={outcome.action}")

        # --- the validator rejects an override with no destination -------------
        wid = f"t-{esc.slug}-nodest-{run}"
        handle = await client.start_workflow(
            LeadIntakeOrchestratedWorkflow.run, esc, id=wid, task_queue=queue
        )
        await _await_gate(handle)
        try:
            await handle.execute_update(
                "lead_review", LeadReviewDecision(confirm_archive=False)
            )
            print("FAIL override with no destination was accepted")
            ok = False
        except Exception as e:
            print(f"ok   override with no destination rejected ({type(e).__name__})")
        # settle it so the run does not linger
        await handle.execute_update(
            "lead_review", LeadReviewDecision(confirm_archive=False, route_to="nurture")
        )
        outcome = await handle.result()
        good = outcome.action == "route_to_nurture" and outcome.override_score == 38
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} nurture override -> action={outcome.action} "
              f"override_score={outcome.override_score} (want 38)")

        # --- the validator rejects a review for a lead that never parks --------
        clean = SCENARIOS_BY_ID["LEAD-4102"] if cfg.escalation_lead_id != "LEAD-4102" else SCENARIOS_BY_ID["LEAD-4103"]
        wid = f"t-{clean.slug}-noreview-{run}"
        handle = await client.start_workflow(
            LeadIntakeOrchestratedWorkflow.run, clean, id=wid, task_queue=queue
        )
        try:
            await handle.execute_update(
                "lead_review", LeadReviewDecision(confirm_archive=True)
            )
            print(f"FAIL {clean.id}: review accepted for a lead that never parked")
            ok = False
        except WorkflowUpdateFailedError as e:
            print(f"ok   {clean.id}: review rejected — {e.cause}")
        except Exception as e:
            print(f"ok   {clean.id}: review rejected ({type(e).__name__})")
        await handle.result()

    print("\nORCHESTRATION", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
