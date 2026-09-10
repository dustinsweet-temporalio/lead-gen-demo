"""Token capture, the mandatory tiktoken fallback, and the New/Wasted bucket math.

CLAUDE.md rule 3: the meter must NEVER silently read zero. We prefer real usage from each
model call (enabled via ``ModelSettings(include_usage=True)`` on every agent, and read off
``response.usage`` for direct chat-completions) and fall back to a tiktoken estimate over
prompt+completion text when a provider doesn't report usage. Estimated contributions flag
the bucket so the UI can show a subtle "~".
"""

from __future__ import annotations

from typing import Any

import tiktoken

# A single stable encoding is fine for an *estimate* — we are not matching Anthropic's
# tokenizer exactly, only guaranteeing a non-zero, proportional number on stage.
_ENC = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(*texts: str) -> int:
    """Rough token count over arbitrary text, used only when real usage is missing."""
    total = 0
    for t in texts:
        if t:
            total += len(_ENC.encode(t))
    return total


def usage_tokens(run_result: Any) -> int | None:
    """Extract total tokens from an Agents SDK RunResult, or None if unavailable.

    Path verified against openai-agents 0.17.x: ``result.context_wrapper.usage`` carries
    ``input_tokens`` / ``output_tokens`` / ``total_tokens``. Some runs leave these at zero
    unless ``include_usage`` is set on the model settings; we treat zero as "missing" so
    the estimator engages.
    """
    try:
        usage = run_result.context_wrapper.usage
    except AttributeError:
        return None
    total = getattr(usage, "total_tokens", 0) or 0
    if not total:
        # Fall back to summing the parts in case total wasn't populated.
        total = (getattr(usage, "input_tokens", 0) or 0) + (
            getattr(usage, "output_tokens", 0) or 0
        )
    return total or None


class TokenAccumulator:
    """Accumulates tokens for the *current attempt* of a unit of work.

    In V1 the single agent activity owns one accumulator for its whole run and heartbeats
    ``current_total()`` continuously. If the attempt completes, the workflow rolls the
    total into New; if it is killed, the last heartbeated total becomes Wasted.
    """

    def __init__(self) -> None:
        self._total = 0
        self._estimated = False

    def add_usage(self, run_result: Any, *, prompt: str = "", completion: str = "") -> int:
        """Add one model call's tokens. Prefer real usage; estimate if missing.

        Returns the number of tokens added (useful for per-step reporting).
        """
        real = usage_tokens(run_result)
        if real is not None:
            self._total += real
            return real
        # Fallback path — flag as estimated and never return zero for a real call.
        est = estimate_tokens(prompt, completion) or 1
        self._total += est
        self._estimated = True
        return est

    def add_raw(self, tokens: int, *, estimated: bool = False) -> None:
        self._total += max(0, tokens)
        if estimated:
            self._estimated = True

    def current_total(self) -> int:
        return self._total

    @property
    def estimated(self) -> bool:
        return self._estimated
