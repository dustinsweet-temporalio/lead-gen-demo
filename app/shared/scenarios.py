"""The seeded lead fixtures (SCENARIOS.md).

The reasoning is real (gpt-5.5); we shape the *inputs* (lead content, CRM matches,
enrichment data) and pin the **score** deterministically so genuine reasoning lands on the
intended route every run. Verdicts are never hardcoded.

LEAD-4102 (the mid-market multi-site inquiry) is the primary demo lead used for the live
runs. LEAD-4105 is the deterministically below-threshold lead that drives the human
review beat: it reads to any human as an obviously real prospect, which is the whole point
of the moment. Set ESCALATION_LEAD_ID=LEAD-4102 to make the primary demo lead itself the
borderline one instead (see config.escalation_lead_id).
"""

from __future__ import annotations

from app.shared.models import Lead

# UI "expected outcome" pill text + accent class (SCENARIOS.md §"expected pill values").
# Accents map to the four routing destinations plus the incomplete path.
EXPECTED_PILLS: dict[str, tuple[str, str]] = {
    "LEAD-4101": ("incomplete → needs info", "info"),
    "LEAD-4102": ("high score → sales", "sales"),
    "LEAD-4103": ("mid score → marketing", "marketing"),
    "LEAD-4104": ("low score → nurture", "nurture"),
    "LEAD-4105": ("below threshold → human review", "archive"),
    "LEAD-4106": ("retry → sales", "sales"),
}

SCENARIOS: list[Lead] = [
    Lead(
        id="LEAD-4102",
        source="inbound email",
        from_name="Marta Feld",
        from_email="marta.feld@northwind-retail.com",
        company="Northwind Retail Group",
        subject="Multi-site deployment — pricing and rollout questions",
        body=(
            "Hello,\n\n"
            "I lead store systems at Northwind Retail Group. We run 41 locations across "
            "the Midwest (about 850 staff total) and we're replacing the point-of-sale "
            "integration layer we built in-house in 2019.\n\n"
            "Two questions before we go further:\n"
            "1. How does licensing work when we deploy to all 41 sites at once vs. "
            "phasing it over two quarters?\n"
            "2. Do you support per-site configuration overrides, or does everything "
            "inherit from one central profile?\n\n"
            "We have budget approved for this fiscal year and want to be live in the "
            "first ten stores by the end of Q2. Happy to set up a call with me and our "
            "VP of Operations.\n\n"
            "Thanks,\n"
            "Marta Feld\n"
            "Director, Store Systems · Northwind Retail Group"
        ),
        status="New",
    ),
    Lead(
        id="LEAD-4103",
        source="web form",
        from_name="Devin Osei",
        from_email="devin.osei@brightlark.io",
        company="Brightlark Software",
        subject="Downloaded the integration guide",
        body=(
            "Form submission — Resource download\n\n"
            "Asset: 'Integration Patterns for Distributed Teams' (PDF)\n"
            "Name: Devin Osei\n"
            "Title: Software Engineer\n"
            "Company: Brightlark Software (approx. 120 employees)\n"
            "Comments: Reading up on options for a project next quarter. Not evaluating "
            "vendors yet, just gathering background for an internal write-up."
        ),
        status="New",
    ),
    Lead(
        id="LEAD-4104",
        source="event CSV",
        from_name="Priti Raman",
        from_email="priti@ramanconsult.net",
        company="Raman Consulting",
        subject="Booth scan — RetailTech Expo",
        body=(
            "Badge scan import (row 318 of retailtech-expo-day2.csv)\n\n"
            "Name: Priti Raman\n"
            "Title: Independent Consultant\n"
            "Company: Raman Consulting (1-5 employees)\n"
            "Notes from booth staff: 'Picked up a brochure, asked what we do. Advises "
            "small retailers on tooling; might refer clients later. No current project.'"
        ),
        status="New",
    ),
    Lead(
        id="LEAD-4105",
        source="inbound email",
        from_name="J. Whitcomb",
        from_email="jwhitcomb84@gmail.com",
        company="",
        subject="replacing our current vendor",
        body=(
            "Hi — we run 60 outpatient clinics and our contract with our current "
            "scheduling vendor ends in January. Need to have something chosen and "
            "signed before then. Who do I talk to about pricing at that scale?\n\n"
            "- Jim"
        ),
        status="New",
    ),
    Lead(
        id="LEAD-4101",
        source="inbound email",
        from_name="",
        from_email="hello.there.2291@mailbox-free.com",
        company="",
        subject="question",
        body="hi saw your website. can you send me info? thanks",
        status="New",
    ),
    Lead(
        id="LEAD-4106",
        source="inbound email",
        from_name="Alan Beitzel",
        from_email="a.beitzel@harborline-logistics.com",
        company="Harborline Logistics",
        subject="RFP timeline — fleet telematics integration",
        body=(
            "Good morning,\n\n"
            "Harborline Logistics is issuing an RFP next month for a telematics "
            "integration covering 320 vehicles across four depots. We're a 600-person "
            "company and this is a board-approved initiative for next year.\n\n"
            "Can you confirm you can support an on-premise deployment, and send over "
            "whatever security documentation you have? I'd like to include you in the "
            "shortlist.\n\n"
            "Alan Beitzel\n"
            "Head of Fleet Operations"
        ),
        status="New",
    ),
]

SCENARIOS_BY_ID: dict[str, Lead] = {lead.id: lead for lead in SCENARIOS}


def get_scenario(lead_id: str) -> Lead | None:
    return SCENARIOS_BY_ID.get(lead_id)


_SHORT_LABELS = {
    "LEAD-4101": "One-line inquiry",
    "LEAD-4102": "Multi-site deployment inquiry",
    "LEAD-4103": "Guide download",
    "LEAD-4104": "Expo badge scan",
    "LEAD-4105": "60-clinic vendor replacement",
    "LEAD-4106": "Fleet telematics RFP",
}

# The order the UI dropdown shows them in: the primary demo lead first, then the three
# leads from the routing slide, then the review-beat lead, then the retry lead.
_UI_ORDER = ["LEAD-4102", "LEAD-4103", "LEAD-4104", "LEAD-4105", "LEAD-4101", "LEAD-4106"]


def scenarios_for_api() -> list[dict]:
    """The lead list for the submission form.

    Deliberately does NOT include the expected outcome, its accent, or the seeded score:
    that modal is on screen in front of the audience, and shipping the answer to the
    browser — even unrendered — is how it leaks. EXPECTED_PILLS above stays as the record
    of intent for SCENARIOS.md and the tests.
    """
    out = []
    for lead_id in _UI_ORDER:
        lead = SCENARIOS_BY_ID[lead_id]
        out.append(
            {
                "id": lead.id,
                "label": _SHORT_LABELS.get(lead.id, lead.subject[:40]),
                "source": lead.source,
                "from_name": lead.from_name,
                "from_email": lead.from_email,
                "company": lead.company,
                "subject": lead.subject,
                "body": lead.body,
            }
        )
    return out
