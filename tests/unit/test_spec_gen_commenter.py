"""Unit tests for the Spec Generator commenter (rendered bot voice)."""

from __future__ import annotations

from agents.spec_generator.commenter import (
    AGENT_MARKER,
    awaiting_reporter_confirm,
    low_confidence_notice,
    sensitive_escalation_notice,
    spec_drafted,
)


def test_every_comment_carries_agent_marker():
    bodies = [
        spec_drafted(
            spec_path="docs/specs/x.md",
            pr_number=42,
            captured_items=["a", "b"],
            open_questions=[],
        ),
        awaiting_reporter_confirm(
            captured_items=["c"], open_questions=["q?"]
        ),
        sensitive_escalation_notice(["sensitive:password"]),
        low_confidence_notice("feature", 0.4, ["fallback:no-signals"]),
    ]
    for body in bodies:
        assert AGENT_MARKER in body


def test_spec_drafted_includes_captured_items_and_pr_number():
    body = spec_drafted(
        spec_path="docs/specs/spec-0012-csv-export-design.md",
        pr_number=15,
        captured_items=["issue #12 — CSV export", "2 attachments"],
        open_questions=[],
    )
    assert "#15" in body
    assert "issue #12 — CSV export" in body
    assert "/confirm" in body
    assert "automatically" in body  # the 24h auto-confirm hint


def test_spec_drafted_lists_open_questions_when_present():
    body = spec_drafted(
        spec_path="x.md",
        pr_number=None,
        captured_items=["a"],
        open_questions=["which timezone for the export?", "include archived?"],
    )
    assert "which timezone for the export?" in body
    assert "include archived?" in body
    # No auto-confirm hint when there are open questions waiting.
    assert "24 hours" not in body


def test_spec_drafted_without_pr_uses_spec_path_label():
    body = spec_drafted(
        spec_path="docs/specs/x.md", pr_number=None,
        captured_items=[], open_questions=[],
    )
    assert "Spec path" in body


def test_sensitive_escalation_does_not_echo_signals_verbatim():
    body = sensitive_escalation_notice(
        ["sensitive:AKIAIOSFODNN7EXAMPLE", "sensitive:password"]
    )
    # The bot must NOT echo the matched secret token back into the comment.
    assert "AKIAIOSFODNN7EXAMPLE" not in body
    # But it should mention that *categories* were detected so a reviewer
    # knows what to look for.
    assert "sensitive" in body


def test_low_confidence_notice_mentions_reclassify_path():
    body = low_confidence_notice("feature", 0.35, ["fallback:no-signals"])
    assert "/reclassify" in body
    assert "0.35" in body
