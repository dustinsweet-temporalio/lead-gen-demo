"""Web-server-side Temporal client + history/heartbeat readers.

The web server holds its OWN client (separate from the worker) so it can start workflows,
read Queries, send Updates, and fetch history even while the worker is dead. The client
must serialize compatibly with the worker for the current variant: ``agent_steps`` uses
the OpenAIAgentsPlugin; the other Temporal variants use the pydantic data converter.
"""

from __future__ import annotations

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType, PendingActivityState, WorkflowExecutionStatus
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
from temporalio.contrib.pydantic import pydantic_data_converter

import app.config as config
from app.config import get_config

# Map history event types -> (timeline `type`, default label).
_EVENT_MAP = {
    "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED": ("started", "workflow started"),
    "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED": ("completed", "workflow completed"),
    "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED": ("failed", "workflow failed"),
    "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED": ("scheduled", "activity scheduled"),
    "EVENT_TYPE_ACTIVITY_TASK_STARTED": ("started", "activity started"),
    "EVENT_TYPE_ACTIVITY_TASK_COMPLETED": ("completed", "activity completed"),
    "EVENT_TYPE_ACTIVITY_TASK_FAILED": ("failed", "activity failed"),
    "EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT": ("timed_out", "activity timed out"),
    "EVENT_TYPE_TIMER_STARTED": ("waiting", "durable wait started"),
    "EVENT_TYPE_TIMER_FIRED": ("completed", "durable wait finished"),
    "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED": ("update", "human review accepted"),
    "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED": ("completed", "human review applied"),
    "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_REJECTED": ("failed", "human review rejected"),
}


async def connect_client(variant: str) -> Client:
    cfg = get_config()
    if variant == config.AGENT_STEPS:
        # Bare plugin on the client side ensures serialization matches the worker.
        return await Client.connect(
            cfg.temporal_address, namespace=cfg.temporal_namespace, plugins=[OpenAIAgentsPlugin()]
        )
    return await Client.connect(
        cfg.temporal_address, namespace=cfg.temporal_namespace, data_converter=pydantic_data_converter
    )


def _friendly(name: str) -> str:
    """Readable label for an activity type in the UI timeline.

    The activity type names are already business vocabulary (that is the point — they get
    read aloud off the Temporal UI); this only prettifies them for the web UI's own
    timeline.
    """
    mapping = {
        "run_lead_intake_agent": "agent (whole loop)",
        # Shared tool activities (activity_as_tool / called by the workflow)
        "fetch_lead_details": "fetch lead details",
        "check_duplicate_contact": "check duplicate contact",
        "score_lead": "score lead",
        "enrich_contact": "enrich contact",
        "verify_email": "verify email",
        "submit_email_verification": "submit email verification",
        "fetch_email_verification": "collect email verification",
        "update_crm": "update CRM",
        "route_to_sales": "route to sales",
        "route_to_marketing": "route to marketing",
        "route_to_nurture": "route to nurture",
        "archive_lead": "archive lead",
        "notify_slack": "notify Slack",
        # Agent Steps reasoning (the plugin's model-invocation activity)
        "InvokeModelActivity": "model call",
        "invoke_model_activity": "model call",
        # Fully Orchestrated reasoning (plain model-call activities — no agent framework)
        "assess_lead_quality": "judge: enough info?",
        "draft_routing_plan": "draft routing plan",
    }
    return mapping.get(name, name.replace("_", " "))


async def fetch_timeline(client: Client, workflow_id: str) -> list[dict]:
    """Return append-only timeline rows derived from real Temporal history."""
    handle = client.get_workflow_handle(workflow_id)
    history = await handle.fetch_history()

    # First pass: scheduled-event-id -> activity type name, to label started/completed.
    sched_names: dict[int, str] = {}
    for ev in history.events:
        if ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            sched_names[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name

    rows: list[dict] = []
    for ev in history.events:
        type_name = EventType.Name(ev.event_type)
        mapped = _EVENT_MAP.get(type_name)
        if not mapped:
            continue
        kind, label = mapped
        # Enrich activity rows with the activity name.
        if type_name == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            name = ev.activity_task_scheduled_event_attributes.activity_type.name
            label = f"{_friendly(name)} scheduled"
        elif type_name == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            sid = ev.activity_task_started_event_attributes.scheduled_event_id
            label = f"{_friendly(sched_names.get(sid, 'activity'))} started"
        elif type_name == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
            sid = ev.activity_task_completed_event_attributes.scheduled_event_id
            label = f"{_friendly(sched_names.get(sid, 'activity'))} completed"
        elif type_name == "EVENT_TYPE_ACTIVITY_TASK_FAILED":
            sid = ev.activity_task_failed_event_attributes.scheduled_event_id
            label = f"{_friendly(sched_names.get(sid, 'activity'))} failed (retrying)"
        elif type_name == "EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT":
            sid = ev.activity_task_timed_out_event_attributes.scheduled_event_id
            label = f"{_friendly(sched_names.get(sid, 'activity'))} timed out"

        rows.append(
            {
                "id": ev.event_id,
                "type": kind,
                "label": label,
                "time": ev.event_time.ToDatetime().isoformat() if ev.event_time else "",
            }
        )
    return rows


_AGENT_ACT = "run_lead_intake_agent"


async def agent_flow(client: Client, workflow_id: str) -> dict:
    """Derive the Agent-as-Workflow flowchart state (start → Agent → end).

    Returns one row per agent-activity attempt. On a crash/retry, the agent activity's
    attempt count grows: each later row shows the Agent re-running from scratch. Built from
    describe + history so it works while the worker is dead. Each row: {agent, result}.
    """
    cfg = get_config()
    resp = await client.workflow_service.describe_workflow_execution(
        DescribeWorkflowExecutionRequest(
            namespace=cfg.temporal_namespace,
            execution=WorkflowExecution(workflow_id=workflow_id),
        )
    )
    status = WorkflowExecutionStatus.Name(resp.workflow_execution_info.status)
    done = status == "WORKFLOW_EXECUTION_STATUS_COMPLETED"
    failed = status in (
        "WORKFLOW_EXECUTION_STATUS_FAILED",
        "WORKFLOW_EXECUTION_STATUS_TERMINATED",
        "WORKFLOW_EXECUTION_STATUS_TIMED_OUT",
    )
    pending = {pa.activity_type.name: pa for pa in resp.pending_activities}

    sched: dict[int, str] = {}
    agent_started = agent_completed = agent_failed = False
    agent_attempts: list[int] = []
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    for ev in history.events:
        t = EventType.Name(ev.event_type)
        if t == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            sched[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
        elif t == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            a = ev.activity_task_started_event_attributes
            if sched.get(a.scheduled_event_id) == _AGENT_ACT:
                agent_started = True
                agent_attempts.append(a.attempt)
        elif t == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
            if sched.get(ev.activity_task_completed_event_attributes.scheduled_event_id) == _AGENT_ACT:
                agent_completed = True
        elif t == "EVENT_TYPE_ACTIVITY_TASK_FAILED":
            if sched.get(ev.activity_task_failed_event_attributes.scheduled_event_id) == _AGENT_ACT:
                agent_failed = True

    agent_pa = pending.get(_AGENT_ACT)
    if agent_pa is not None:
        attempt = max(agent_pa.attempt, max(agent_attempts, default=1))
        running = PendingActivityState.Name(agent_pa.state) == "PENDING_ACTIVITY_STATE_STARTED"
        agent_cur = "running" if running else "pending"
        agent_started = True
    elif agent_completed:
        attempt, agent_cur = max(agent_attempts, default=1), "done"
    elif agent_failed:
        attempt, agent_cur = max(agent_attempts, default=1), "failed"
    elif agent_started:
        attempt, agent_cur = max(agent_attempts, default=1), "running"
    else:
        attempt, agent_cur = 0, "pending"

    rows: list[dict] = []
    if attempt == 0:
        rows.append({"agent": "pending", "result": "pending"})
    else:
        for i in range(1, attempt + 1):
            if i < attempt:
                agent_box = "failed"  # an earlier attempt that crashed
            else:
                agent_box = "done" if done else agent_cur
            result_box = "pending"
            if i == attempt:
                result_box = "done" if done else ("failed" if failed and agent_cur == "failed" else "pending")
            rows.append({"agent": agent_box, "result": result_box})
    return {"rows": rows}


# Agent-Steps activity-type -> hub-and-spoke node key. The agent calls the shared tool
# activities (each a durable activity); the plugin's model call is the inference hub.
_STEPS_NODE = {
    "invoke_model_activity": "inference",
    "InvokeModelActivity": "inference",
    "fetch_lead_details": "read_crm",
    "check_duplicate_contact": "read_crm",
    "score_lead": "score",
    "enrich_contact": "enrich",
    "verify_email": "verify",
    "update_crm": "write_crm",
    "route_to_sales": "route",
    "route_to_marketing": "route",
    "route_to_nurture": "route",
    "archive_lead": "route",
    "notify_slack": "notify",
}
_STEPS_NODE_KEYS = ["inference", "read_crm", "score", "enrich", "verify", "write_crm", "route", "notify"]


async def agent_steps_flow(client: Client, workflow_id: str) -> dict:
    """Agent-Steps hub-and-spoke flow (history-based; see _segmented_flow)."""
    return await _segmented_flow(client, workflow_id, _STEPS_NODE, _STEPS_NODE_KEYS)


# Fully Orchestrated: history-based segmentation (like Agent Steps), so a worker crash
# pops a second chart on restart showing replayed (cached) steps + the resume point.
# Each distinct activity maps to a node; update_crm maps to a BRANCH-specific node
# (resolved from the queried branch, since that activity differs only by params).
# The `review` diamond is the human gate (a wait_condition, not an activity) — overlaid
# onto the latest chart.
_ORCH_NODE_KEYS = [
    "intake", "quality_q", "score", "threshold_q", "review", "enrich", "verify", "tier_q",
    "archive", "archived", "needs_info", "needs_info_end",
    "to_sales", "sales_end", "to_marketing", "marketing_end", "to_nurture", "nurture_end",
]
# branch -> the terminal pair it lights up (route activity node, notify node)
_ORCH_BRANCH_NODES = {
    "sales": ("to_sales", "sales_end"),
    "marketing": ("to_marketing", "marketing_end"),
    "nurture": ("to_nurture", "nurture_end"),
    "archive": ("archive", "archived"),
    "needs_info": ("needs_info", "needs_info_end"),
}


async def orchestrated_flow(client: Client, workflow_id: str, review: dict | None, branch: str) -> dict:
    """Fully-Orchestrated directed flow, history-segmented by worker crash (see
    _segmented_flow). `branch` (from the outcome query) attributes the shared write
    activities to the right branch node; `review` lights the human-gate diamond."""
    node_map = {
        "fetch_lead_details": "intake",
        "check_duplicate_contact": "intake",
        "assess_lead_quality": "quality_q",
        "score_lead": "score",
        "enrich_contact": "enrich",
        "submit_email_verification": "verify",
        "fetch_email_verification": "verify",
        "draft_routing_plan": "tier_q",
        "route_to_sales": "to_sales",
        "route_to_marketing": "to_marketing",
        "route_to_nurture": "to_nurture",
        "archive_lead": "archive",
    }
    if branch in _ORCH_BRANCH_NODES:
        _route_node, notify_node = _ORCH_BRANCH_NODES[branch]
        node_map["update_crm"] = _route_node
        node_map["notify_slack"] = notify_node

    flow = await _segmented_flow(client, workflow_id, node_map, _ORCH_NODE_KEYS)

    # Overlay the human-review diamond and the threshold decision — neither is an
    # activity, so history can't see them directly. The gate sits immediately after
    # `score`, so its per-segment state mirrors the `score` node: green in the segment it
    # resolved, "replayed" (gray dashed) on later segments, gray before it. It only shows
    # at all when this run actually went through review.
    had_gate = bool(review and review.get("score") is not None)
    awaiting = bool(review and review.get("awaiting"))
    charts = flow["charts"]
    for k, chart in enumerate(charts):
        nodes = chart["nodes"]
        s = nodes.get("score", "gray")
        nodes["threshold_q"] = s
        if not had_gate:
            nodes["review"] = "gray"  # scored above the threshold → gate skipped
        elif awaiting and k == len(charts) - 1:
            nodes["review"] = "blue"  # currently parked, waiting on a person
        else:
            nodes["review"] = "replayed" if s == "replayed" else ("green" if s == "green" else "gray")
    return flow


async def _segmented_flow(client: Client, workflow_id: str, node_map: dict, node_keys: list) -> dict:
    """Per-node activity flow, segmented by worker crash (shared by the two flows).

    Each worker crash splits the run into a new "chart". Within chart k a node is: blue
    (running / was running at the crash that ended this segment), green (its activity
    completed in segment k), replayed (completed only in an earlier segment — Temporal
    returned the cached result, NOT re-run), or gray (not yet run). One chart per segment;
    the last is the live one.
    """
    cfg = get_config()
    resp = await client.workflow_service.describe_workflow_execution(
        DescribeWorkflowExecutionRequest(
            namespace=cfg.temporal_namespace,
            execution=WorkflowExecution(workflow_id=workflow_id),
        )
    )
    status = WorkflowExecutionStatus.Name(resp.workflow_execution_info.status)
    done = status == "WORKFLOW_EXECUTION_STATUS_COMPLETED"
    failed = status in (
        "WORKFLOW_EXECUTION_STATUS_FAILED",
        "WORKFLOW_EXECUTION_STATUS_TERMINATED",
        "WORKFLOW_EXECUTION_STATUS_TIMED_OUT",
    )

    seg = 0
    sched: dict[int, str] = {}                  # scheduled_event_id -> activity name
    completed: dict[str, set[int]] = {}         # node -> segments it completed in
    crashed_running: set[tuple[str, int]] = set()  # (node, seg) running at the crash that ended seg
    hist_attempt: dict[str, int] = {}           # node -> max started attempt seen in history

    # A crash-retry does NOT write a TIMED_OUT/FAILED event — it surfaces only as an
    # ActivityTaskStarted with attempt>1. So each such retry is a crash boundary.
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    for ev in history.events:
        t = EventType.Name(ev.event_type)
        if t == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            sched[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
        elif t == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            a = ev.activity_task_started_event_attributes
            name = sched.get(a.scheduled_event_id)
            node = node_map.get(name)
            if node:
                hist_attempt[node] = max(hist_attempt.get(node, 1), a.attempt)
                if a.attempt > 1:  # this attempt is a retry → the prior segment crashed
                    crashed_running.add((node, seg))
                    seg += 1
        elif t == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
            name = sched.get(ev.activity_task_completed_event_attributes.scheduled_event_id)
            if name in node_map:
                completed.setdefault(node_map[name], set()).add(seg)

    # Currently-running node from the pending-activity record. If it's a pending RETRY
    # (attempt>1) not yet reflected in history, open the crash segment now so the duplicate
    # chart appears during the dead-worker window.
    running_node: str | None = None
    for pa in resp.pending_activities:
        nm = pa.activity_type.name
        if nm in node_map:
            running_node = node_map[nm]
            if pa.attempt > hist_attempt.get(running_node, 1):
                crashed_running.add((running_node, seg))
                seg += 1

    num_segments = seg + 1

    def node_state(node: str, k: int, latest: bool) -> str:
        if latest and running_node == node:
            return "blue"
        if (node, k) in crashed_running:
            return "failed"  # was running when this segment's crash hit → show red, not blue
        if k in completed.get(node, set()):
            return "green"
        if any(s < k for s in completed.get(node, set())):
            return "replayed"
        return "gray"

    charts = []
    for k in range(num_segments):
        latest = k == num_segments - 1
        nodes = {n: node_state(n, k, latest) for n in node_keys}
        if latest:
            result = "done" if done else ("failed" if failed else "pending")
        else:
            result = "pending"  # this segment was frozen by a crash
        charts.append({"nodes": nodes, "result": result})
    return {"charts": charts}


async def pending_heartbeat_tokens(client: Client, workflow_id: str) -> dict | None:
    """Read the in-flight agent activity's last heartbeat (prior_wasted + attempt_tokens).

    Works even while the worker is dead: Temporal retains the last heartbeat on the pending
    activity. Returns None if there is no pending activity with token details.
    """
    cfg = get_config()
    resp = await client.workflow_service.describe_workflow_execution(
        DescribeWorkflowExecutionRequest(
            namespace=cfg.temporal_namespace,
            execution=WorkflowExecution(workflow_id=workflow_id),
        )
    )
    for pa in resp.pending_activities:
        payloads = pa.heartbeat_details.payloads
        if not payloads:
            continue
        try:
            details = client.data_converter.payload_converter.from_payloads(payloads)
        except Exception:
            continue
        if details and isinstance(details[0], dict) and "attempt_tokens" in details[0]:
            d = details[0]
            return {
                "prior_wasted": int(d.get("prior_wasted", 0)),
                "attempt_tokens": int(d.get("attempt_tokens", 0)),
                "estimated": bool(d.get("estimated", False)),
                "attempt": pa.attempt,
            }
    return None
