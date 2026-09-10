"""Tool activities — the intake tool set as @activity.defn, backed by the shared fixtures.
Mirror, one-for-one, the @function_tool tools in app/shared/agent_tools.py.

Used two ways:
  * Agent Steps — each is wrapped with ``activity_as_tool`` and the agent calls it.
  * Fully Orchestrated — the workflow calls them directly, around its own model-call
    activities.

Each call is a real Activity boundary, so the event history reads as named business steps:
fetch_lead_details, check_duplicate_contact, score_lead, enrich_contact, verify_email,
update_crm, route_to_sales, notify_slack. These names are read aloud off the Temporal UI
during the presentation — never rename them to anything generic.
"""

from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

import app.config as config
from app.shared import fixtures

# Modest latency on the read tools keeps the timeline readable and gives the operator a
# window to crash mid-step. Well under any heartbeat timeout.
_READ_LATENCY_SECONDS = 0.4

# Per-process routing log (resets per worker; durable state lives in Temporal).
_routed: list[dict] = []

# Fault injection (keyed by workflow + step). The FIRST attempt of the routing step fails
# RETRYABLY for the retry-demo lead, then succeeds (SCENARIOS.md). By then the expensive
# reasoning is already banked in completed activities, so the automatic retry wastes
# essentially nothing — the contrast to Agent as Workflow, where a retry re-runs the lot.
_RETRY_DEMO_LEAD = "LEAD-4106"
_attempts: dict[tuple[str, str], int] = {}


def _maybe_inject_failure(workflow_id: str, step: str, lead_id: str) -> None:
    if lead_id != _RETRY_DEMO_LEAD:
        return
    key = (workflow_id, step)
    _attempts[key] = _attempts.get(key, 0) + 1
    if _attempts[key] == 1:
        raise ApplicationError(
            f"enrichment provider returned 503 for {lead_id} (injected transient failure)",
            non_retryable=False,
        )


async def _heartbeat_loop() -> None:
    """Heartbeat while a slow call runs so a worker crash is detected at the activity's
    heartbeat_timeout rather than waiting out start_to_close."""
    hb = activity.info().heartbeat_timeout
    if not hb:
        return
    interval = max(0.4, hb.total_seconds() / 2)
    while True:
        await asyncio.sleep(interval)
        activity.heartbeat()


# --- CRM --------------------------------------------------------------------------


@activity.defn
async def fetch_lead_details(lead_id: str) -> dict:
    """Retrieve the lead's full record from the CRM."""
    return fixtures.lead_detail(lead_id) or {"error": f"Unknown lead {lead_id}"}


@activity.defn
async def check_duplicate_contact(query: str) -> list[dict]:
    """Search the CRM for an existing contact matching this lead; empty when it is new."""
    await asyncio.sleep(_READ_LATENCY_SECONDS)
    return fixtures.search_contacts(query)


@activity.defn
async def update_crm(
    lead_id: str,
    status: str | None = None,
    owner: str | None = None,
    note: str | None = None,
) -> dict:
    """Update the lead in the CRM: status / owner / note."""
    return {"lead_id": lead_id, "status": status, "owner": owner, "note_added": bool(note)}


# --- Scoring ----------------------------------------------------------------------


@activity.defn
async def score_lead(lead_id: str) -> dict:
    """Score the lead 0-100 with the lead-scoring service, with the signals behind it."""
    return fixtures.lead_score(lead_id).model_dump()


# --- Enrichment + verification providers ------------------------------------------


@activity.defn
async def enrich_contact(lead_id: str) -> dict:
    """Enrich the contact with company data from the enrichment provider."""
    await asyncio.sleep(_READ_LATENCY_SECONDS)
    return fixtures.enrichment(lead_id)


@activity.defn
async def verify_email(lead_id: str) -> dict:
    """Verify the lead's email with the email-verification provider — the slow dependency.

    The whole wait happens inside this activity, so a long provider latency shows up as one
    long-running activity (heartbeating, so a crash is still detected in ~2s) and a worker
    crash mid-wait restarts the wait. Fully Orchestrated instead splits this into
    submit -> durable timer -> fetch (see submit_email_verification /
    fetch_email_verification), which survives a crash without restarting the wait.
    """
    hb = asyncio.create_task(_heartbeat_loop())
    try:
        await asyncio.sleep(config.get_config().email_verify_delay_seconds)
    finally:
        hb.cancel()
    return fixtures.email_verification(lead_id).model_dump()


@activity.defn
async def submit_email_verification(lead_id: str) -> dict:
    """Submit the address to the email-verification provider and return immediately.

    The provider is asynchronous and can take 30-60 minutes to come back. Rather than
    holding an activity (or a polling loop) open for the duration, we submit here, let the
    WORKFLOW wait on a durable timer, and collect the result with
    ``fetch_email_verification``. The wait costs nothing while it runs and survives a
    worker crash — a workflow that waits an hour mid-run is just a workflow that is slow
    in the middle.
    """
    delay = config.get_config().email_verify_delay_seconds
    return {
        "lead_id": lead_id,
        "request_id": f"evr-{lead_id.lower()}",
        "status": "pending",
        "eta_seconds": delay,
    }


@activity.defn
async def fetch_email_verification(lead_id: str, request_id: str) -> dict:
    """Collect the finished email-verification result the provider has been working on."""
    return fixtures.email_verification(lead_id).model_dump()


# --- Routing (exactly one of these fires per run) ---------------------------------


@activity.defn
async def route_to_sales(lead_id: str, owner: str, reason: str) -> dict:
    """Route the lead to a sales AE for follow-up. Home of the injected retry for the
    retry-demo lead: the reasoning is already banked in completed activities, so the
    automatic retry wastes almost nothing."""
    _maybe_inject_failure(activity.info().workflow_id, "route_to_sales", lead_id)
    _routed.append({"lead_id": lead_id, "destination": "sales", "owner": owner})
    return {"lead_id": lead_id, "destination": "sales", "owner": owner, "sla_hours": 4}


@activity.defn
async def route_to_marketing(lead_id: str, campaign: str, reason: str) -> dict:
    """Route the lead into a marketing campaign."""
    _routed.append({"lead_id": lead_id, "destination": "marketing", "owner": campaign})
    return {"lead_id": lead_id, "destination": "marketing", "campaign": campaign}


@activity.defn
async def route_to_nurture(lead_id: str, campaign: str, reason: str) -> dict:
    """Route the lead into a long-cycle nurture campaign."""
    _routed.append({"lead_id": lead_id, "destination": "nurture", "owner": campaign})
    return {"lead_id": lead_id, "destination": "nurture", "campaign": campaign}


@activity.defn
async def archive_lead(lead_id: str, reason: str) -> dict:
    """Archive the lead as not worth pursuing."""
    _routed.append({"lead_id": lead_id, "destination": "archive", "owner": None})
    return {"lead_id": lead_id, "destination": "archive", "archived": True}


# --- Slack ------------------------------------------------------------------------


@activity.defn
async def notify_slack(channel: str, text: str) -> dict:
    """The single terminal lead-routing notification every branch posts."""
    return {"ts": f"{1700000000 + len(_routed)}.000100", "channel": channel}


# Wrapped by activity_as_tool for the agent (Agent Steps). `verify_email` is the
# single-activity (agent-facing) form of the slow call; the submit/fetch pair is
# workflow-facing only, so the agent is not asked to manage a durable timer.
TOOL_ACTIVITIES = [
    fetch_lead_details,
    check_duplicate_contact,
    score_lead,
    enrich_contact,
    verify_email,
    update_crm,
    route_to_sales,
    route_to_marketing,
    route_to_nurture,
    archive_lead,
    notify_slack,
]

# Everything the worker must register (the two workflow-facing verification activities are
# not offered to the agent as tools).
ALL_TOOL_ACTIVITIES = [
    *TOOL_ACTIVITIES,
    submit_email_verification,
    fetch_email_verification,
]
