"""Unit tests for Spec Generator events + github_adapter (Tier 1)."""

from __future__ import annotations

from agents.spec_generator.events import Event, EventType
from agents.spec_generator.github_adapter import event_from_webhook


def _issue_payload(
    action: str = "opened",
    number: int = 42,
    title: str = "Add CSV export",
    body: str = "We need a CSV export button on /sale/orders.",
    user: str = "alice",
    labels: list[str] | None = None,
    label_event: str | None = None,
) -> dict:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": user},
            "labels": [{"name": name} for name in (labels or [])],
        },
        "label": {"name": label_event} if label_event else None,
    }


def test_issues_opened_with_routing_label_picks_up_kind_hint():
    event = event_from_webhook(
        "issues", _issue_payload(labels=["feature-request", "priority/p2"])
    )
    assert event is not None
    assert event.type is EventType.ISSUE_OPENED
    assert event.issue == 42
    assert event.kind_hint == "feature-request"
    assert event.actor == "alice"


def test_issues_opened_without_routing_label_has_no_kind_hint():
    event = event_from_webhook(
        "issues", _issue_payload(labels=["priority/p1"])
    )
    assert event is not None
    assert event.kind_hint is None


def test_issues_labeled_with_routing_label_dispatches():
    event = event_from_webhook(
        "issues",
        _issue_payload(action="labeled", label_event="bug"),
    )
    assert event is not None
    assert event.type is EventType.ISSUE_LABELED
    assert event.kind_hint == "bug"


def test_issues_labeled_with_non_routing_label_is_dropped():
    assert (
        event_from_webhook(
            "issues",
            _issue_payload(action="labeled", label_event="priority/p2"),
        )
        is None
    )


def test_closed_action_is_dropped():
    assert event_from_webhook("issues", _issue_payload(action="closed")) is None


def test_pr_disguised_as_issue_is_dropped():
    payload = _issue_payload()
    payload["issue"]["pull_request"] = {"url": "https://example.com/pr/1"}
    assert event_from_webhook("issues", payload) is None


def test_issue_comment_routes_to_issue_comment_event():
    payload = {
        "action": "created",
        "issue": {"number": 7, "title": "T", "body": "B"},
        "comment": {"body": "/confirm", "user": {"login": "bob"}},
    }
    event = event_from_webhook("issue_comment", payload)
    assert event is not None
    assert event.type is EventType.ISSUE_COMMENT
    assert event.comment == "/confirm"
    assert event.pr is None  # plain-issue comment, not a PR


def test_issue_comment_on_a_pr_carries_pr_number():
    payload = {
        "action": "created",
        "issue": {"number": 9, "pull_request": {"url": "x"}, "title": "T", "body": "B"},
        "comment": {"body": "hi", "user": {"login": "bob"}},
    }
    event = event_from_webhook("issue_comment", payload)
    assert event is not None
    assert event.pr == 9


def test_unhandled_webhook_returns_none():
    assert event_from_webhook("push", {"ref": "refs/heads/main"}) is None


def test_event_dataclass_defaults_are_safe():
    e = Event(type=EventType.ISSUE_OPENED)
    assert e.raw == {}
    assert e.issue is None
