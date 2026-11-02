"""Token prices used for cost estimates.

Source: Gemini Developer API pricing, https://ai.google.dev/gemini-api/docs/pricing
(page "Last updated 2026-10-30 UTC", read 2026-11-02). Standard tier, paid, USD per 1M tokens.
The pricing page lists Computer Use as "Charged as regular tokens per model pricing" on the paid
tier and "Not available" on the free tier.

Prices change. Several of these are promotional and step up on a published date (noted below).
Re-check the page before quoting a number publicly.
"""
from __future__ import annotations

from dataclasses import dataclass

PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_READ_ON = "2026-11-02"


@dataclass(frozen=True)
class Price:
    input_per_m: float
    output_per_m: float  # output price includes thinking tokens
    cached_per_m: float | None
    note: str = ""


PRICES: dict[str, Price] = {
    "gemini-3.8-flash": Price(0.75, 3.75, 0.075, "through 2026-12-31; $1.50 / $7.50 from 2027-01-01"),
    "gemini-3.5-flash-lite": Price(0.30, 2.50, 0.03, ""),
    "gemini-4-argon": Price(2.00, 10.00, 0.10, "through 2026-11-30; $4.00 / $20.00 from 2026-12-01"),
    "gemini-3-flash-preview": Price(0.50, 3.00, 0.05, ""),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int, thought_tokens: int = 0,
                  cached_tokens: int = 0) -> float | None:
    """Estimated USD cost. Returns None for a model with no price on file.

    Assumptions (documented in README): cached tokens are a subset of input tokens and billed at
    the cache rate; thinking tokens are reported separately from output tokens and billed at the
    output rate, as the pricing page says output "includes thinking tokens".
    """
    p = PRICES.get(model)
    if p is None:
        return None
    cached = min(cached_tokens, input_tokens) if p.cached_per_m is not None else 0
    fresh = input_tokens - cached
    cost = fresh * p.input_per_m + (output_tokens + thought_tokens) * p.output_per_m
    if cached:
        cost += cached * (p.cached_per_m or 0)
    return cost / 1_000_000
