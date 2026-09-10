"""Agent as Workflow — the entire agent run inside a single Temporal Activity.

The plain Agents SDK drives the whole multi-turn tool-calling loop here; Temporal sees one
opaque activity. A crash mid-run forces this activity to re-run from the beginning,
re-spending every token already spent — that is the lesson, not a bug (CLAUDE.md rule 5).

Token accounting: the activity heartbeats its running total continuously from a background
timer (rule 6) so the UI meter climbs live. On retry, the fresh attempt reads the previous
(killed) attempt's last heartbeat from ``activity.info()`` and carries that figure forward
as **Wasted**; the new attempt's tokens accumulate toward **New**.
"""

from __future__ import annotations

import asyncio

from agents import OpenAIChatCompletionsModel, RunHooks, Runner
from openai import AsyncOpenAI
from temporalio import activity
from temporalio.exceptions import ApplicationError

import app.config as config
from app.shared.agent_defs import make_intake_agent, render_lead
from app.shared.models import ACTIONS, Action, IntakeOutcome, Lead, TokenBuckets
from app.shared.tokens import estimate_tokens, usage_tokens

# --- Fault injection (the retry-demo lead) ----------------------------------------
# Module-level attempt counter keyed by (workflow_id, step). Fails the FIRST attempt with
# a RETRYABLE error so Temporal auto-recovers on attempt 2 (SCENARIOS.md fault contract).
# In-memory: resets if the worker is killed — so never combine the retry-demo lead with a
# worker kill in the same run.
_RETRY_DEMO_LEAD = "LEAD-4106"
_attempts: dict[tuple[str, str], int] = {}


def maybe_inject_failure(workflow_id: str, step: str, lead_id: str) -> None:
    if lead_id != _RETRY_DEMO_LEAD:
        return
    key = (workflow_id, step)
    _attempts[key] = _attempts.get(key, 0) + 1
    if _attempts[key] == 1:
        raise ApplicationError(
            f"enrichment provider returned 503 for {lead_id} (injected transient failure)",
            non_retryable=False,
        )


# --- Live per-call usage capture --------------------------------------------------


class _AttemptUsage:
    def __init__(self) -> None:
        self.tokens = 0
        self.estimated = False

    def add(self, n: int, *, estimated: bool = False) -> None:
        if n > 0:
            self.tokens += n
        if estimated:
            self.estimated = True


class _UsageHooks(RunHooks):
    """Accumulates token usage after each model call so the heartbeat climbs live."""

    def __init__(self, usage: _AttemptUsage) -> None:
        self._u = usage

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        u = getattr(response, "usage", None)
        n = (getattr(u, "total_tokens", 0) or 0) if u else 0
        if not n and u:
            n = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
        self._u.add(n)


# --- Heartbeating -----------------------------------------------------------------


async def _heartbeat_loop(prior_wasted: int, usage: _AttemptUsage) -> None:
    """Emit the cumulative running token total continuously (background timer).

    Details schema: {prior_wasted, attempt_tokens, estimated}. If this attempt is killed,
    the next attempt reads it and rolls (prior_wasted + attempt_tokens) into Wasted.
    """
    interval = config.get_config().heartbeat_interval_seconds
    while True:
        try:
            activity.heartbeat(
                {
                    "prior_wasted": prior_wasted,
                    "attempt_tokens": usage.tokens,
                    "estimated": usage.estimated,
                }
            )
        except Exception:
            # Never let a transient heartbeat error kill the background task — that would
            # cause a false heartbeat timeout while the agent is still working.
            pass
        await asyncio.sleep(interval)


def _carry_prior_wasted() -> tuple[int, bool]:
    """Read the previous (killed) attempt's last heartbeat and total it into Wasted."""
    details = activity.info().heartbeat_details
    if not details:
        return 0, False
    d = details[0] if isinstance(details, (list, tuple)) else details
    if not isinstance(d, dict):
        return 0, False
    wasted = int(d.get("prior_wasted", 0)) + int(d.get("attempt_tokens", 0))
    return wasted, bool(d.get("estimated", False))


# --- The activity -----------------------------------------------------------------


def _coerce_action(value: str) -> Action:
    return value if value in ACTIONS else "needs_info"


@activity.defn
async def run_lead_intake_agent(lead: Lead) -> IntakeOutcome:
    """The whole agent, as one activity. One opaque box in the Temporal UI — which is
    exactly what this variant exists to show."""
    config.disable_agent_tracing()
    key = config.require_api_key()
    cfg = config.get_config()

    prior_wasted, prior_estimated = _carry_prior_wasted()
    usage = _AttemptUsage()
    usage.estimated = prior_estimated

    # Emit one heartbeat immediately (before any heavy/blocking setup) so a slow prelude
    # can't trip the timeout before the background loop's first tick.
    activity.heartbeat({"prior_wasted": prior_wasted, "attempt_tokens": 0, "estimated": prior_estimated})
    hb = asyncio.create_task(_heartbeat_loop(prior_wasted, usage))
    try:
        # The whole loop runs in-process inside this one activity, with no plugin. The model
        # is a concrete OpenAIChatCompletionsModel over gpt-5.5; max_retries=0 so Temporal
        # owns retries. Tools are in-process function tools. include_usage is set on the agent.
        model = OpenAIChatCompletionsModel(
            model=cfg.model, openai_client=AsyncOpenAI(api_key=key, max_retries=0)
        )
        prompt = render_lead(lead)

        agent = make_intake_agent(model)
        result = await Runner.run(
            agent, input=prompt, hooks=_UsageHooks(usage), max_turns=30
        )

        # Reconcile against the authoritative end-of-run usage; never let it read zero.
        final = usage_tokens(result)
        if final and final > usage.tokens:
            usage.tokens = final
        if usage.tokens == 0:
            usage.add(estimate_tokens(prompt, str(result.final_output)) or 1, estimated=True)

        # Deliberate fault injection AFTER tokens are spent, so the auto-retry re-runs the
        # whole agent and the Wasted figure jumps — the cost lesson.
        maybe_inject_failure(activity.info().workflow_id, "lead_intake_agent", lead.id)

        r = result.final_output
        return IntakeOutcome(
            lead_id=lead.id,
            action=_coerce_action(r.action),
            summary=r.summary,
            score=r.score,
            owner=r.owner,
            matched_contact=r.matched_contact,
            branch=r.branch_taken,
            detail=r.detail,
            tokens=TokenBuckets(new=usage.tokens, wasted=prior_wasted, estimated=usage.estimated),
            stage="done",
        )
    finally:
        hb.cancel()
