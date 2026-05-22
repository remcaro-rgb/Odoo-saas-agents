"""Unit tests for the GitHub webhook -> Event adapter (Phase D)."""

from agents.implementation.events import EventType
from agents.implementation.github_adapter import (
    INTENT_CONFIRMED_LABEL,
    event_from_webhook,
)

_ISSUE_COMMENT = {
    "action": "created",
    "issue": {"number": 42, "pull_request": {"url": "https://api.github.com/pr/42"}},
    "comment": {"body": "Please rename the field", "user": {"login": "reporter-alice"}},
}

_PR_LABELED = {
    "action": "labeled",
    "label": {"name": INTENT_CONFIRMED_LABEL},
    "pull_request": {"number": 17, "head": {"ref": "agent/spec-1500"}},
    "sender": {"login": "maintainer-bob"},
}


def test_issue_comment_on_a_pr_maps_to_an_issue_comment_event():
    event = event_from_webhook("issue_comment", _ISSUE_COMMENT)
    assert event is not None
    assert event.type is EventType.ISSUE_COMMENT
    assert event.pr == 42
    assert event.comment == "Please rename the field"
    assert event.actor == "reporter-alice"


def test_issue_comment_on_a_plain_issue_is_ignored():
    payload = {"action": "created", "issue": {"number": 9}, "comment": {"body": "hi"}}
    assert event_from_webhook("issue_comment", payload) is None


def test_a_non_created_issue_comment_is_ignored():
    edited = {**_ISSUE_COMMENT, "action": "edited"}
    assert event_from_webhook("issue_comment", edited) is None


def test_pull_request_labeled_intent_confirmed_maps_to_intent_confirmed():
    event = event_from_webhook("pull_request", _PR_LABELED)
    assert event is not None
    assert event.type is EventType.INTENT_CONFIRMED
    assert event.pr == 17
    assert event.branch == "agent/spec-1500"


def test_pull_request_labeled_with_another_label_is_ignored():
    other = {**_PR_LABELED, "label": {"name": "needs-review"}}
    assert event_from_webhook("pull_request", other) is None


def test_an_unrecognized_webhook_is_ignored():
    assert event_from_webhook("star", {"action": "created"}) is None


_PUSH = {
    "ref": "refs/heads/agent/spec-1500",
    "after": "abc123def456789",
    "sender": {"login": "lead-dev"},
    "pusher": {"name": "lead-dev"},
}


def test_push_to_an_agent_branch_by_a_human_maps_to_human_push():
    event = event_from_webhook("push", _PUSH)
    assert event is not None
    assert event.type is EventType.HUMAN_PUSH
    assert event.branch == "agent/spec-1500"
    assert event.actor == "lead-dev"


def test_push_by_the_implementation_bot_is_ignored():
    """The agent's own pushes are not human commits."""
    bot = {**_PUSH, "sender": {"login": "implementation-bot"}}
    assert event_from_webhook("push", bot) is None


def test_push_to_a_non_agent_branch_is_ignored():
    main = {**_PUSH, "ref": "refs/heads/main"}
    assert event_from_webhook("push", main) is None


def test_push_with_a_tag_ref_is_ignored():
    tag = {**_PUSH, "ref": "refs/tags/v1.0"}
    assert event_from_webhook("push", tag) is None
