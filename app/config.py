"""Central configuration + the tracing-disable helper.

Every entrypoint that constructs an agent (worker, web server, smoke test, the Agent-Only
runner) must call :func:`disable_agent_tracing` at process startup. We do not export
traces during the demo — it adds network calls and noise to a live run (CLAUDE.md rule 2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

# Load .env once, at import time, so every process picks up OPENAI_API_KEY etc.
load_dotenv()

# Variant slugs. `agent_only` uses no Temporal; the other three each get their own task
# queue so filtering the Temporal UI by queue during the demo shows exactly one variant.
AGENT_ONLY = "agent_only"
AGENT = "agent"
AGENT_STEPS = "agent_steps"
ORCHESTRATED = "orchestrated"

WORKER_VARIANTS = (AGENT, AGENT_STEPS, ORCHESTRATED)
ALL_VARIANTS = (AGENT_ONLY, *WORKER_VARIANTS)

# Slug -> task-queue suffix (queue is f"{task_queue_prefix}-{suffix}").
_QUEUE_SUFFIX = {
    AGENT: "agent",
    AGENT_STEPS: "agent-steps",
    ORCHESTRATED: "orchestrated",
}


def variant_suffix(variant: str) -> str:
    """Short, greppable variant tag used in task queues and workflow ids."""
    return _QUEUE_SUFFIX.get(variant, variant.replace("_", "-"))


# Slug -> registered workflow type name. These names are read aloud off the Temporal UI
# during the presentation, so they name the business process first and the variant second.
WORKFLOW_TYPE = {
    AGENT: "LeadIntakeAgentWorkflow",
    AGENT_STEPS: "LeadIntakeAgentStepsWorkflow",
    ORCHESTRATED: "LeadIntakeOrchestratedWorkflow",
}


@dataclass(frozen=True)
class Config:
    openai_api_key: str | None
    model: str
    temporal_address: str
    temporal_namespace: str
    temporal_ui: str
    task_queue_prefix: str
    web_port: int
    heartbeat_timeout_seconds: float
    heartbeat_interval_seconds: float
    # The human lead-review window for the Fully Orchestrated gate (seconds). Deliberately
    # long — the point on stage is that a workflow can park for a week for effectively
    # nothing. It exists only so an unattended run eventually escalates rather than
    # hanging forever.
    review_timeout_seconds: float
    # --- Lead scoring thresholds (shown in the UI so the audience sees why a lead routed
    #     the way it did). score >= sales -> sales; >= marketing -> marketing;
    #     >= qualify -> nurture; below qualify -> not worth pursuing (archive), which is
    #     the branch that triggers human review in Fully Orchestrated.
    sales_threshold: int
    marketing_threshold: int
    qualify_threshold: int
    # The seeded lead that deterministically scores below `qualify_threshold`, i.e. the
    # one that drives the human-review beat. Default LEAD-4105 (a separate, obviously-real
    # prospect, so the talk track for the earlier variants stays true). Set this to
    # LEAD-4102 for the tighter "the same demo lead gets mishandled, then caught" arc.
    escalation_lead_id: str
    # Simulated latency of the third-party email-verification provider. Short by default
    # so the stage runtime stays tight; raise it (e.g. 1800) to demonstrate a workflow
    # that just waits an hour mid-run. In Fully Orchestrated the wait is a DURABLE
    # workflow timer, so it survives a worker crash.
    email_verify_delay_seconds: float
    # The "Time Spend" figures in the UI are the run's elapsed time multiplied by this,
    # to stand in for a real pipeline whose elapsed time is dominated by third-party
    # latency rather than compute. 20x makes a 30-second demo run read as the ~10 minutes
    # one lead would really take end to end against live vendors.
    #
    # It is DERIVED AND DISPLAY-ONLY on purpose: nothing simulated is written to the
    # durable record, so the workflow history and the token figures stay measured.
    time_multiplier: float

    def task_queue(self, variant: str) -> str:
        """Per-variant task queue, so the Temporal UI filters cleanly per variant."""
        suffix = _QUEUE_SUFFIX.get(variant)
        if not suffix:
            raise ValueError(f"{variant!r} has no task queue (agent_only uses no Temporal)")
        return f"{self.task_queue_prefix}-{suffix}"

    @property
    def all_task_queues(self) -> list[str]:
        return [self.task_queue(v) for v in WORKER_VARIANTS]


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config(
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        # OpenAI model id — no provider prefix. Read from env, sensible default.
        model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        temporal_ui=os.getenv("TEMPORAL_UI", "http://localhost:8233"),
        task_queue_prefix=os.getenv("TASK_QUEUE_PREFIX", "lead-intake"),
        web_port=int(os.getenv("WEB_PORT", "8000")),
        heartbeat_timeout_seconds=float(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "2.5")),
        heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "1.0")),
        review_timeout_seconds=float(os.getenv("REVIEW_TIMEOUT_SECONDS", "604800")),  # 7 days
        sales_threshold=int(os.getenv("SALES_THRESHOLD", "75")),
        marketing_threshold=int(os.getenv("MARKETING_THRESHOLD", "50")),
        qualify_threshold=int(os.getenv("QUALIFY_THRESHOLD", "25")),
        escalation_lead_id=os.getenv("ESCALATION_LEAD_ID", "LEAD-4105").upper(),
        email_verify_delay_seconds=float(os.getenv("EMAIL_VERIFY_DELAY_SECONDS", "3")),
        time_multiplier=float(os.getenv("TIME_MULTIPLIER", "20")),
    )


# --- Who a routed lead goes to ----------------------------------------------------
# Territory owners by enriched industry, so "routed to sales" names a real-looking human
# on stage. Pure data + pure lookup, so the workflow can decide the owner itself.

AE_BY_INDUSTRY = {
    "Retail": "ae.tan",
    "Transportation & logistics": "ae.okafor",
    "Healthcare": "ae.okafor",
    "Software": "ae.rivera",
    "Professional services": "ae.rivera",
}
DEFAULT_AE = "ae.rivera"
MARKETING_CAMPAIGN = "mkt.product-education-q3"
NURTURE_CAMPAIGN = "nurture.long-cycle"


def owner_for(tier: str, industry: str | None = None) -> str:
    """The AE handle (sales) or campaign (marketing/nurture) a tier routes to."""
    if tier == "sales":
        return AE_BY_INDUSTRY.get(industry or "", DEFAULT_AE)
    if tier == "marketing":
        return MARKETING_CAMPAIGN
    if tier == "nurture":
        return NURTURE_CAMPAIGN
    return ""


# The scale the scoring service reports on.
MAX_SCORE = 100


def override_score_for(tier: str) -> int:
    """The score a human override should READ as, once a person has overruled the model.

    Sitting the needle midway between the chosen tier's floor and the next tier up puts it
    unambiguously inside that band — so the gauge stops showing the model's rejected
    number (which is misleading once a human has overruled it) and instead shows where the
    lead now sits. Rounded half-up so it never displays a fraction.
    """
    cfg = get_config()
    floors = {
        "sales": (cfg.sales_threshold, MAX_SCORE),
        "marketing": (cfg.marketing_threshold, cfg.sales_threshold),
        "nurture": (cfg.qualify_threshold, cfg.marketing_threshold),
    }
    if tier not in floors:
        return 0
    floor, ceiling = floors[tier]
    return int((ceiling - floor) / 2 + floor + 0.5)


def tier_for_score(score: int) -> str:
    """Map a score to its routing tier. Pure + deterministic — safe to call in a
    workflow, which is where the branch decision belongs (CLAUDE.md rule 4)."""
    cfg = get_config()
    if score >= cfg.sales_threshold:
        return "sales"
    if score >= cfg.marketing_threshold:
        return "marketing"
    if score >= cfg.qualify_threshold:
        return "nurture"
    return "archive"


def require_api_key() -> str:
    """Return the OpenAI key or raise a clear, actionable error."""
    key = get_config().openai_api_key
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and set your "
            "OpenAI key."
        )
    return key


_tracing_disabled = False


def disable_agent_tracing() -> None:
    """Idempotently disable OpenAI Agents tracing. Call at every entrypoint.

    We don't export traces during the demo (CLAUDE.md rule 2) — it only adds network
    calls and noise to a live run.
    """
    global _tracing_disabled
    if _tracing_disabled:
        return
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    _tracing_disabled = True
