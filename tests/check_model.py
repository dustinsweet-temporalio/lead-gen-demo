"""Earliest real-model smoke point.

Runs the shared lead-intake agent over a seeded lead directly (plain Agents SDK, no
Temporal) and asserts: tracing is off, the model string + key work, the agent returns a
structured ``LeadIntakeAgentResult`` with a valid action, and token usage is non-zero (or
the estimator engaged). Verifies the whole real-model path in one shot.

Run:  python -m tests.check_model [LEAD-4102]
Requires:  OPENAI_API_KEY in .env
"""

from __future__ import annotations

import asyncio
import sys

from agents import OpenAIChatCompletionsModel, Runner
from openai import AsyncOpenAI

import app.config as config
from app.shared.agent_defs import make_intake_agent, render_lead
from app.shared.models import ACTIONS
from app.shared.scenarios import get_scenario
from app.shared.tokens import usage_tokens


async def main() -> int:
    config.disable_agent_tracing()
    key = config.require_api_key()
    lead_id = sys.argv[1] if len(sys.argv) > 1 else "LEAD-4102"
    lead = get_scenario(lead_id)
    if lead is None:
        print(f"unknown lead {lead_id}")
        return 1

    model = OpenAIChatCompletionsModel(
        model=config.get_config().model, openai_client=AsyncOpenAI(api_key=key, max_retries=0)
    )
    agent = make_intake_agent(model)  # default in-process function tools
    result = await Runner.run(agent, input=render_lead(lead), max_turns=30)

    r = result.final_output
    tokens = usage_tokens(result) or 0
    print(f"action={r.action} branch={r.branch_taken} score={r.score} owner={r.owner} tokens={tokens}")
    print(f"summary: {r.summary}")

    ok = r.action in ACTIONS and tokens > 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
