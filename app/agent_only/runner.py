"""Agent Only — the baseline variant that uses NO Temporal.

The plain Agents SDK drives the whole intake loop in an ordinary asyncio task inside the
web-server process. There is no workflow, no worker, no activity, no durable record: if
this process dies mid-run the work is lost entirely and nothing resumes. That is the
"before" the three Temporal variants improve on, and it is how a classifier running as a
plain stateless function behaves today.

Run state lives in an in-memory registry the `/api/*` endpoints read. Tokens accumulate
live (New only) so the meter climbs during a run; there is no Wasted bucket — a crash
discards the run, so "wasted" is reported as N/A (null).

`kill_run` is the stage equivalent of the process dying: the task is cancelled and the
record is DELETED, so there is nothing left to inspect or resume — which is the whole
point of this variant. (Ctrl-C on the web server is the fully faithful version; it also
takes the UI down with it.)
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

from agents import OpenAIChatCompletionsModel, RunHooks, Runner
from openai import AsyncOpenAI

import app.config as config
from app.shared.agent_defs import make_intake_agent, render_lead
from app.shared.models import ACTIONS, Lead
from app.shared.tokens import estimate_tokens, usage_tokens


@dataclass
class RunRecord:
    lead_id: str
    stage: str = "running"           # representative; the agent free-runs opaquely
    new_tokens: int = 0
    estimated: bool = False
    done: bool = False
    error: Optional[str] = None
    outcome: Optional[dict] = None
    steps: list[dict] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
    _seq: int = 0

    def add_step(self, kind: str, label: str) -> None:
        self._seq += 1
        self.steps.append({"id": self._seq, "type": kind, "label": label})

    def status(self) -> dict:
        return {
            "stage": "done" if self.done else self.stage,
            "done": self.done,
            "execution_status": "COMPLETED" if self.done else "RUNNING",
            "result": self.outcome,
            # Agent Only has no durable Wasted bucket — null signals "N/A" to the UI.
            "tokens": {
                "new": self.new_tokens,
                "wasted": None,
                "total": self.new_tokens,
                "estimated": self.estimated,
            },
            "durable": False,
        }


_RUNS: dict[str, RunRecord] = {}


def get_run(run_id: str) -> Optional[RunRecord]:
    return _RUNS.get(run_id)


def kill_run(run_id: str) -> bool:
    """Kill an in-flight Agent-Only run and discard every trace of it.

    Returns False if there was no such run. There is deliberately no recovery path: the
    tokens already spent are gone, and no record survives to inspect.
    """
    rec = _RUNS.pop(run_id, None)
    if rec is None:
        return False
    if rec.task is not None and not rec.task.done():
        rec.task.cancel()
    return True


class _Hooks(RunHooks):
    """Accumulate token usage and append a live step per model/tool call."""

    def __init__(self, rec: RunRecord) -> None:
        self._rec = rec

    async def on_llm_end(self, context, agent, response) -> None:  # type: ignore[override]
        u = getattr(response, "usage", None)
        n = (getattr(u, "total_tokens", 0) or 0) if u else 0
        if not n and u:
            n = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
        if n:
            self._rec.new_tokens += n
        self._rec.add_step("completed", "model call")

    async def on_tool_start(self, context, agent, tool) -> None:  # type: ignore[override]
        name = getattr(tool, "name", "tool")
        self._rec.add_step("started", f"tool: {name}")


def _coerce_action(value: str) -> str:
    return value if value in ACTIONS else "needs_info"


async def _run(run_id: str, lead: Lead) -> None:
    rec = _RUNS.get(run_id)
    if rec is None:
        return
    try:
        config.disable_agent_tracing()
        key = config.require_api_key()
        cfg = config.get_config()
        model = OpenAIChatCompletionsModel(
            model=cfg.model, openai_client=AsyncOpenAI(api_key=key, max_retries=0)
        )
        prompt = render_lead(lead)
        rec.add_step("started", "agent started (no Temporal)")
        result = await Runner.run(make_intake_agent(model), input=prompt, hooks=_Hooks(rec), max_turns=30)

        final = usage_tokens(result)
        if final and final > rec.new_tokens:
            rec.new_tokens = final
        if rec.new_tokens == 0:
            rec.new_tokens = estimate_tokens(prompt, str(result.final_output)) or 1
            rec.estimated = True

        r = result.final_output
        rec.outcome = {
            "action": _coerce_action(r.action),
            "summary": r.summary,
            "score": r.score,
            "owner": r.owner,
            "matched_contact": r.matched_contact,
            "tokens": {"new": rec.new_tokens, "wasted": None, "total": rec.new_tokens, "estimated": rec.estimated},
            "branch": r.branch_taken,
            "detail": r.detail,
            "human_override": False,
        }
        rec.add_step("completed", "agent finished")
    except asyncio.CancelledError:
        # The run was killed. Nothing is recorded, because there is nothing to recover.
        raise
    except Exception as e:  # noqa: BLE001
        rec.error = str(e)
        rec.outcome = {"error": str(e)}
        rec.add_step("failed", f"agent failed: {e}")
        rec.done = True
    else:
        rec.done = True


def start_run(lead: Lead) -> str:
    """Launch an Agent-Only intake as a background asyncio task; return its run id."""
    run_id = f"agent-only-{lead.slug}-{uuid.uuid4().hex[:6]}"
    rec = RunRecord(lead_id=lead.id)
    _RUNS[run_id] = rec
    rec.task = asyncio.create_task(_run(run_id, lead))
    return run_id
