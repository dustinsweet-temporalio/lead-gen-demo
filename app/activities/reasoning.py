"""Fully Orchestrated reasoning steps as plain model-call activities — NO Agents SDK.

The workflow owns the flow and calls these two model activities where judgement is
genuinely needed. Each calls the model through ``AsyncOpenAI`` (JSON mode) and returns a
plain dict (so it round-trips through the pydantic data converter) plus the token usage,
which the workflow banks into durable state.

Note what is NOT here: nothing decides the route. The score comes from the deterministic
scoring service and the WORKFLOW applies the thresholds (CLAUDE.md rule 4) — which is also
why the human-review beat fires reliably every run.
"""

from __future__ import annotations

import asyncio
import json

from openai import AsyncOpenAI
from temporalio import activity
from temporalio.exceptions import ApplicationError

import app.config as config
from app.shared import prompts
from app.shared.models import Lead
from app.shared.tokens import estimate_tokens


async def _heartbeat_loop() -> None:
    """Heartbeat while a (possibly long) model call runs so a worker crash is detected at
    the activity's heartbeat_timeout (~2.5s) rather than waiting out start_to_close."""
    hb = activity.info().heartbeat_timeout
    if not hb:
        return
    interval = max(0.5, hb.total_seconds() / 2)
    while True:
        await asyncio.sleep(interval)
        activity.heartbeat()


async def _chat(system: str, user: str, max_tokens: int) -> tuple[dict, int, bool]:
    """One JSON-mode model call; returns (parsed_json, tokens, estimated)."""
    key = config.require_api_key()
    cfg = config.get_config()
    client = AsyncOpenAI(api_key=key, max_retries=0)  # Temporal owns retries
    hb_task = asyncio.create_task(_heartbeat_loop())
    try:
        resp = await client.chat.completions.create(
            model=cfg.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_completion_tokens=max_tokens,
        )
    finally:
        hb_task.cancel()

    content = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        # Truncated/malformed JSON won't improve on retry with the same cap — non-retryable.
        raise ApplicationError(f"model returned malformed JSON: {exc}", non_retryable=True) from exc

    usage = getattr(resp, "usage", None)
    tokens = (getattr(usage, "total_tokens", 0) or 0) if usage else 0
    estimated = False
    if not tokens:  # never let the meter read zero (CLAUDE.md rule 3)
        tokens = estimate_tokens(system, user, content) or 1
        estimated = True
    return data, tokens, estimated


@activity.defn
async def assess_lead_quality(lead: Lead, contacts: list[dict]) -> dict:
    """Judge whether the lead has enough information to act on, and what it wants."""
    data, tokens, estimated = await _chat(
        prompts.QUALITY_INSTRUCTIONS
        + "\nRespond with JSON only:\n"
        '{"has_enough_info": true, "buying_intent": "...", "missing_info": [], '
        '"confidence": 0.0, "rationale": "..."}',
        prompts.render_lead(lead) + "\n" + prompts.format_contacts(contacts),
        max_tokens=400,
    )
    return {
        "has_enough_info": bool(data.get("has_enough_info", False)),
        "buying_intent": str(data.get("buying_intent", "")),
        "missing_info": list(data.get("missing_info", [])),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "rationale": str(data.get("rationale", "")),
        "tokens": tokens,
        "estimated": estimated,
    }


@activity.defn
async def draft_routing_plan(
    lead: Lead,
    destination: str,
    owner: str,
    score: dict,
    enrichment: dict,
    verification: dict,
) -> dict:
    """Write the CRM note and talking points for a lead already tiered by score.

    The destination is an INPUT, not an output: the workflow decided it from the score.
    """
    data, tokens, estimated = await _chat(
        prompts.ROUTING_INSTRUCTIONS
        + "\nRespond with JSON only:\n"
        '{"owner": "...", "crm_note": "...", "talking_points": ["..."], "rationale": "..."}',
        prompts.render_lead(lead)
        + "\n"
        + prompts.format_score(score)
        + "\n"
        + prompts.format_enrichment(enrichment)
        + "\n"
        + prompts.format_verification(verification)
        + f"\nDecided destination: {destination.upper()}\nOwner to use: {owner}\n",
        max_tokens=600,
    )
    return {
        "owner": str(data.get("owner") or owner),
        "crm_note": str(data.get("crm_note", "")),
        "talking_points": list(data.get("talking_points", [])),
        "rationale": str(data.get("rationale", "")),
        "tokens": tokens,
        "estimated": estimated,
    }


REASONING_ACTIVITIES = [assess_lead_quality, draft_routing_plan]
