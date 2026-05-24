"""Unit tests for the HeuristicClassifier."""

from __future__ import annotations

from agents.spec_generator.classifier import (
    Classifier,
    HeuristicClassifier,
    IntakeKind,
    KindResult,
)
from agents.spec_generator.intake import Intake


def _intake(**kw: object) -> Intake:
    base = dict(
        issue=1,
        title="title",
        body="body",
        reporter="alice",
        language="en",
    )
    base.update(kw)
    return Intake(**base)  # type: ignore[arg-type]


def test_protocol_compliance():
    assert isinstance(HeuristicClassifier(), Classifier)


def test_label_hint_feature_request_wins():
    result = HeuristicClassifier().classify(
        _intake(kind_hint="feature-request", body="text")
    )
    assert result.kind is IntakeKind.FEATURE
    assert result.confidence >= 0.85


def test_label_hint_bug_wins():
    result = HeuristicClassifier().classify(
        _intake(kind_hint="bug", body="text")
    )
    assert result.kind is IntakeKind.BUG


def test_live_labels_break_ties_when_no_event_hint():
    result = HeuristicClassifier().classify(
        _intake(labels=("bug",), body="something")
    )
    assert result.kind is IntakeKind.BUG


def test_bug_keywords_route_without_label():
    result = HeuristicClassifier().classify(
        _intake(body="The PDF export crashes with a 500 error.")
    )
    assert result.kind is IntakeKind.BUG


def test_feature_keywords_route_without_label():
    result = HeuristicClassifier().classify(
        _intake(body="Please add a download-as-CSV button.")
    )
    assert result.kind is IntakeKind.FEATURE


def test_config_question_routes_to_config():
    result = HeuristicClassifier().classify(
        _intake(body="How do I configure the SMTP server for this tenant?")
    )
    assert result.kind is IntakeKind.CONFIG


def test_no_signals_falls_back_to_feature_low_confidence():
    result = HeuristicClassifier().classify(_intake(body="Hi everyone."))
    assert result.kind is IntakeKind.FEATURE
    assert result.confidence < 0.5


def test_sensitive_content_beats_a_feature_label():
    result = HeuristicClassifier().classify(
        _intake(
            kind_hint="feature-request",
            body="Here is my AWS key AKIAIOSFODNN7EXAMPLE so you can repro.",
        )
    )
    assert result.kind is IntakeKind.SENSITIVE
    assert any("sensitive:" in s for s in result.signals)


def test_sensitive_content_in_title_or_body_triggers_match():
    result = HeuristicClassifier().classify(
        _intake(
            title="Password broken",
            body="I want to reset my password but the button is missing.",
        )
    )
    # "password" alone is enough to trip the detector — that's defence in
    # depth at the cost of some false-positives; the comment is conservative
    # ("looks like it may contain sensitive content").
    assert result.kind is IntakeKind.SENSITIVE


def test_kind_result_dataclass_defaults():
    assert KindResult(kind=IntakeKind.FEATURE, confidence=0.9).signals == ()
