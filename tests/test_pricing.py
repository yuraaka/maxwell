import io
import re

import pytest

from maxwell.config import ProviderSettings
from maxwell.contracts import Spending, Usage
from maxwell.pricing import OPENAI_PRICING, price_usage, reported_spending, spending_summary
from maxwell.provider import ClaudeProvider, CodexProvider
from maxwell.scan_display import ScanDisplay


def test_published_prices_apply_cache_rates_without_inferring_context_from_aggregate(home):
    config = home.load().model_copy(update={"provider": ProviderSettings(model="gpt-6.1-sol")})
    provider = CodexProvider(home, config)
    for _ in range(2):
        provider.event(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 200_000,
                    "cached_input_tokens": 100_000,
                    "output_tokens": 1_000,
                },
            }
        )
    assert provider.usage.input_tokens == 400_000
    assert provider.usage.spending.micros == 440_000
    provider.event(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 300_000,
                "cached_input_tokens": 100_000,
                "cache_write_tokens": 10_000,
                "output_tokens": 1_000,
            },
        }
    )
    assert provider.usage.spending.micros == 865_000
    assert provider.usage.spending.upper_micros == 1_285_000
    assert "$1 USD" in spending_summary(provider.usage.spending)
    assert provider.usage.spending.source == OPENAI_PRICING
    assert provider.usage.cache_write_tokens == 10_000
    assert not provider.usage.spending.complete


def test_configured_rates_override_published_rates():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, reported=True, complete=True)
    settings = ProviderSettings(model="gpt-6.1-sol", input_usd_per_mtok=7, output_usd_per_mtok=21)
    value = price_usage(usage, settings)
    assert value.micros == 9_100_000
    assert value.source == "configured model tariff" and value.complete
    assert value.rates["input_usd_per_mtok"] == 7
    assert price_usage(Usage(), settings) is None
    assert price_usage(usage, ProviderSettings(model="unrecognized-model")) is None


@pytest.mark.parametrize("value", [None, "invalid", "NaN", "Infinity", -1])
def test_invalid_reported_cost_is_unknown(value):
    assert reported_spending(value) is None


def test_claude_uses_reported_cost_without_guessing_alias_prices(home):
    config = home.load()
    config = config.model_copy(
        update={
            "provider": config.provider.model_copy(
                update={"input_usd_per_mtok": None, "output_usd_per_mtok": None}
            )
        }
    )
    provider = ClaudeProvider(home, config)
    provider.event(
        {
            "type": "result",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "total_cost_usd": 0.012345,
        }
    )
    assert provider.usage.spending.micros == 12_345
    assert "Claude Code reported" in provider.usage.spending.source
    assert "API-equivalent estimate" in spending_summary(provider.usage.spending)
    assert "partial usage" in spending_summary(provider.usage.spending)


def test_progress_cost_totals_replace_execution_snapshots_and_reset_per_repository():
    stream = io.StringIO()
    display = ScanDisplay(stream)
    display({"state": "plan", "repository": "acme/repo", "stages": ["hunt", "review"]})
    display({"state": "running", "id": "hunt"})
    assert display.cost_label() == "Cost unknown"
    for amount in (1_000_000, 2_000_000, 2_000_000):
        display(
            {
                "state": "progress",
                "id": "hunt",
                "phase": "agent running",
                "usage": {"spending": Spending(micros=amount, source="test").model_dump()},
            }
        )
    assert display.cost_label() == "$2 USD est · partial"
    display(
        {
            "state": "completed",
            "id": "hunt",
            "usage": {
                "spending": Spending(micros=2_500_000, source="test", complete=True).model_dump()
            },
        }
    )
    display({"state": "running", "id": "review"})
    assert display.cost_label() == "$3 USD est · partial"
    display(
        {
            "state": "completed",
            "id": "review",
            "usage": {
                "spending": Spending(micros=1_500_000, source="test", complete=True).model_dump()
            },
        }
    )
    assert display.cost_label() == "$4 USD est"
    display({"state": "plan", "repository": "acme/other", "stages": ["hunt"]})
    assert display.cost_label() == "$0 USD est"


def test_long_cost_ranges_fit_small_terminal_without_hiding_progress(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("TERM", "xterm")
    stream = io.StringIO()
    stream.isatty = lambda: True
    display = ScanDisplay(stream)
    display({"state": "plan", "repository": "acme/repo", "stages": ["hunt"]})
    display({"state": "stage", "stage": "hunt", "status": "running"})
    display(
        {
            "state": "progress",
            "id": "hunt",
            "phase": "agent running",
            "usage": {
                "spending": Spending(
                    micros=1_000_000, upper_micros=2_000_000, source="test"
                ).model_dump()
            },
        }
    )
    lines = [re.sub(r"\033\[[0-9;]*m", "", line) for line in display.panel("0:01")]
    assert all(len(line) == 39 for line in lines)
    assert "0%" in lines[2] and "$1–$2" in lines[2]


def test_skipped_execution_without_model_call_does_not_create_unknown_cost():
    display = ScanDisplay(io.StringIO())
    display({"state": "running", "id": "hunt", "usage": None})
    display({"state": "skipped", "id": "hunt", "executed": 0, "usage": {}})
    assert display.cost_label() == "$0 USD est"
