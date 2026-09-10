"""End-to-end smoke test — the gate for "done".

Verifies against a RUNNING Temporal dev server (this test starts its own worker in-proc
per variant, so you do NOT need the web-server supervisor running):

  1. Tracing is disabled (no tracing-export error on agent construction).
  2. Each seeded lead reaches its expected routing action.
  3. Token counts are non-zero (or the estimator engaged and flagged `estimated`).
  4. The retry-demo lead shows >=2 attempts on the injected step and still succeeds.
  5. Fully Orchestrated parks at the human lead-review gate; the test drives the Update,
     overriding the score for the escalation lead (the path used live on stage).

Requires OPENAI_API_KEY (real model). Run:
    python -m tests.smoke_test [agent|agent_steps|orchestrated]

This is a real-model test and will spend tokens.
"""

from __future__ import annotations

import asyncio
import sys

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import app.config as config
from app.shared.models import IntakeOutcome, LeadReviewDecision
from app.shared.scenarios import SCENARIOS_BY_ID
from app.worker.worker_main import ACTIVITIES, WORKFLOWS, _plugin

# Expected routing action per seeded lead. The escalation lead (default LEAD-4105) parks
# for human review; this test overrides, so it lands on sales.
EXPECTED = {
    "LEAD-4102": "route_to_sales",
    "LEAD-4103": "route_to_marketing",
    "LEAD-4104": "route_to_nurture",
    "LEAD-4105": "route_to_sales",   # below threshold -> reviewed -> overridden
    "LEAD-4101": "needs_info",
    "LEAD-4106": "route_to_sales",   # with the injected retryable failure
}

# Only Fully Orchestrated has the human gate. In the agent-driven variants nobody reviews
# the escalation lead, so the agent archives it — that contrast is the point of variant 4.
EXPECTED_AGENT_DRIVEN = dict(EXPECTED, **{"LEAD-4105": "archive"})


async def _client(variant: str) -> Client:
    cfg = config.get_config()
    if variant == config.AGENT_STEPS:
        return await Client.connect(
            cfg.temporal_address, namespace=cfg.temporal_namespace, plugins=[_plugin()]
        )
    return await Client.connect(
        cfg.temporal_address, namespace=cfg.temporal_namespace, data_converter=pydantic_data_converter
    )


async def _drive_review(client: Client, wid: str) -> str | None:
    """Fully Orchestrated parks at the lead-review gate for a below-threshold lead.

    Send the human decision as an Update, overriding the score — the path the presenter
    takes live. Returns a note about what happened, or None if the gate never fired.
    """
    handle = client.get_workflow_handle(wid)
    for _ in range(600):  # ~2 min
        try:
            desc = await handle.describe()
            if desc.status and desc.status.name != "RUNNING":
                return None
        except Exception:
            pass
        try:
            review = await handle.query("get_review")
        except Exception:
            review = None
        if review and review.get("awaiting"):
            ack = await handle.execute_update(
                "lead_review",
                LeadReviewDecision(
                    confirm_archive=False, route_to="sales", note="smoke test: override"
                ),
            )
            return f"review gate fired at score {review.get('score')}; update ack={ack}"
        await asyncio.sleep(0.2)
    return None


async def run_variant(variant: str) -> bool:
    config.disable_agent_tracing()  # check 1: must not raise
    config.require_api_key()
    cfg = config.get_config()
    client = await _client(variant)
    wf_type = config.WORKFLOW_TYPE[variant]
    task_queue = cfg.task_queue(variant)
    expected_all = EXPECTED if variant == config.ORCHESTRATED else EXPECTED_AGENT_DRIVEN

    ok = True
    async with Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=ACTIVITIES):
        for lead_id, expected in expected_all.items():
            lead = SCENARIOS_BY_ID[lead_id]
            wid = f"smoke-{lead.slug}-{config.variant_suffix(variant)}"
            # result_type so the outcome decodes into IntakeOutcome rather than a dict
            # (starting by workflow-type STRING gives the converter nothing to target).
            handle = await client.start_workflow(
                wf_type, lead, id=wid, task_queue=task_queue, result_type=IntakeOutcome
            )

            review_task = None
            if variant == config.ORCHESTRATED:
                review_task = asyncio.create_task(_drive_review(client, wid))

            outcome = await handle.result()
            if review_task:
                note = await review_task
                if note:
                    print(f"  {note}")

            tokens = outcome.tokens
            nonzero = tokens.total > 0
            branch_ok = outcome.action == expected
            print(
                f"[{variant}] {lead_id}: action={outcome.action} (want {expected}) "
                f"score={outcome.score} owner={outcome.owner} "
                f"tokens={tokens.total}{' ~est' if tokens.estimated else ''} branch={outcome.branch}"
                + (" OVERRIDDEN" if outcome.human_override else "")
            )
            if not branch_ok:
                print(f"  FAIL: expected {expected}, got {outcome.action}")
                ok = False
            if not nonzero:
                print("  FAIL: token total is zero")
                ok = False

            if lead_id == "LEAD-4106":
                attempts = await _max_activity_attempts(client, wid)
                print(f"  LEAD-4106 activity attempts on the injected step = {attempts}")
                if attempts < 2:
                    print("  FAIL: expected >=2 attempts on the injected step")
                    ok = False
    return ok


async def _max_activity_attempts(client: Client, workflow_id: str) -> int:
    """Highest attempt number any activity reached in this run.

    Read off ActivityTaskStarted, NOT ActivityTaskFailed: when Temporal retries an
    activity, no failure event is written to history until the retries are exhausted. The
    retry is visible only as a later ActivityTaskStarted with attempt > 1.
    """
    from temporalio.api.enums.v1 import EventType

    hist = await client.get_workflow_handle(workflow_id).fetch_history()
    return max(
        (
            e.activity_task_started_event_attributes.attempt
            for e in hist.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        ),
        default=0,
    )


async def main() -> int:
    variant = sys.argv[1] if len(sys.argv) > 1 else config.AGENT_STEPS
    if variant not in config.WORKER_VARIANTS:
        print(f"variant must be one of {config.WORKER_VARIANTS}")
        return 2
    ok = await run_variant(variant)
    print("\nSMOKE", "PASS" if ok else "FAIL", f"({variant})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
