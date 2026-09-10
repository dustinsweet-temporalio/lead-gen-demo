"""Prompt text and formatting helpers — no Agents-SDK dependency.

Lives apart from ``agent_defs`` so Fully Orchestrated (which uses plain chat-completions
calls, not the OpenAI Agents SDK) can build prompts without importing the framework. The
agent-driven variants re-use the bits they need from here too.
"""

from __future__ import annotations

import app.config as config
from app.shared.models import Lead


def render_lead(lead: Lead) -> str:
    return (
        f"Lead id: {lead.id}\n"
        f"Source: {lead.source}\n"
        f"From: {lead.from_name or '(no name given)'} <{lead.from_email}>\n"
        f"Company: {lead.company or '(not provided)'}\n"
        f"Subject: {lead.subject}\n"
        f"Message:\n{lead.body}\n"
    )


def territory_roster() -> str:
    """The AE-by-industry roster, rendered for the agent's prompt.

    The Fully Orchestrated workflow picks the owner from this same roster
    (``config.owner_for``), so all four variants name the SAME AE for a given lead — which
    matters when the presenter reads the owner aloud across consecutive runs.
    """
    lines = [
        f"     {industry} -> {ae}" for industry, ae in sorted(config.AE_BY_INDUSTRY.items())
    ]
    lines.append(f"     anything else, or no company matched -> {config.DEFAULT_AE}")
    return "\n".join(lines)


def routing_rubric() -> str:
    """The score-to-destination rubric, rendered from the configured thresholds.

    The same rubric text goes to the free-running agent (so its routing matches the
    workflow's) and into the Fully Orchestrated prompts as context. In Fully Orchestrated
    the *workflow* applies these thresholds itself — the model never picks the branch
    (CLAUDE.md rule 4).
    """
    cfg = config.get_config()
    return (
        f"Routing rubric (lead score, 0-100, from `score_lead`):\n"
        f"  score >= {cfg.sales_threshold}                  -> route to SALES\n"
        f"  {cfg.marketing_threshold} <= score < {cfg.sales_threshold}   -> route to MARKETING\n"
        f"  {cfg.qualify_threshold} <= score < {cfg.marketing_threshold}   -> route to NURTURE\n"
        f"  score < {cfg.qualify_threshold}                   -> ARCHIVE (not worth pursuing)\n"
    )


# --- Bounded-reasoning instructions (used by the Fully Orchestrated model activities and
#     by the tool-less judges; the data they reason over is in the prompt) ---

QUALITY_INSTRUCTIONS = (
    "You are a growth engineer's assistant screening inbound leads. Decide the ONE narrow "
    "question below, and summarise what the sender appears to want.\n\n"
    "The question is only: can we tell WHO this is?\n"
    "  has_enough_info = TRUE whenever you can identify the person or the organisation "
    "they represent — a name, a company, a business email domain, or form/event metadata "
    "naming them. This holds EVEN IF they state no specific request, no timeline, no "
    "budget and no project. Such a lead is complete but probably low value, and the "
    "scoring step downstream is what handles low value. Do not pre-empt it.\n"
    "  has_enough_info = FALSE only when you cannot tell who they are at all: no name, no "
    "company, no identifiable business domain, and no specific request either.\n\n"
    "Worked examples:\n"
    "  - A named consultant at a 2-person firm who took a brochure and has no current "
    "project -> TRUE (we know exactly who they are; they are simply low value).\n"
    "  - An engineer who downloaded a PDF and says they are not evaluating yet -> TRUE.\n"
    "  - 'hi saw your website, can you send me info?' from a free-mail address, no name, "
    "no company -> FALSE.\n\n"
    "Return a structured verdict: has_enough_info, buying_intent (a short phrase), a "
    "list of missing_info that would make it actionable, a confidence in [0,1], and a "
    "one-sentence rationale."
)

ROUTING_INSTRUCTIONS = (
    "You are a growth engineer's assistant writing up a lead that has ALREADY been "
    "tiered by score. The destination is decided — do not argue with it. Your job is the "
    "write-up the receiving team will read.\n"
    "Produce: the owner (for sales, the AE handle you are given; for marketing or "
    "nurture, the campaign name you are given), a crm_note of two or three sentences "
    "stating who this is, what they want, and what should happen next, up to three "
    "talking_points, and a one-sentence rationale."
)


# --- Data formatters (pure; safe to call from a workflow) -------------------------


def format_score(score: dict | None) -> str:
    if not score:
        return "Lead score: unavailable.\n"
    lines = [f"Lead score: {score.get('score')} / 100 (model {score.get('model_version', 'n/a')})"]
    for s in score.get("signals", []):
        lines.append(f"  - {s}")
    return "\n".join(lines) + "\n"


def format_contacts(candidates: list[dict]) -> str:
    if not candidates:
        return "Existing CRM contacts matching this lead: none found.\n"
    lines = ["Existing CRM contacts matching this lead:"]
    for c in candidates:
        lines.append(
            f"- {c.get('id')} {c.get('name')} at {c.get('company')} "
            f"(owner {c.get('owner')}, stage {c.get('stage')}, "
            f"last activity {c.get('last_activity')}, match {c.get('score')})"
        )
    return "\n".join(lines) + "\n"


def format_enrichment(data: dict | None) -> str:
    if not data:
        return "Enrichment: not run.\n"
    if not data.get("matched"):
        return f"Enrichment: NO company record matched ({data.get('reason', 'no reason given')}).\n"
    return (
        "Enrichment (company record):\n"
        f"  company: {data.get('company')} ({data.get('domain')})\n"
        f"  industry: {data.get('industry')}\n"
        f"  employees: {data.get('employees')}\n"
        f"  annual revenue (USD): {data.get('annual_revenue_usd')}\n"
        f"  HQ: {data.get('hq')}   locations: {data.get('locations')}\n"
        f"  contact title: {data.get('contact_title')} ({data.get('seniority')})\n"
        f"  tech stack: {', '.join(data.get('tech_stack') or []) or 'unknown'}\n"
    )


def format_verification(data: dict | None) -> str:
    if not data:
        return "Email verification: not run.\n"
    return (
        f"Email verification: {data.get('email')} -> {data.get('result')} "
        f"(deliverable={data.get('deliverable')})\n"
    )
