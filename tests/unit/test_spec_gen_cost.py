"""Unit tests for the Tier 6 spend cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.spec_generator.cost import (
    DEFAULT_WEEKLY_CAP_USD,
    Budget,
    InMemoryCostLedger,
)


def _now() -> datetime:
    return datetime(2026, 5, 24, 12, 0, tzinfo=UTC)


def test_empty_ledger_admits_with_no_warning():
    budget = Budget(InMemoryCostLedger())
    decision = budget.decide(now=_now())
    assert decision.admit
    assert not decision.warning
    assert decision.spent_week_usd == 0.0


def test_within_cap_admits():
    ledger = InMemoryCostLedger()
    ledger.record(amount_usd=10.0, when=_now() - timedelta(hours=1))
    decision = Budget(ledger).decide(now=_now())
    assert decision.admit
    assert not decision.warning


def test_above_80_percent_admits_with_warning():
    ledger = InMemoryCostLedger()
    ledger.record(amount_usd=42.0, when=_now() - timedelta(hours=1))
    decision = Budget(ledger).decide(now=_now())
    assert decision.admit
    assert decision.warning


def test_at_or_above_cap_refuses():
    ledger = InMemoryCostLedger()
    ledger.record(amount_usd=DEFAULT_WEEKLY_CAP_USD, when=_now())
    decision = Budget(ledger).decide(now=_now())
    assert not decision.admit
    assert "cap" in decision.reason


def test_records_older_than_7_days_are_excluded():
    ledger = InMemoryCostLedger()
    # An $80 spend 8 days ago does NOT count against the rolling 7d cap.
    ledger.record(amount_usd=80.0, when=_now() - timedelta(days=8))
    decision = Budget(ledger).decide(now=_now())
    assert decision.admit
    assert decision.spent_week_usd == 0.0


def test_budget_record_writes_through_to_ledger():
    ledger = InMemoryCostLedger()
    budget = Budget(ledger)
    budget.record(amount_usd=5.0, when=_now())
    assert budget.decide(now=_now()).spent_week_usd == 5.0


def test_custom_cap_is_honoured():
    ledger = InMemoryCostLedger()
    ledger.record(amount_usd=15.0, when=_now())
    decision = Budget(ledger, weekly_cap_usd=10.0).decide(now=_now())
    assert not decision.admit
