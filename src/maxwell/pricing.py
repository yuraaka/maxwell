from decimal import Decimal, InvalidOperation

from maxwell.contracts import Spending, estimate_cost, micros

OPENAI_PRICING = "https://developers.openai.com/api/docs/pricing"
VERIFIED_ON = "2026-10-15"
STANDARD_RATES = {
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-6.1-sol": (2, 0.1, 2.5, 10),
    "gpt-6-sol": (2, 0.2, 2.5, 10),
    "gpt-6-luna": (0.1, 0.01, 0.125, 0.5),
    "gpt-5.6-sol": (4, 0.4, 5, 20),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
}
RATE_FIELDS = (
    "input_usd_per_mtok",
    "cached_input_usd_per_mtok",
    "cache_write_usd_per_mtok",
    "output_usd_per_mtok",
)


def price_usage(usage, settings):
    if not usage.reported:
        return None
    configured = settings.input_usd_per_mtok is not None
    if configured:
        rates = {key: getattr(settings, key) for key in RATE_FIELDS}
        rates = {key: value for key, value in rates.items() if value is not None}
        source, date = "configured model tariff", None
    elif settings.adapter == "codex" and settings.model in STANDARD_RATES:
        rates = dict(zip(RATE_FIELDS, STANDARD_RATES[settings.model], strict=True))
        source = (
            f"https://developers.openai.com/api/docs/models/{settings.model}"
            if settings.model in ("gpt-6-sol", "gpt-5.6-luna")
            else OPENAI_PRICING
        )
        date = VERIFIED_ON
    else:
        return None
    priced = settings.model_copy(update=rates)
    amount = estimate_cost(usage.model_copy(update={"complete": True}), priced)
    if amount is None:
        return None
    upper = None
    if not configured and usage.input_tokens > 272_000:
        long_rates = {
            key: value * (1.5 if key == "output_usd_per_mtok" else 2)
            for key, value in rates.items()
        }
        upper = estimate_cost(
            usage.model_copy(update={"complete": True}), settings.model_copy(update=long_rates)
        )
        rates.update({f"long_context_{key}": value for key, value in long_rates.items()})
    return Spending(
        micros=amount,
        upper_micros=upper,
        source=source,
        as_of=date,
        rates=rates,
        complete=usage.complete,
    )


def reported_spending(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        return Spending(micros=micros(amount), source="Claude Code reported API-equivalent cost")
    except (InvalidOperation, ValueError, TypeError):
        return None


def combine_spending(first, second):
    if first is None or second is None:
        known = first or second
        return known.model_copy(update={"complete": False}) if known else None
    return first.model_copy(
        update={
            "micros": first.micros + second.micros,
            "upper_micros": (
                (first.upper_micros if first.upper_micros is not None else first.micros)
                + (second.upper_micros if second.upper_micros is not None else second.micros)
                if first.upper_micros is not None or second.upper_micros is not None
                else None
            ),
            "complete": first.complete and second.complete,
            "rates": first.rates if first.rates == second.rates else {},
        }
    )


def money(amount):
    return f"${Decimal(amount) / 1_000_000:,.4f}"


def cost_range(amount, upper=None):
    return money(amount) + (f"–{money(upper)}" if upper is not None and upper != amount else "")


def spending_summary(spending):
    if spending is None:
        return "Cost: unknown (usage or tariff unavailable)"
    if isinstance(spending, dict):
        spending = Spending.model_validate(spending)
    partial = "; partial usage" if not spending.complete else ""
    date = f"; verified {spending.as_of}" if spending.as_of else ""
    context = (
        " Context-tier range: CLI usage aggregates requests."
        if spending.upper_micros is not None
        else ""
    )
    return (
        f"Cost: **{cost_range(spending.micros, spending.upper_micros)} USD** (API-equivalent estimate{partial}). "
        f"Tariff: {spending.source}{date}.{context}"
    )
