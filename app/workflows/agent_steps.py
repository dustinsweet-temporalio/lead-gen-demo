"""LeadIntakeAgentStepsWorkflow — the SAME agent as LeadIntakeAgentWorkflow, but each
model call and each tool call is its own Temporal Activity (the ``OpenAIAgentsPlugin`` +
``activity_as_tool``).

The agent still free-runs its own tool-calling loop; the workflow does not branch. What
changes is visibility and recovery granularity: every enrichment call, verification call
and CRM write is an individually named activity with its inputs and outputs in the event
history, and a mid-run crash resumes from the last completed activity — the work already
done is not repeated.

Token banking: a ``RunHooks.on_llm_end`` hook fires in the workflow after each model
activity completes and accumulates usage into durable workflow state, so the meter climbs
per step and a crash wastes ~one step's tokens.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib import openai_agents
    from agents import RunHooks, Runner

    import app.config as config
    from app.activities.tool_activities import TOOL_ACTIVITIES
    from app.shared.agent_defs import make_intake_agent, render_lead
    from app.shared.models import ACTIONS, Action, IntakeOutcome, Lead, TokenBuckets
    from app.shared.tokens import estimate_tokens, usage_tokens
    from app.shared import stages

_ACT_TIMEOUT = timedelta(seconds=60)
_ACT_RETRY = RetryPolicy(maximum_attempts=3)
_HEARTBEAT = timedelta(seconds=2.0)  # tight crash detection (~2s)


def _coerce_action(value: str) -> "Action":
    return value if value in ACTIONS else "needs_info"


@workflow.defn
class LeadIntakeAgentStepsWorkflow:
    def __init__(self) -> None:
        self._stage = stages.RUNNING
        self._tokens = TokenBuckets()
        self._outcome: IntakeOutcome | None = None

    class _BankHooks(RunHooks):
        """Accumulate usage into workflow state after each completed model activity."""

        def __init__(self, wf: "LeadIntakeAgentStepsWorkflow") -> None:
            self._wf = wf

        async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
            u = getattr(response, "usage", None)
            n = (getattr(u, "total_tokens", 0) or 0) if u else 0
            if not n and u:
                n = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
            if n:
                self._wf._tokens.new += n

    @workflow.run
    async def run(self, lead: Lead) -> IntakeOutcome:
        self._stage = stages.RUNNING

        # Each tool the agent chooses to call runs as its own durable Temporal activity —
        # named for the business step, with its inputs and outputs in the history.
        def tool(fn):
            # The email-verification provider is slow, so give that one a longer ceiling.
            timeout = (
                timedelta(seconds=max(60, config.get_config().email_verify_delay_seconds + 30))
                if getattr(fn, "__name__", "") == "verify_email"
                else _ACT_TIMEOUT
            )
            return openai_agents.workflow.activity_as_tool(
                fn, start_to_close_timeout=timeout, heartbeat_timeout=_HEARTBEAT, retry_policy=_ACT_RETRY
            )

        tools = [tool(fn) for fn in TOOL_ACTIVITIES]
        # Model as a string — the plugin resolves it to a model activity. SAME agent/prompt
        # as Agent as Workflow (only the tool/model wrapping differs).
        agent = make_intake_agent(config.get_config().model, tools)
        prompt = render_lead(lead)

        result = await Runner.run(agent, input=prompt, hooks=self._BankHooks(self), max_turns=30)

        # Reconcile against the authoritative end-of-run usage; never read zero.
        final = usage_tokens(result)
        if final and final > self._tokens.new:
            self._tokens.new = final
        if self._tokens.new == 0:
            self._tokens.new = estimate_tokens(prompt, str(result.final_output)) or 1
            self._tokens.estimated = True

        r = result.final_output
        self._outcome = IntakeOutcome(
            lead_id=lead.id,
            action=_coerce_action(r.action),
            summary=r.summary,
            score=r.score,
            owner=r.owner,
            matched_contact=r.matched_contact,
            branch=r.branch_taken,
            detail=r.detail,
            tokens=self._tokens,
            stage=stages.DONE,
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
    def get_branch(self) -> str:
        return self._outcome.branch if self._outcome else ""
