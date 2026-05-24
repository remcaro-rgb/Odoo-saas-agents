"""Unit tests for the Tier 4 reproducer (preflight + agentlab dispatch)."""

from __future__ import annotations

from agents.spec_generator.intake import Intake
from agents.spec_generator.repro import (
    FakeAgentlabClient,
    Reproducer,
    ReproOutcome,
    ReproResult,
    classify_repro_readiness,
)


def _intake(**kw) -> Intake:
    base = dict(
        issue=42,
        title="PDF export broken",
        body=(
            "Repro:\n"
            "1. Click Print on /sale/orders/1\n"
            "2. Choose PDF\n\n"
            "Expected: PDF downloads\n"
            "Actual: 500 page"
        ),
        reporter="alice",
        language="en",
    )
    base.update(kw)
    return Intake(**base)


def test_preflight_passes_when_body_has_repro_expected_actual():
    early, qs = classify_repro_readiness(_intake())
    assert early is None
    assert qs == []


def test_preflight_routes_missing_steps_to_needs_info():
    early, qs = classify_repro_readiness(
        _intake(body="It just doesn't work, please fix.")
    )
    assert early is ReproOutcome.NEEDS_REPRO_INFO
    assert qs  # at least one targeted question


def test_preflight_routes_customer_data_references_to_needs_fixture():
    early, qs = classify_repro_readiness(
        _intake(body="Repro: see our prod database, tenant id 17.")
    )
    assert early is ReproOutcome.NEEDS_FIXTURE
    assert qs == []  # the bot doesn't ask questions for this route


def test_reproducer_delegates_to_agentlab_when_preflight_clear():
    fake = FakeAgentlabClient()
    rep = Reproducer(fake)
    result = rep.attempt(_intake())
    assert result.outcome is ReproOutcome.REPRO_CONFIRMED
    assert fake.calls == [42]


def test_reproducer_short_circuits_on_needs_repro_info():
    fake = FakeAgentlabClient()
    rep = Reproducer(fake)
    result = rep.attempt(_intake(body="halp"))
    assert result.outcome is ReproOutcome.NEEDS_REPRO_INFO
    # Playwright was NOT called — the preflight saved a slow run.
    assert fake.calls == []


def test_reproducer_short_circuits_on_needs_fixture():
    fake = FakeAgentlabClient()
    rep = Reproducer(fake)
    result = rep.attempt(_intake(body="see customer data in prod db"))
    assert result.outcome is ReproOutcome.NEEDS_FIXTURE
    assert fake.calls == []


def test_agentlab_outcome_propagates_screenshots_and_logs():
    fake = FakeAgentlabClient(
        outcome_map={
            42: ReproResult(
                outcome=ReproOutcome.REPRO_CONFIRMED,
                summary="exported PDF returned HTTP 500",
                logs="ERROR: KeyError: 'invoice_lines'",
                screenshots=("https://uploader/shot1.png",),
            )
        }
    )
    result = Reproducer(fake).attempt(_intake())
    assert result.summary.startswith("exported PDF")
    assert "KeyError" in result.logs
    assert result.screenshots == ("https://uploader/shot1.png",)
