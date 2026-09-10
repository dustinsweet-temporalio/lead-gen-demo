"""The single shared lead-intake agent (OpenAIChatCompletionsModel -> gpt-5.5) and its prompt.

**Agent Only**, **Agent as Workflow** and **Agent Steps** run the SAME agent — same
SYSTEM_PROMPT, same tools, same single ``Runner.run`` — so their token totals are
comparable and the only difference is the Temporal durability wrapping. They differ only
in how the tools are supplied:

* **Agent Only / Agent as Workflow** — a concrete ``OpenAIChatCompletionsModel`` + the
  in-process ``@function_tool``s (app.shared.agent_tools).
* **Agent Steps** — the model as a string (the plugin resolves it to a model activity) +
  the tools wrapped with ``activity_as_tool`` (Temporal-activity-backed).

``include_usage=True`` so the SDK reports token usage (CLAUDE.md rule 3). The agent returns
a structured ``LeadIntakeAgentResult`` for the UI outcome card. (Fully Orchestrated uses no
agent — see app/activities/reasoning.py.)
"""

from __future__ import annotations

from typing import Optional

from agents import Agent, ModelSettings
from pydantic import BaseModel

from app.shared.agent_tools import AGENT_TOOLS
from app.shared.prompts import render_lead, routing_rubric, territory_roster  # re-exported

ModelLike = object
_USAGE = ModelSettings(include_usage=True)

__all__ = ["render_lead", "make_intake_agent", "LeadIntakeAgentResult", "system_prompt"]


class LeadIntakeAgentResult(BaseModel):
    """Structured result the intake agent returns after running the full flow."""

    action: str  # route_to_sales | route_to_marketing | route_to_nurture | archive | needs_info
    summary: str
    score: Optional[int] = None
    owner: Optional[str] = None            # AE handle or campaign name
    matched_contact: Optional[str] = None  # existing CRM contact id, if one was found
    branch_taken: str  # sales | marketing | nurture | archive | needs_info
    detail: str = ""


def system_prompt() -> str:
    """The intake agent's instructions, with the routing thresholds rendered in."""
    return (
        "You are the lead-intake agent for a B2B growth team. Given one inbound lead, run "
        "the COMPLETE intake flow yourself using the CRM, enrichment, email-verification "
        "and Slack tools, then return a single structured result.\n\n"
        "Flow:\n"
        "1. FETCH: call `fetch_lead_details(lead_id)` for the full record.\n"
        "2. KNOWN CONTACT: call `check_duplicate_contact(query)` with the sender's name, "
        "company and email. Note any match in your CRM note (set matched_contact to its "
        "id) — a match does not by itself change the routing.\n"
        "3. COMPLETENESS: the ONLY question here is whether you can tell WHO this is. If "
        "you cannot — no name, no company, no identifiable business domain and no specific "
        "request — then: `update_crm(lead_id, status='Needs Info', note=<what is "
        "missing>)`, then `notify_slack`. action='needs_info', "
        "branch_taken='needs_info'. STOP.\n"
        "   Otherwise CONTINUE to scoring, even when the lead states no request, no "
        "timeline, no budget and no project. A named person at a tiny company who took a "
        "brochure is COMPLETE but low value, and the score in step 4 is what handles low "
        "value — do not pre-empt it by asking for more information.\n"
        "4. SCORE: call `score_lead(lead_id)` and apply this rubric EXACTLY as written. "
        "The score decides the destination; do not substitute your own judgement for it.\n"
        + routing_rubric()
        + "5. ENRICH + VERIFY (skip for ARCHIVE — we do not pay the vendors for leads we "
        "are not pursuing): call `enrich_contact(lead_id)` then `verify_email(lead_id)`.\n"
        "6. ROUTE: call exactly ONE routing tool, matching the rubric:\n"
        "   - SALES: `route_to_sales(lead_id, owner=<AE handle>, reason=...)` then "
        "`update_crm(lead_id, status='Routed to Sales', owner=<AE handle>, note=...)`. "
        "action='route_to_sales', branch_taken='sales'. Pick the AE by the enriched "
        "industry, using this territory roster exactly:\n"
        + territory_roster()
        + "\n"
        "   - MARKETING: `route_to_marketing(lead_id, campaign='mkt.product-education-q3', "
        "reason=...)` then `update_crm(lead_id, status='Marketing Qualified', "
        "owner='mkt.product-education-q3', note=...)`. action='route_to_marketing', "
        "branch_taken='marketing'.\n"
        "   - NURTURE: `route_to_nurture(lead_id, campaign='nurture.long-cycle', "
        "reason=...)` then `update_crm(lead_id, status='Nurture', "
        "owner='nurture.long-cycle', note=...)`. action='route_to_nurture', "
        "branch_taken='nurture'.\n"
        "   - ARCHIVE: `archive_lead(lead_id, reason=...)` then `update_crm(lead_id, "
        "status='Archived', note=<why>)`. action='archive', branch_taken='archive'.\n"
        "7. NOTIFY: ALWAYS finish by posting ONE `notify_slack` to '#lead-routing' "
        "summarising the lead id, the score, the destination, and the owner.\n\n"
        "Set `score` to the score you got back. Put a human-readable summary in `summary` "
        "and any branch-specific detail (the CRM note, or the missing information "
        "requested) in `detail`."
    )


def make_intake_agent(model: ModelLike, tools: list | None = None) -> Agent:
    """The one intake agent, shared by the three agent-driven variants.

    ``tools`` defaults to the in-process function tools; Agent Steps passes the
    ``activity_as_tool``-wrapped equivalents.
    """
    return Agent(
        name="lead-intake-agent",
        instructions=system_prompt(),
        model=model,
        tools=tools if tools is not None else AGENT_TOOLS,
        model_settings=_USAGE,
        output_type=LeadIntakeAgentResult,
    )
