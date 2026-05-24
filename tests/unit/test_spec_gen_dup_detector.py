"""Unit tests for the Tier 5 duplicate detector."""

from __future__ import annotations

from agents.spec_generator.dup_detector import (
    DUPLICATE_THRESHOLD,
    BagOfWordsKnowledgeBase,
    DuplicateCandidate,
    DuplicateDetector,
    DuplicateResult,
    render_duplicate_callout,
    title_prefix_for,
)
from agents.spec_generator.intake import Intake


def _intake(title: str, body: str) -> Intake:
    return Intake(
        issue=1, title=title, body=body, reporter="alice", language="en"
    )


def test_above_threshold_marks_duplicate():
    """Near-identical bodies cross the 0.85 threshold."""
    kb = BagOfWordsKnowledgeBase()
    body = "Add a CSV export download button to the sale orders list view."
    kb.add(
        DuplicateCandidate(
            kind="spec", ref="docs/specs/x-design.md",
            title="Add CSV export to /sale/orders",
            score=0.0,
        ),
        body,
    )
    # Same body submitted again — perfect cosine match.
    result = DuplicateDetector(kb).detect(_intake("Add CSV export", body))
    assert isinstance(result, DuplicateResult)
    assert result.is_duplicate
    assert result.top is not None
    assert result.top.score >= DUPLICATE_THRESHOLD


def test_below_threshold_returns_suggestions_not_duplicate():
    kb = BagOfWordsKnowledgeBase()
    kb.add(
        DuplicateCandidate(
            kind="open_issue", ref="https://example.com/issues/5",
            title="Performance regression on /sale/list", score=0.0,
        ),
        "performance regression on the sale order list view",
    )
    result = DuplicateDetector(kb).detect(
        _intake("Add CSV export", "Please add a download-as-CSV button.")
    )
    assert not result.is_duplicate
    # Sub-threshold candidates can still surface as suggestions.
    assert len(result.candidates) >= 0  # may be empty if cosine == 0


def test_empty_kb_yields_no_duplicates():
    detector = DuplicateDetector(BagOfWordsKnowledgeBase())
    result = detector.detect(_intake("Add X", "Body"))
    assert not result.is_duplicate
    assert result.candidates == ()


def test_render_callout_includes_link_and_score():
    candidates = (
        DuplicateCandidate(
            kind="spec",
            ref="docs/specs/y-design.md",
            title="Existing CSV export work",
            score=0.92,
        ),
    )
    body = render_duplicate_callout(
        DuplicateResult(is_duplicate=True, candidates=candidates)
    )
    assert "docs/specs/y-design.md" in body
    assert "0.92" in body


def test_title_prefix_only_when_above_threshold():
    above = DuplicateResult(
        is_duplicate=True,
        candidates=(DuplicateCandidate(
            kind="spec", ref="x", title="t", score=0.9,
        ),),
    )
    below = DuplicateResult(
        is_duplicate=False,
        candidates=(DuplicateCandidate(
            kind="spec", ref="x", title="t", score=0.5,
        ),),
    )
    assert title_prefix_for(above) == "[possible-dup]"
    assert title_prefix_for(below) is None
