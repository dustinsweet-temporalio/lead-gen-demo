"""In-process @function_tool tools for the free-running lead-intake agent (Agent Only and
Agent as Workflow), backed by the shared fixtures.

These mirror, one-for-one, the @activity.defn tools in app/activities/tool_activities.py
(used by Agent Steps and Fully Orchestrated). Those variants run the *identical* agent and
SYSTEM_PROMPT — only the durability wrapping of these tools differs.

Tool names are the vocabulary of the talk track (fetch_lead_details, enrich_contact,
verify_email, check_duplicate_contact, score_lead, route_to_sales, update_crm,
notify_slack): they are read aloud off the Temporal UI, so they never get generic names.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from agents import function_tool

import app.config as config
from app.shared import fixtures

# In-memory record of what the tools were asked to do. Resets with the process; the
# durable record lives in Temporal, which is the point.
_routed: list[dict] = []


@function_tool
def fetch_lead_details(lead_id: str) -> dict:
    """Retrieve the lead's full record from the CRM (sender, company, subject, message, queue)."""
    return fixtures.lead_detail(lead_id) or {"error": f"Unknown lead {lead_id}"}


@function_tool
def check_duplicate_contact(query: str) -> list[dict]:
    """Search the CRM for an existing contact matching this lead (name, company, email).
    Returns candidates ranked by match strength; empty when the lead is new to the CRM."""
    return fixtures.search_contacts(query)


@function_tool
def score_lead(lead_id: str) -> dict:
    """Score the lead 0-100 with the lead-scoring service, with the signals behind the score."""
    return fixtures.lead_score(lead_id).model_dump()


@function_tool
async def enrich_contact(lead_id: str) -> dict:
    """Enrich the contact with company data from the enrichment provider (industry,
    headcount, revenue, locations, title). `matched: false` means no company record was found."""
    return fixtures.enrichment(lead_id)


@function_tool
async def verify_email(lead_id: str) -> dict:
    """Verify the lead's email address is deliverable with the email-verification provider.
    This provider is slow — the call can take a while to come back."""
    await asyncio.sleep(config.get_config().email_verify_delay_seconds)
    return fixtures.email_verification(lead_id).model_dump()


@function_tool
def update_crm(
    lead_id: str,
    status: Optional[str] = None,
    owner: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Update the lead in the CRM: set status (e.g. 'Needs Info', 'Routed to Sales',
    'Marketing Qualified', 'Nurture', 'Archived'), set the owner, and/or add a note."""
    return {"lead_id": lead_id, "status": status, "owner": owner, "note_added": bool(note)}


@function_tool
def route_to_sales(lead_id: str, owner: str, reason: str) -> dict:
    """Route the lead to a sales AE for follow-up. Use for high-scoring leads."""
    _routed.append({"lead_id": lead_id, "destination": "sales", "owner": owner})
    return {"lead_id": lead_id, "destination": "sales", "owner": owner, "sla_hours": 4}


@function_tool
def route_to_marketing(lead_id: str, campaign: str, reason: str) -> dict:
    """Route the lead into a marketing campaign. Use for mid-scoring leads."""
    _routed.append({"lead_id": lead_id, "destination": "marketing", "owner": campaign})
    return {"lead_id": lead_id, "destination": "marketing", "campaign": campaign}


@function_tool
def route_to_nurture(lead_id: str, campaign: str, reason: str) -> dict:
    """Route the lead into a long-cycle nurture campaign. Use for low-scoring leads that
    are still worth keeping."""
    _routed.append({"lead_id": lead_id, "destination": "nurture", "owner": campaign})
    return {"lead_id": lead_id, "destination": "nurture", "campaign": campaign}


@function_tool
def archive_lead(lead_id: str, reason: str) -> dict:
    """Archive the lead as not worth pursuing. Use only for leads below the qualifying score."""
    _routed.append({"lead_id": lead_id, "destination": "archive", "owner": None})
    return {"lead_id": lead_id, "destination": "archive", "archived": True}


@function_tool
def notify_slack(channel: str, text: str) -> dict:
    """Post the single terminal lead-routing notification to a channel (e.g. '#lead-routing')."""
    return {"ts": f"{1700000000 + len(_routed)}.000100", "channel": channel}


AGENT_TOOLS = [
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
