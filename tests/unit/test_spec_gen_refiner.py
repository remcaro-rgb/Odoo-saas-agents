"""Unit tests for the Spec Generator Refiner (Tier 2 reporter Q&A loop)."""

from __future__ import annotations

from agents.spec_generator.refiner import (
    AWAITING_CONFIRM_LABEL,
    AWAITING_RECONFIRM_LABEL,
    INTENT_CONFIRMED_LABEL,
    CommentIntent,
    HeuristicCommentClassifier,
    Refiner,
)
from agents.spec_generator.speckit_driver import SpecKitFrontDriver


class _FakeClient:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def run_command(self, session_id, command, arguments="", *, model=None):
        self.calls.append({"command": command, "arguments": arguments})
        return {"parts": [{"type": "text", "text": self.reply}]}


def _refiner(reply: str = "") -> Refiner:
    return Refiner(driver=SpecKitFrontDriver(_FakeClient(reply)))


def test_slash_confirm_classifies_as_confirm():
    assert HeuristicCommentClassifier().classify("/confirm") is CommentIntent.CONFIRM


def test_lgtm_classifies_as_approval():
    assert HeuristicCommentClassifier().classify("LGTM, ship it") is CommentIntent.APPROVAL


def test_reclassify_bug_recognized():
    assert (
        HeuristicCommentClassifier().classify("Actually this is a bug — /reclassify bug")
        is CommentIntent.RECLASSIFY_BUG
    )


def test_reclassify_feature_recognized():
    assert (
        HeuristicCommentClassifier().classify("/reclassify feature please")
        is CommentIntent.RECLASSIFY_FEATURE
    )


def test_freetext_classifies_as_clarify():
    assert (
        HeuristicCommentClassifier().classify("Yes, please use UTC timezone.")
        is CommentIntent.CLARIFY
    )


def test_empty_comment_is_noise():
    assert HeuristicCommentClassifier().classify("   ") is CommentIntent.NOISE


def test_confirm_applies_intent_confirmed_label_and_removes_awaiting():
    outcome = _refiner().apply(comment="/confirm", session_id="sess-1")
    assert outcome.status == "handed_off"
    assert any(name == INTENT_CONFIRMED_LABEL for _, name in outcome.labels)
    assert any(
        name == AWAITING_CONFIRM_LABEL for _, name in outcome.labels_to_remove
    )
    assert outcome.comments  # the bot still posts a reply


def test_approval_alias_also_hands_off():
    outcome = _refiner().apply(comment="lgtm", session_id="sess-1")
    assert outcome.status == "handed_off"


def test_reclassify_bug_returns_reclassified_status_and_kind():
    outcome = _refiner().apply(comment="/reclassify bug", session_id="sess-1")
    assert outcome.status == "reclassified"
    assert outcome.new_kind == "bug"


def test_clarify_runs_speckit_clarify_and_emits_update_comment():
    refiner = Refiner(
        driver=SpecKitFrontDriver(
            _FakeClient(
                reply="# CSV export\n- now with UTC tz\n- additional capture",
            )
        )
    )
    outcome = refiner.apply(
        comment="Use UTC for the timezone.", session_id="sess-1"
    )
    assert outcome.status == "clarified"
    # Spec text propagated.
    assert "UTC" in outcome.spec_text
    # Captured items extracted from the bullet list.
    assert any("UTC tz" in item for item in outcome.captured_items)
    # Awaiting-confirm label is restored (no open questions left).
    assert any(name == AWAITING_CONFIRM_LABEL for _, name in outcome.labels)


def test_clarify_with_open_questions_marks_reconfirm():
    refiner = Refiner(
        driver=SpecKitFrontDriver(
            _FakeClient(
                reply=(
                    "# Spec\n- captured\n"
                    "- [NEEDS CLARIFICATION: still need archive flag answer]"
                ),
            )
        )
    )
    outcome = refiner.apply(comment="Use UTC.", session_id="sess-1")
    assert outcome.status == "clarified"
    assert outcome.open_questions == ("still need archive flag answer",)
    assert any(
        name == AWAITING_RECONFIRM_LABEL for _, name in outcome.labels
    )
    assert any(
        name == AWAITING_CONFIRM_LABEL for _, name in outcome.labels_to_remove
    )


def test_clarify_with_empty_reply_escalates_to_human():
    outcome = _refiner(reply="").apply(comment="Use UTC.", session_id="s")
    assert outcome.status == "ignored"
    assert any("needs-human" == name for _, name in outcome.labels)


def test_noise_comment_is_a_no_op():
    outcome = _refiner().apply(comment="   ", session_id="s")
    assert outcome.status == "ignored"
    assert outcome.comments == []
    assert outcome.labels == []


def test_driver_exception_escalates_cleanly():
    class _BoomClient:
        def run_command(self, *a, **k):
            raise RuntimeError("opencode down")

    refiner = Refiner(driver=SpecKitFrontDriver(_BoomClient()))
    outcome = refiner.apply(comment="Use UTC.", session_id="s")
    assert outcome.status == "ignored"
    assert any("needs-human" == name for _, name in outcome.labels)
