"""Pydantic models shared across workflows, activities, agents, and the API.

All agent return types are explicit Pydantic models (CLAUDE.md rule 4): the Fully
Orchestrated workflow branches on these structured verdicts, and the Pydantic data
converter serializes them cleanly across the activity boundary.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# --- Domain types -----------------------------------------------------------------

# Where the lead came in from (inbound email, web form, event CSV, …). Display metadata
# for the prompt and the UI — nothing branches on it.
Source = str

# The routing decisions the intake flow can reach. `needs_info` is the incomplete-lead
# path; the other four are the routing destinations shown in the UI.
Action = Literal["route_to_sales", "route_to_marketing", "route_to_nurture", "archive", "needs_info"]

# The path taken through intake. Mirrors Action, and lets the UI mark bypassed steps.
Branch = Literal["sales", "marketing", "nurture", "archive", "needs_info"]

ACTIONS: tuple[str, ...] = (
    "route_to_sales",
    "route_to_marketing",
    "route_to_nurture",
    "archive",
    "needs_info",
)


class Lead(BaseModel):
    """An incoming lead to qualify and route."""

    id: str
    source: Source = "inbound email"
    from_name: str = ""
    from_email: str = ""
    company: str = ""
    subject: str
    body: str
    status: str = "New"

    @property
    def slug(self) -> str:
        """`LEAD-4102` -> `lead-4102`, the human-readable base for workflow ids."""
        return self.id.lower()


# --- Scoring ----------------------------------------------------------------------


class LeadScore(BaseModel):
    """The deterministic lead score, as returned by the scoring service.

    Deliberately deterministic per lead id (SCENARIOS.md): the demo's escalation beat
    cannot depend on a model happening to score low on a given run.
    """

    lead_id: str
    score: int = Field(ge=0, le=100)
    signals: list[str] = Field(default_factory=list)
    model_version: str = "lead-scoring-v3"


# --- Reasoning verdicts (structured outputs) --------------------------------------


class QualityVerdict(BaseModel):
    """Is there enough here to act on, and does it read as a buying signal?"""

    has_enough_info: bool
    buying_intent: str = ""  # a short phrase: what the prospect appears to want
    missing_info: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


class RoutingPlan(BaseModel):
    """The routing write-up for a lead already tiered by score."""

    owner: str = ""        # AE name for sales, campaign name for marketing/nurture
    crm_note: str = ""     # what gets written to the CRM
    talking_points: list[str] = Field(default_factory=list)
    rationale: str = ""


class LeadReviewDecision(BaseModel):
    """The human lead-review verdict, delivered to Fully Orchestrated via a Temporal
    **Update** (not a signal): the update validator can reject a malformed or unexpected
    decision synchronously, so the operator gets real feedback in the UI."""

    # True = the human agrees with the low score and the lead is archived.
    # False = the human overrides: it IS a real lead, and `route_to` says where it goes.
    confirm_archive: bool
    # Required when overriding: "sales" | "marketing" | "nurture". The reviewer picks the
    # destination rather than everything defaulting to sales.
    route_to: Optional[str] = None
    reviewer: str = "operator"
    note: str = ""


class EmailVerification(BaseModel):
    """Result of the (deliberately slow) third-party email-verification call."""

    email: str
    deliverable: bool = True
    result: str = "deliverable"
    provider_latency_seconds: float = 0.0


# --- Token accounting -------------------------------------------------------------


class TokenBuckets(BaseModel):
    """New + Wasted = Total. `estimated` is true if any contribution came from the
    tiktoken fallback rather than real provider usage."""

    new: int = 0
    wasted: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.new + self.wasted

    def to_api(self) -> dict:
        return {
            "new": self.new,
            "wasted": self.wasted,
            "total": self.total,
            "estimated": self.estimated,
        }


# --- Outcome ----------------------------------------------------------------------


class IntakeOutcome(BaseModel):
    lead_id: str
    action: Action
    summary: str
    score: Optional[int] = None
    owner: Optional[str] = None          # AE or campaign the lead was routed to
    matched_contact: Optional[str] = None  # existing CRM contact, when one was found
    tokens: TokenBuckets = Field(default_factory=TokenBuckets)
    stage: str = "done"
    # Which intake path was taken: sales | marketing | nurture | archive | needs_info.
    branch: str = ""
    # Branch-specific detail for the outcome card / Slack body.
    detail: str = ""
    # True when a human overrode the score at the review gate (Fully Orchestrated).
    human_override: bool = False
    # What the score should READ as after an override — midway into the band the reviewer
    # chose. `score` above keeps the model's original number, so the durable record still
    # shows what the scorer said and that a person overruled it.
    override_score: Optional[int] = None

    def to_api(self) -> dict:
        return {
            "action": self.action,
            "summary": self.summary,
            "score": self.score,
            "owner": self.owner,
            "matched_contact": self.matched_contact,
            "tokens": self.tokens.to_api(),
            "branch": self.branch,
            "detail": self.detail,
            "human_override": self.human_override,
            "override_score": self.override_score,
        }
