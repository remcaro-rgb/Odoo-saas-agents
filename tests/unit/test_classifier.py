"""Unit tests for the reporter-comment classifier (Phase D)."""

from agents.implementation.classifier import (
    Classifier,
    CommentIntent,
    HeuristicClassifier,
)


def _classify(comment: str) -> CommentIntent:
    return HeuristicClassifier().classify(comment)


def test_heuristic_classifier_satisfies_the_protocol():
    assert isinstance(HeuristicClassifier(), Classifier)


def test_approval_comment_is_classified_as_approval():
    assert _classify("LGTM, ship it") is CommentIntent.APPROVAL


def test_a_plain_change_request_is_classified_as_change_request():
    assert (
        _classify("Please rename the field to internal_note")
        is CommentIntent.CHANGE_REQUEST
    )


def test_a_question_is_classified_as_question():
    assert _classify("Why is the note on a separate tab?") is CommentIntent.QUESTION


def test_a_thankyou_is_noise():
    assert _classify("Thanks, appreciate it!") is CommentIntent.NOISE


def test_blank_comment_is_noise():
    assert _classify("   ") is CommentIntent.NOISE


def test_change_request_outranks_approval():
    """'looks good' alone is approval, but a change request anywhere wins."""
    assert (
        _classify("Looks good overall, but move the field to the top")
        is CommentIntent.CHANGE_REQUEST
    )


def test_change_request_phrased_as_a_question_is_a_change_request():
    """A polite 'can you ...' asking for a change is still a change request."""
    assert (
        _classify("Can you rename it to customer_note?")
        is CommentIntent.CHANGE_REQUEST
    )


def test_approval_that_mentions_change_is_not_a_change_request():
    """'nothing to change here' is approval — the bare word 'change' must not fire."""
    assert _classify("Nothing to change here, lgtm") is CommentIntent.APPROVAL


def test_move_on_is_not_a_change_request():
    """'move on' is not a request to move code."""
    assert _classify("Nice, let's move on to the next one") is CommentIntent.NOISE


def test_does_not_look_right_is_a_change_request():
    assert (
        _classify("This does not look right to me") is CommentIntent.CHANGE_REQUEST
    )
