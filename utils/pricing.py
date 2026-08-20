"""Token pricing and cost calculation for Anthropic API calls.

Prices are USD per million tokens, last verified 18 August 2026 against public
pricing pages. Anthropic can change them at any time, so treat this table as a
local estimate, not as an invoice: the authoritative figure is always the
Anthropic Console usage dashboard. Override any rate from secrets.toml with

    [MODEL_PRICING]
    "claude-sonnet-5" = { input = 2.0, output = 10.0 }

Cache reads are billed at 10% of the input rate; a 5-minute cache write at
125% and a 1-hour cache write at 200%.
"""

PRICING_VERIFIED = "2026-08-18"

MODEL_PRICING = {
    "claude-opus-5":              {"input":  5.00, "output": 25.00},
    "claude-sonnet-5":            {"input":  2.00, "output": 10.00},
    "claude-fable-5":             {"input": 10.00, "output": 50.00},
    "claude-haiku-4-5-20251001":  {"input":  1.00, "output":  5.00},
    # legacy, kept so historical rows still price correctly
    "claude-sonnet-4-6":          {"input":  3.00, "output": 15.00},
    "claude-opus-4-1":            {"input": 15.00, "output": 75.00},
}

CACHE_READ_MULTIPLIER  = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

DEFAULT_PRICE = {"input": 3.00, "output": 15.00}


def rates_for(model: str, overrides: dict | None = None) -> tuple[dict, bool]:
    """Return (rates, known). `known` is False when the model is not in the table."""
    if overrides:
        for key, val in overrides.items():
            if key == model and isinstance(val, dict):
                return {"input": float(val.get("input", DEFAULT_PRICE["input"])),
                        "output": float(val.get("output", DEFAULT_PRICE["output"]))}, True
    if model in MODEL_PRICING:
        return MODEL_PRICING[model], True
    for known_model, rates in MODEL_PRICING.items():
        if model.startswith(known_model):
            return rates, True
    return DEFAULT_PRICE, False


def usage_to_dict(usage) -> dict:
    """Normalise an Anthropic usage object (or dict) into plain integers."""
    def get(name):
        if usage is None:
            return 0
        if isinstance(usage, dict):
            return int(usage.get(name) or 0)
        return int(getattr(usage, name, 0) or 0)
    return {
        "input_tokens":  get("input_tokens"),
        "output_tokens": get("output_tokens"),
        "cache_read":    get("cache_read_input_tokens"),
        "cache_write":   get("cache_creation_input_tokens"),
    }


def cost_usd(model: str, tokens: dict, overrides: dict | None = None) -> tuple[float, bool]:
    """Cost of one call in USD, plus whether the model's rate was known."""
    rates, known = rates_for(model, overrides)
    m = 1_000_000
    total = (
        tokens.get("input_tokens", 0)  * rates["input"]  / m
        + tokens.get("output_tokens", 0) * rates["output"] / m
        + tokens.get("cache_read", 0)  * rates["input"] * CACHE_READ_MULTIPLIER  / m
        + tokens.get("cache_write", 0) * rates["input"] * CACHE_WRITE_MULTIPLIER / m
    )
    return round(total, 6), known


def summarize_events(events) -> dict:
    """Aggregate a list of usage events into totals."""
    out = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "by_step": {}, "by_model": {}}
    for e in events or []:
        out["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
            out[k] += int(e.get(k) or 0)
        out["cost_usd"] += float(e.get("cost_usd") or 0)
        for key, bucket in (("step", "by_step"), ("model", "by_model")):
            name = e.get(key) or "—"
            b = out[bucket].setdefault(name, {"calls": 0, "cost_usd": 0.0, "tokens": 0})
            b["calls"] += 1
            b["cost_usd"] += float(e.get("cost_usd") or 0)
            b["tokens"] += int(e.get("input_tokens") or 0) + int(e.get("output_tokens") or 0)
    out["cost_usd"] = round(out["cost_usd"], 6)
    for bucket in ("by_step", "by_model"):
        for b in out[bucket].values():
            b["cost_usd"] = round(b["cost_usd"], 6)
    return out


def fmt_usd(amount) -> str:
    a = float(amount or 0)
    return f"${a:,.4f}" if a < 1 else f"${a:,.2f}"


def fmt_eur(amount_usd, usd_per_eur: float | None) -> str:
    if not usd_per_eur:
        return ""
    a = float(amount_usd or 0) / float(usd_per_eur)
    return f"€{a:,.4f}" if a < 1 else f"€{a:,.2f}"
