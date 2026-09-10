"""Deterministic, canned fixture data behind the CRM / enrichment / email-verification /
Slack tools.

Given the same lead id, every lookup returns the same data every run — no randomness, no
time-dependent output — so the agent-restart-from-scratch and resume-from-last-step
behaviours are both reproducible, and the figures the presenter quotes hold across runs.
The agent still genuinely *chooses* which tools to call; only the bytes returned are
fixtures. No network calls, no vendor credentials: the demo runs fully offline except for
the model.
"""

from __future__ import annotations

import re

import app.config as config
from app.shared.models import EmailVerification, LeadScore
from app.shared.scenarios import SCENARIOS_BY_ID

# --- CRM: the lead record ---------------------------------------------------------

# Which rep currently owns the inbound queue each lead landed in.
_QUEUE_OWNER = {
    "inbound email": "inbound-queue",
    "web form": "web-queue",
    "event CSV": "events-queue",
}


def lead_detail(lead_id: str) -> dict | None:
    lead = SCENARIOS_BY_ID.get(lead_id)
    if not lead:
        return None
    return {
        "id": lead.id,
        "status": lead.status,
        "source": lead.source,
        "from_name": lead.from_name or "(not provided)",
        "from_email": lead.from_email,
        "company": lead.company or "(not provided)",
        "subject": lead.subject,
        "body": lead.body,
        "queue": _QUEUE_OWNER.get(lead.source, "inbound-queue"),
        "owner": None,
    }


# --- CRM: existing contacts, for the duplicate-contact check ----------------------
# A small, specific book of business. Only a couple of leads have a real match; the rest
# return low-overlap noise or nothing, so the "is this already in the CRM" answer is
# genuine rather than fabricated.

EXISTING_CONTACTS = [
    {
        "id": "CON-8871",
        "name": "Devin Osei",
        "company": "Brightlark Software",
        "email": "devin.osei@brightlark.io",
        "owner": "ae.rivera",
        "last_activity": "2026-05-14",
        "stage": "Marketing qualified",
    },
    {
        "id": "CON-8420",
        "name": "Sasha Lindqvist",
        "company": "Northwind Retail Group",
        "email": "sasha.lindqvist@northwind-retail.com",
        "owner": "ae.tan",
        "last_activity": "2025-11-02",
        "stage": "Closed lost",
    },
    {
        "id": "CON-9107",
        "name": "Owen Brady",
        "company": "Cardinal Freight",
        "email": "obrady@cardinalfreight.com",
        "owner": "ae.rivera",
        "last_activity": "2026-06-30",
        "stage": "Nurture",
    },
    {
        "id": "CON-7756",
        "name": "Hana Ito",
        "company": "Pearl District Dental",
        "email": "hito@pearldistrictdental.com",
        "owner": "ae.okafor",
        "last_activity": "2026-02-19",
        "stage": "Closed won",
    },
]

_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "in", "on", "for", "and", "or", "when",
    "with", "that", "this", "it", "its", "be", "as", "at", "by", "from", "com", "net",
    "io", "inc", "llc", "ltd", "group", "software", "consulting", "we", "our", "us",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def search_contacts(query: str) -> list[dict]:
    """Real keyword-overlap search over the existing contact book.

    Returns candidates ranked by overlap (Jaccard over content tokens). Whether the lead
    is already known is judged from these genuine signals; nothing is fabricated.
    """
    q = _tokens(query)
    scored = []
    for contact in EXISTING_CONTACTS:
        terms = _tokens(f"{contact['name']} {contact['company']} {contact['email']}")
        if not terms:
            continue
        overlap = q & terms
        score = len(overlap) / len(q | terms) if (q | terms) else 0.0
        scored.append(
            {
                "id": contact["id"],
                "name": contact["name"],
                "company": contact["company"],
                "email": contact["email"],
                "owner": contact["owner"],
                "stage": contact["stage"],
                "last_activity": contact["last_activity"],
                "overlap_terms": sorted(overlap),
                "score": round(score, 3),
            }
        )
    scored.sort(key=lambda c: c["score"], reverse=True)
    return [c for c in scored if c["score"] > 0.0][:4]


# --- Lead scoring (deterministic) -------------------------------------------------
# The scoring service is a stub that returns a fixed score per seeded lead. This is
# deliberate: the human-review beat cannot depend on the model happening to score a lead
# low on a given run (SCENARIOS.md §"Determinism of the escalation"). The *reasoning*
# around the score is still real; the number is pinned.

_BASE_SCORES: dict[str, tuple[int, list[str]]] = {
    "LEAD-4102": (
        87,
        [
            "business email domain matches an enriched company record",
            "stated headcount 850 (mid-market band)",
            "seniority: director-level title detected",
            "explicit budget and timeline language",
            "multi-site scope (41 locations)",
        ],
    ),
    "LEAD-4103": (
        62,
        [
            "business email domain matches an enriched company record",
            "company size 120 (SMB band)",
            "seniority: individual contributor",
            "content-download intent, no evaluation language",
            "existing CRM contact at marketing-qualified stage",
        ],
    ),
    "LEAD-4104": (
        38,
        [
            "company size 1-5 (micro band)",
            "seniority: independent consultant",
            "no project or timeline stated",
            "event badge scan, low engagement depth",
        ],
    ),
    "LEAD-4105": (
        18,
        [
            "free-mail domain (gmail.com) — no company record matched",
            "company name not provided",
            "no headcount or revenue signal available",
            "message length below the qualifying floor (34 words)",
            "no title or seniority signal detected",
        ],
    ),
    "LEAD-4101": (
        8,
        [
            "disposable-mail domain — no company record matched",
            "company name not provided",
            "no name provided",
            "message length below the qualifying floor (11 words)",
            "no intent keywords detected",
        ],
    ),
    "LEAD-4106": (
        81,
        [
            "business email domain matches an enriched company record",
            "stated headcount 600 (mid-market band)",
            "seniority: head-of-function title detected",
            "RFP and board-approval language",
            "fleet scope (320 vehicles, 4 depots)",
        ],
    ),
}

# Alternate profile for LEAD-4105 when it is NOT the configured escalation lead: it scores
# above the qualifying floor instead, so exactly ONE seeded lead ever parks for human
# review no matter which lead ESCALATION_LEAD_ID names.
_ALT_SCORES: dict[str, tuple[int, list[str]]] = {
    "LEAD-4105": (
        68,
        [
            "free-mail domain (gmail.com) — no company record matched",
            "stated scope 60 locations (mid-market band)",
            "explicit contract-renewal deadline",
            "pricing request detected",
        ],
    ),
}

# What the scorer reports when a lead is the configured escalation lead but has no
# low-score profile of its own (i.e. ESCALATION_LEAD_ID was pointed at a different lead).
_ESCALATION_SIGNALS = [
    "sparse firmographic match — enrichment returned no confident company record",
    "no headcount or revenue signal available",
    "message length below the qualifying floor",
    "no title or seniority signal detected",
]
_ESCALATION_SCORE = 18


def lead_score(lead_id: str) -> LeadScore:
    """The deterministic score for a lead.

    The lead named by ``ESCALATION_LEAD_ID`` always scores below the qualify threshold,
    so the Fully Orchestrated human-review gate fires reliably on every run — that beat
    gets one shot on stage.
    """
    cfg = config.get_config()
    base = _BASE_SCORES.get(lead_id)
    if lead_id == cfg.escalation_lead_id:
        # Prefer the lead's own hand-written low-score signals when it has them.
        if base and base[0] < cfg.qualify_threshold:
            return LeadScore(lead_id=lead_id, score=base[0], signals=list(base[1]))
        return LeadScore(lead_id=lead_id, score=_ESCALATION_SCORE, signals=list(_ESCALATION_SIGNALS))
    # Not the escalation lead: a lead whose only profile is a below-threshold one falls
    # back to its alternate, so it doesn't park for review as well.
    alt = _ALT_SCORES.get(lead_id)
    if alt and base and base[0] < cfg.qualify_threshold:
        return LeadScore(lead_id=lead_id, score=alt[0], signals=list(alt[1]))
    if base:
        return LeadScore(lead_id=lead_id, score=base[0], signals=list(base[1]))
    # Unknown / hand-composed lead: a neutral mid score keeps it out of both the archive
    # and the sales tier so an ad-hoc submission still routes somewhere sensible.
    return LeadScore(
        lead_id=lead_id,
        score=55,
        signals=["no seeded profile for this lead — scored on message content only"],
    )


# --- Enrichment provider: company data --------------------------------------------
# Stands in for a contact-enrichment vendor. Keyed by lead id; returns a company record
# for the leads whose email domain resolves, and an explicit miss for the others (which
# is exactly why those leads score low).

_ENRICHMENT: dict[str, dict] = {
    "LEAD-4102": {
        "matched": True,
        "company": "Northwind Retail Group",
        "domain": "northwind-retail.com",
        "industry": "Retail",
        "employees": 850,
        "annual_revenue_usd": 240_000_000,
        "hq": "Columbus, OH",
        "locations": 41,
        "tech_stack": ["in-house POS integration", "Oracle Retail", "SAP"],
        "contact_title": "Director, Store Systems",
        "seniority": "director",
    },
    "LEAD-4103": {
        "matched": True,
        "company": "Brightlark Software",
        "domain": "brightlark.io",
        "industry": "Software",
        "employees": 120,
        "annual_revenue_usd": 18_000_000,
        "hq": "Austin, TX",
        "locations": 2,
        "tech_stack": ["AWS", "Postgres", "Kafka"],
        "contact_title": "Software Engineer",
        "seniority": "individual contributor",
    },
    "LEAD-4104": {
        "matched": True,
        "company": "Raman Consulting",
        "domain": "ramanconsult.net",
        "industry": "Professional services",
        "employees": 2,
        "annual_revenue_usd": 400_000,
        "hq": "Portland, OR",
        "locations": 1,
        "tech_stack": [],
        "contact_title": "Independent Consultant",
        "seniority": "owner",
    },
    "LEAD-4105": {
        # The miss that produces the low score — and the reason a human catches it.
        "matched": False,
        "reason": "free-mail domain (gmail.com); no company name supplied to match on",
        "company": None,
        "domain": "gmail.com",
        "employees": None,
    },
    "LEAD-4101": {
        "matched": False,
        "reason": "disposable-mail domain; no company name supplied to match on",
        "company": None,
        "domain": "mailbox-free.com",
        "employees": None,
    },
    "LEAD-4106": {
        "matched": True,
        "company": "Harborline Logistics",
        "domain": "harborline-logistics.com",
        "industry": "Transportation & logistics",
        "employees": 600,
        "annual_revenue_usd": 155_000_000,
        "hq": "Tacoma, WA",
        "locations": 4,
        "tech_stack": ["on-premise VMware", "Samsara", "Oracle TMS"],
        "contact_title": "Head of Fleet Operations",
        "seniority": "head of function",
    },
}

_ENRICHMENT_MISS = {
    "matched": False,
    "reason": "no company record matched for this domain",
    "company": None,
    "employees": None,
}


def enrichment(lead_id: str) -> dict:
    return dict(_ENRICHMENT.get(lead_id, _ENRICHMENT_MISS))


# --- Email-verification provider --------------------------------------------------
# Stands in for an email-deliverability vendor — the slow third-party dependency. The
# latency is configurable (EMAIL_VERIFY_DELAY_SECONDS); in Fully Orchestrated the wait is
# a durable workflow timer, so the workflow can sit in it for an hour and survive a
# worker crash.

_UNDELIVERABLE = {"LEAD-4101"}


def email_verification(lead_id: str) -> EmailVerification:
    lead = SCENARIOS_BY_ID.get(lead_id)
    email = lead.from_email if lead else ""
    if lead_id in _UNDELIVERABLE:
        return EmailVerification(
            email=email,
            deliverable=False,
            result="undeliverable — mailbox does not exist",
            provider_latency_seconds=config.get_config().email_verify_delay_seconds,
        )
    return EmailVerification(
        email=email,
        deliverable=True,
        result="deliverable",
        provider_latency_seconds=config.get_config().email_verify_delay_seconds,
    )


# --- Sales roster: who a routed lead goes to --------------------------------------
# The roster itself lives in config (pure data + pure lookup, so the Fully Orchestrated
# workflow can decide the owner itself rather than asking an activity).


def assigned_ae(lead_id: str) -> str:
    """Territory owner for a lead, by enriched industry."""
    industry = (enrichment(lead_id) or {}).get("industry")
    return config.owner_for("sales", industry)


# --- Slack ------------------------------------------------------------------------

SLACK_CHANNEL = "#lead-routing"
