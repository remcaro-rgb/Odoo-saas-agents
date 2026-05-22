"""Unit tests for the bot-voice comment renderer (Phase D)."""

from agents.implementation.commenter import (
    AGENT_MARKER,
    escalation_notice,
    human_commit_ping,
    implementation_ready,
    iteration_update,
)


def test_iteration_update_includes_the_summary():
    out = iteration_update("Added an Internal Notes field to res.partner")
    assert "Added an Internal Notes field to res.partner" in out
    assert AGENT_MARKER in out


def test_iteration_update_includes_the_preview_url_when_given():
    out = iteration_update("did the thing", preview_url="https://pr-42.example.dev")
    assert "https://pr-42.example.dev" in out


def test_iteration_update_omits_the_preview_line_without_a_url():
    out = iteration_update("did the thing")
    assert "preview" not in out.lower()


def test_escalation_notice_includes_reason_and_details():
    out = escalation_notice(
        "spec-refinement-needed", "the /analyze report flagged a contradiction"
    )
    assert "spec-refinement-needed" in out
    assert "contradiction" in out
    assert AGENT_MARKER in out


def test_escalation_notice_works_without_details():
    out = escalation_notice("retry-cap-exceeded")
    assert "retry-cap-exceeded" in out
    assert AGENT_MARKER in out


def test_implementation_ready_includes_the_summary():
    out = implementation_ready("Implemented `custom-addons/equipment_checkout`.")
    assert "custom-addons/equipment_checkout" in out
    assert AGENT_MARKER in out


def test_implementation_ready_includes_the_preview_url_when_given():
    out = implementation_ready("did the thing", preview_url="https://pr-7.example.dev")
    assert "https://pr-7.example.dev" in out


def test_implementation_ready_omits_the_preview_line_without_a_url():
    out = implementation_ready("did the thing")
    assert "preview" not in out.lower()


def test_human_commit_ping_names_the_author_and_commit():
    out = human_commit_ping("abc123def456789", "lead-dev")
    assert "lead-dev" in out
    assert "abc123de" in out  # the short sha
    assert AGENT_MARKER in out


def test_human_commit_ping_asks_the_reporter_to_re_confirm():
    out = human_commit_ping("abc123def456789", "lead-dev").lower()
    assert "re-confirm" in out or "re-review" in out
