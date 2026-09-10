"""LeadIntakeAgentWorkflow — the whole agent as ONE activity.

Temporal's view is a single ``run_lead_intake_agent`` activity that wraps the entire agent
loop: one lead, one workflow, one opaque step. The token meter climbs live off that
activity's heartbeats (read by the web server from the pending-activity record); the
workflow-state buckets are the committed figures, exposed via Query.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import app.config as config
    from app.activities.intake_agent import run_lead_intake_agent
    from app.shared import stages
    from app.shared.models import IntakeOutcome, Lead, TokenBuckets


@workflow.defn
class LeadIntakeAgentWorkflow:
    def __init__(self) -> None:
        self._stage = stages.RUNNING
        self._tokens = TokenBuckets()
        self._outcome: IntakeOutcome | None = None

    @workflow.run
    async def run(self, lead: Lead) -> IntakeOutcome:
        # The one big activity runs the whole agent loop opaquely. We surface a generic
        # "running" stage; the flowchart caption explains "one Activity wraps the agent".
        self._stage = stages.RUNNING

        # DELIBERATE TEACHING CHOICE (CLAUDE.md rule 5): this activity RETRIES FROM SCRATCH
        # and is NOT idempotent. A mid-run crash re-runs the entire agent from the top,
        # re-spending every token already spent — which is exactly what makes the wasted-
        # token cost visible in the meter. DO NOT "optimize" this by checkpointing or by
        # setting maximum_attempts=1; that would delete the whole point of this variant.
        outcome: IntakeOutcome = await workflow.execute_activity(
            run_lead_intake_agent,
            lead,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=config.get_config().heartbeat_timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_attempts=3,
            ),
        )

        self._tokens = outcome.tokens
        self._outcome = outcome
        self._stage = stages.DONE
        return outcome

    # --- Queries (read by the web server even while the worker restarts) ----------

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
