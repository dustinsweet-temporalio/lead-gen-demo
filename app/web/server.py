"""FastAPI web server — serves the UI and the /api surface (API_CONTRACT.md).

Long-lived: it owns the Worker Supervisor and a Temporal client, runs the Agent-Only
variant in-process, and is NOT killed during the demo. Token and timeline reads for the
Temporal variants go through the client's history/describe APIs (not Queries), so they keep
working while the worker is dead — that is how the UI narrates the dead-worker interval.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

import app.config as config
from app.agent_only import runner as agent_only
from app.shared import stages
from app.shared.models import Lead, LeadReviewDecision
from app.shared.scenarios import SCENARIOS_BY_ID, scenarios_for_api
from app.web.temporal_client import (
    agent_flow,
    agent_steps_flow,
    connect_client,
    fetch_timeline,
    orchestrated_flow,
    pending_heartbeat_tokens,
)
from app.worker.supervisor import WorkerSupervisor

config.disable_agent_tracing()

STATIC_DIR = Path(__file__).parent / "static"

_BYPASS_VARIANTS = (config.AGENT_STEPS, config.ORCHESTRATED)


class AppState:
    def __init__(self) -> None:
        self.supervisor = WorkerSupervisor()
        self.variant = config.AGENT
        self.client = None
        # Last-good cache so the UI keeps narrating while the worker is mid-restart.
        self.token_cache: dict[str, dict] = {}
        self.status_cache: dict[str, dict] = {}

    async def reconnect(self, *, retries: int = 30, delay: float = 2.0) -> None:
        # Retry so the app survives a still-booting Temporal (e.g. just after compose up).
        last = None
        for _ in range(retries):
            try:
                self.client = await connect_client(self.variant)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(delay)
        raise RuntimeError(f"could not connect to Temporal: {last}")


state = AppState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await state.reconnect()
    state.supervisor.start(state.variant)
    yield
    state.supervisor.stop()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def _no_store(request, call_next):
    # The demo's UI assets change between builds and the page is reloaded constantly.
    # Disable caching so a plain reload always gets the latest HTML/JS/CSS.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _temporal_url(workflow_id: str) -> str:
    cfg = config.get_config()
    return f"{cfg.temporal_ui}/namespaces/{cfg.temporal_namespace}/workflows/{workflow_id}"


# Workflow Queries are answered by the WORKER. When the worker is dead (the crash demo!) a
# Query would hang until RPC timeout. Skip Queries while the worker is down and fast-fail
# with a short timeout otherwise — token/timeline/status then fall back to cache +
# server-side describe/history, which keep working without a worker.
_QUERY_RPC_TIMEOUT = timedelta(seconds=1.2)


async def _query(workflow_id: str, name: str, default=None):
    if not state.supervisor.is_running():
        return default
    try:
        return await state.client.get_workflow_handle(workflow_id).query(
            name, rpc_timeout=_QUERY_RPC_TIMEOUT
        )
    except Exception:
        return default


# --- health & variant -------------------------------------------------------------


@app.get("/api/health")
async def health():
    cfg = config.get_config()
    return {
        "temporal_connected": state.client is not None,
        "worker_running": state.supervisor.is_running(),
        "temporal_ui": cfg.temporal_ui,
        "variant": state.variant,
        "durable": state.variant != config.AGENT_ONLY,
        "task_queue": (
            cfg.task_queue(state.variant) if state.variant != config.AGENT_ONLY else None
        ),
        # Shown in the UI so the audience can see why a lead routed where it did.
        "thresholds": {
            "sales": cfg.sales_threshold,
            "marketing": cfg.marketing_threshold,
            "qualify": cfg.qualify_threshold,
        },
        "escalation_lead_id": cfg.escalation_lead_id,
        "email_verify_delay_seconds": cfg.email_verify_delay_seconds,
        # Display-only: the UI multiplies elapsed time by this for "Time Spend".
        "time_multiplier": cfg.time_multiplier,
    }


@app.post("/api/variant")
async def set_variant(body: dict):
    variant = body.get("variant")
    if variant not in config.ALL_VARIANTS:
        return JSONResponse(
            {"error": f"variant must be one of {config.ALL_VARIANTS}"}, status_code=400
        )
    state.variant = variant
    state.supervisor.set_variant(variant)  # SIGKILL + relaunch (or stop, for agent_only)
    await state.reconnect()  # client serialization must match the new worker
    return {
        "variant": state.variant,
        "worker_running": state.supervisor.is_running(),
        "durable": variant != config.AGENT_ONLY,
    }


@app.get("/api/scenarios")
async def scenarios():
    return scenarios_for_api()


# --- lead intake ------------------------------------------------------------------


def _lead_from_body(body: dict) -> Lead:
    lead_id = body.get("id", "LEAD-0000")
    base = SCENARIOS_BY_ID.get(lead_id)
    return Lead(
        id=lead_id,
        source=body.get("source", base.source if base else "inbound email"),
        from_name=body.get("from_name", base.from_name if base else ""),
        from_email=body.get("from_email", base.from_email if base else ""),
        company=body.get("company", base.company if base else ""),
        subject=body.get("subject", base.subject if base else ""),
        body=body.get("body", base.body if base else ""),
        status=base.status if base else "New",
    )


async def _workflow_id(lead: Lead) -> str:
    """`lead-4102-orchestrated` — keyed to the lead so it is findable at a glance.

    If a workflow with that id is still running (a re-run during the same demo), append a
    short counter rather than silently colliding.
    """
    base = f"{lead.slug}-{config.variant_suffix(state.variant)}"
    for candidate in (base, *(f"{base}-{n}" for n in range(2, 10))):
        try:
            desc = await state.client.get_workflow_handle(candidate).describe()
        except RPCError:
            return candidate  # nothing with this id yet
        except Exception:
            return candidate
        if desc.status and desc.status.name != "RUNNING":
            return candidate  # closed — Temporal allows the id to be reused
    return base


@app.post("/api/intake")
async def intake(body: dict):
    lead = _lead_from_body(body)

    # Agent Only: no Temporal — run in-process and return an opaque run id.
    if state.variant == config.AGENT_ONLY:
        run_id = agent_only.start_run(lead)
        return {
            "workflow_id": run_id,
            "temporal_url": None,
            "variant": state.variant,
            "durable": False,
        }

    if not state.supervisor.is_running():
        return JSONResponse(
            {"error": "Worker is down — click Restart worker, then submit again."},
            status_code=409,
        )
    cfg = config.get_config()
    workflow_id = await _workflow_id(lead)
    # Workflow ids are reused across runs (lead-4102-agent), so drop any cached state for
    # this id first. Without this, the new run serves the PREVIOUS run's result and token
    # totals until the worker's Queries start answering — which made the score card reveal
    # itself the instant a lead was submitted, showing a stale score.
    state.status_cache.pop(workflow_id, None)
    state.token_cache.pop(workflow_id, None)
    await state.client.start_workflow(
        config.WORKFLOW_TYPE[state.variant],
        lead,
        id=workflow_id,
        task_queue=cfg.task_queue(state.variant),
    )
    return {
        "workflow_id": workflow_id,
        "temporal_url": _temporal_url(workflow_id),
        "variant": state.variant,
        "durable": True,
        "task_queue": cfg.task_queue(state.variant),
    }


@app.post("/api/agent_only/kill/{run_id}")
async def agent_only_kill(run_id: str):
    """Kill an in-flight Agent-Only run — the no-Temporal baseline's failure mode.

    The run is cancelled and its record deleted: no resumption, nothing left to inspect,
    every token already spent is gone.
    """
    if not agent_only.kill_run(run_id):
        return JSONResponse({"error": "unknown run"}, status_code=404)
    return {"ok": True, "recoverable": False}


# --- human lead review (Fully Orchestrated) ---------------------------------------


@app.post("/api/review/{workflow_id}")
async def review(workflow_id: str, body: dict):
    """Deliver the operator's lead-review decision as a Temporal **Update**.

    Body: {"confirm_archive": bool, "route_to": "sales"|"marketing"|"nurture"|null,
    "note": str}. An Update (not a signal) so the workflow's validator can reject a review
    for a lead that isn't parked — or an override that names no destination — and the
    operator gets that answer synchronously. The ack carries the effective score the
    gauge should switch to.
    """
    if state.variant != config.ORCHESTRATED:
        return JSONResponse(
            {"error": "lead review only applies to Temporal-Native Loop"}, status_code=400
        )
    if not state.supervisor.is_running():
        # An Update needs a worker to accept it. During the crash beat, restart first.
        return JSONResponse(
            {"error": "Worker is down — click Restart worker, then send the review again."},
            status_code=409,
        )
    decision = LeadReviewDecision(
        confirm_archive=bool(body.get("confirm_archive")),
        route_to=body.get("route_to"),
        reviewer=body.get("reviewer") or "operator",
        note=body.get("note") or "",
    )
    try:
        ack = await state.client.get_workflow_handle(workflow_id).execute_update(
            "lead_review", decision
        )
    except WorkflowUpdateFailedError as e:
        # The validator rejected it (already reviewed / not parked) — that is a real answer.
        return JSONResponse({"error": f"review rejected: {e.cause}"}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "decision": decision.model_dump(), "ack": ack}


# --- status / tokens / timeline ---------------------------------------------------


async def _execution_status(workflow_id: str) -> str:
    try:
        desc = await state.client.get_workflow_handle(workflow_id).describe()
        return desc.status.name if desc.status else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


@app.get("/api/status/{workflow_id}")
async def status(workflow_id: str):
    # Agent Only: read the in-process run record.
    if state.variant == config.AGENT_ONLY:
        rec = agent_only.get_run(workflow_id)
        if rec is None:
            # Killed (or never existed): there is deliberately nothing to recover.
            return JSONResponse(
                {"error": "unknown run", "lost": True}, status_code=404
            )
        return rec.status()

    exec_status = await _execution_status(workflow_id)
    out = {
        "stage": stages.START,
        "done": exec_status in ("COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT", "CANCELED"),
        "execution_status": exec_status,
        "result": None,
        "tokens": {"new": 0, "wasted": 0, "total": 0, "estimated": False},
        "durable": True,
    }
    cached = state.status_cache.get(workflow_id, {})
    # Run the Queries CONCURRENTLY. Right after a restart the worker isn't answering yet, so
    # a sequential chain would stack each query's RPC timeout (~seconds each) and stall the
    # whole status response — which delayed the resumed flowchart from appearing. One gather
    # bounds the wait to a single timeout; the flowchart itself comes from history below and
    # does not depend on these queries. (Unknown queries per-variant just return defaults.)
    stage, outcome, committed, bypassed, review_state = await asyncio.gather(
        _query(workflow_id, "get_stage"),
        _query(workflow_id, "get_outcome"),
        _query(workflow_id, "get_tokens"),
        _query(workflow_id, "get_bypassed", []),
        _query(workflow_id, "get_review"),
    )
    out["stage"] = stage if stage is not None else cached.get("stage", stages.START)
    out["result"] = outcome if outcome is not None else cached.get("result")
    if state.variant in _BYPASS_VARIANTS:
        out["bypassed"] = bypassed or []
    if stage is not None or outcome is not None:
        # Keep any cached review context: it is the only copy once the worker is dead and
        # Queries stop answering, and it is what keeps the score visible in the UI.
        entry = state.status_cache.setdefault(workflow_id, {})
        entry["stage"] = out["stage"]
        entry["result"] = out["result"]
    if committed and committed.get("total"):
        state.token_cache[workflow_id] = committed
    if exec_status == "FAILED":
        out["result"] = out["result"] or {"error": "workflow failed"}
    out["tokens"] = await _tokens(workflow_id, committed)

    # Flowchart state (derived from describe + history — survives worker death, no Query).
    try:
        if state.variant == config.AGENT:
            out["flow"] = await agent_flow(state.client, workflow_id)
        elif state.variant == config.AGENT_STEPS:
            out["flow"] = await agent_steps_flow(state.client, workflow_id)
        elif state.variant == config.ORCHESTRATED:
            review_state = review_state or state.status_cache.get(workflow_id, {}).get("review") or {}
            out["review"] = review_state
            if review_state.get("score") is not None:
                state.status_cache.setdefault(workflow_id, {})["review"] = review_state
            branch = (out["result"] or {}).get("branch", "") if out["result"] else ""
            out["flow"] = await orchestrated_flow(state.client, workflow_id, review_state, branch)
    except Exception:
        pass
    return out


async def _tokens(workflow_id: str, committed: dict | None = None) -> dict:
    # Committed buckets from workflow state (Query) when available, else cache.
    if committed is None:
        committed = await _query(workflow_id, "get_tokens")
        if committed and committed.get("total"):
            state.token_cache[workflow_id] = committed
    if not committed:
        committed = state.token_cache.get(
            workflow_id, {"new": 0, "wasted": 0, "total": 0, "estimated": False}
        )

    # Live in-flight figures from the pending agent activity's last heartbeat
    # (Agent as Workflow). Works even while the worker is dead.
    try:
        hb = await pending_heartbeat_tokens(state.client, workflow_id)
    except Exception:
        hb = None
    if hb:
        new = hb["attempt_tokens"]
        wasted = hb["prior_wasted"]
        return {"new": new, "wasted": wasted, "total": new + wasted, "estimated": hb["estimated"]}
    return committed


@app.get("/api/tokens/{workflow_id}")
async def tokens(workflow_id: str):
    if state.variant == config.AGENT_ONLY:
        rec = agent_only.get_run(workflow_id)
        if rec is None:
            return {"new": 0, "wasted": None, "total": 0, "estimated": False}
        return rec.status()["tokens"]
    return await _tokens(workflow_id)


@app.get("/api/timeline/{workflow_id}")
async def timeline(workflow_id: str):
    if state.variant == config.AGENT_ONLY:
        rec = agent_only.get_run(workflow_id)
        return rec.steps if rec else []
    try:
        return await fetch_timeline(state.client, workflow_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=404)


# --- worker controls --------------------------------------------------------------


@app.post("/api/worker/stop")
async def worker_stop():
    state.supervisor.stop()
    return {"worker_running": state.supervisor.is_running()}


@app.post("/api/worker/start")
async def worker_start():
    state.supervisor.start()
    return {"worker_running": state.supervisor.is_running()}


@app.post("/api/reset")
async def reset():
    state.token_cache.clear()
    state.status_cache.clear()
    return {"ok": True}


# --- static UI (mounted last so /api/* wins) --------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    cfg = config.get_config()
    uvicorn.run(app, host="0.0.0.0", port=cfg.web_port)


if __name__ == "__main__":
    main()
