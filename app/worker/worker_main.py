"""The killable worker process.

Launched (and SIGKILLed / relaunched) by the web server's supervisor. Takes
``--variant {agent,agent_steps,orchestrated}`` and configures the Temporal client
accordingly:

* **agent_steps** connects with the ``OpenAIAgentsPlugin``. The plugin auto-registers the
  model-invocation activity and installs its pydantic-compatible payload converter.
  (Defaults to OpenAI — no model_provider needed.)
* **agent / orchestrated** connect WITHOUT the plugin (plain client + the pydantic data
  converter).

Each variant polls its **own task queue** (``lead-intake-agent``,
``lead-intake-agent-steps``, ``lead-intake-orchestrated``), so filtering the Temporal UI
by task queue during the presentation shows exactly one variant's runs.

The worker does NOT self-restart — that is the operator's job (CLAUDE.md rule 11). The
Agent Only variant has no worker; it runs in the web-server process.

Run manually:  python -m app.worker.worker_main --variant agent_steps
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import app.config as config
from app.activities.intake_agent import run_lead_intake_agent
from app.activities.reasoning import REASONING_ACTIVITIES
from app.activities.tool_activities import ALL_TOOL_ACTIVITIES
from app.workflows.agent import LeadIntakeAgentWorkflow
from app.workflows.agent_steps import LeadIntakeAgentStepsWorkflow
from app.workflows.orchestrated import LeadIntakeOrchestratedWorkflow

VARIANTS = config.WORKER_VARIANTS

WORKFLOWS = [
    LeadIntakeAgentWorkflow,
    LeadIntakeAgentStepsWorkflow,
    LeadIntakeOrchestratedWorkflow,
]
# The agent activity (Agent as Workflow) + the shared tool activities (Agent Steps /
# Fully Orchestrated) + the Fully-Orchestrated model-call activities.
ACTIVITIES = [
    run_lead_intake_agent,
    *ALL_TOOL_ACTIVITIES,
    *REASONING_ACTIVITIES,
]


def _plugin() -> OpenAIAgentsPlugin:
    return OpenAIAgentsPlugin(
        # Defaults to OpenAI (reads OPENAI_API_KEY from env) — no model_provider needed.
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=60),
            # The model activity is auto-heartbeated by the integration (~every 0.8s, see
            # the throttle below), so a 2.0s heartbeat timeout makes a worker crash
            # mid-inference detectable in ~2s — without false timeouts on long model calls.
            heartbeat_timeout=timedelta(seconds=2.0),
            retry_policy=RetryPolicy(maximum_attempts=3),
        ),
    )


async def run_worker(variant: str) -> None:
    config.disable_agent_tracing()
    config.require_api_key()  # fail fast with a clear message if the key is missing
    cfg = config.get_config()

    # Empty against a local dev server; carries the API key + TLS when the demo is
    # deployed against Temporal Cloud. Same options the web server's client uses.
    options = cfg.temporal_connect_options

    if variant == config.AGENT_STEPS:
        # Only this variant runs the OpenAI Agents SDK in the workflow (the plugin turns
        # model calls into activities). Agent as Workflow uses the SDK inside its one
        # activity; Fully Orchestrated uses no SDK — both use the plain pydantic converter.
        client = await Client.connect(
            cfg.temporal_address,
            namespace=cfg.temporal_namespace,
            plugins=[_plugin()],
            **options,
        )
    else:
        client = await Client.connect(
            cfg.temporal_address,
            namespace=cfg.temporal_namespace,
            data_converter=pydantic_data_converter,
            **options,
        )

    task_queue = cfg.task_queue(variant)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        # Deliver heartbeats ~every 0.8s instead of the default 0.8x-timeout throttle, so
        # rule 6's tight 2.5s heartbeat timeout still gives reliable dead-worker detection.
        max_heartbeat_throttle_interval=timedelta(seconds=0.8),
        default_heartbeat_throttle_interval=timedelta(seconds=0.8),
    )
    print(f"[worker] started variant={variant} task_queue={task_queue}", flush=True)
    await worker.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lead-intake worker")
    parser.add_argument("--variant", choices=list(VARIANTS), default=config.AGENT)
    args = parser.parse_args()
    asyncio.run(run_worker(args.variant))


if __name__ == "__main__":
    main()
