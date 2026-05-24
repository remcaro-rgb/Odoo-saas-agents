"""Unit tests for the Spec Generator github_io seam — FakeIssueClient + handle_webhook."""

from __future__ import annotations

from agents.spec_generator.core import Orchestrator
from agents.spec_generator.github_io import (
    FakeIssueClient,
    ShadowIssueClient,
    handle_webhook,
)


def _issue_payload(
    action: str = "opened",
    number: int = 42,
    title: str = "Add CSV export",
    body: str = "Please add a CSV download.",
    labels: list[str] | None = None,
):
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": "alice"},
            "labels": [{"name": name} for name in (labels or [])],
        },
        "label": None,
    }


def test_fake_issue_client_round_trips_labels_and_records_writes():
    fake = FakeIssueClient(labels_by_issue={42: ["feature-request"]})
    assert fake.issue_labels(42) == ["feature-request"]
    fake.post_issue_comment(42, "hi")
    fake.add_issue_label(42, "spec-drafted")
    assert fake.issue_comments_posted == [(42, "hi")]
    assert fake.issue_labels_added == [(42, "spec-drafted")]


def test_shadow_client_records_writes_but_delegates_reads():
    inner = FakeIssueClient(labels_by_issue={42: ["bug"]})
    shadow = ShadowIssueClient(inner)
    shadow.post_issue_comment(42, "x")
    shadow.add_issue_label(42, "spec-drafted")
    assert shadow.issue_comments_posted == [(42, "x")]
    assert shadow.issue_labels_added == [(42, "spec-drafted")]
    # Writes did NOT pass through to the inner reader.
    assert inner.issue_comments_posted == []
    # Reads do delegate.
    assert shadow.issue_labels(42) == ["bug"]


def test_handle_webhook_skipped_for_unhandled_event(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    assert handle_webhook("push", {"ref": "refs/heads/main"}, orch, github) is None


def test_handle_webhook_drafts_for_a_feature_request(fake_client):
    fake_client.set_command_result(
        "speckit.specify",
        {"parts": [{"type": "text", "text": "# Spec\n- captured\n"}]},
    )
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient(labels_by_issue={42: ["feature-request"]})
    result = handle_webhook(
        "issues",
        _issue_payload(labels=["feature-request"]),
        orch,
        github,
    )
    assert result is not None
    assert result.status == "drafted"
    # The summary comment was posted to the issue.
    assert any(issue == 42 for issue, _ in github.issue_comments_posted)
    # Both routing labels landed.
    label_names = [name for _, name in github.issue_labels_added]
    assert "spec-drafted" in label_names
    assert "awaiting-reporter-confirm" in label_names


def test_handle_webhook_issue_comment_with_no_session_is_skipped(fake_client):
    """A reporter comment on an issue with no prior draft has nothing to clarify."""
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    payload = {
        "action": "created",
        "issue": {"number": 7, "title": "T", "body": "B"},
        "comment": {"body": "/confirm", "user": {"login": "bob"}},
    }
    result = handle_webhook("issue_comment", payload, orch, github)
    # No session known for issue 7 -> we return a DraftResult skipped.
    assert result is not None
    assert getattr(result, "status", "") == "skipped"
    assert github.issue_comments_posted == []
    assert fake_client.commands == []


def test_handle_webhook_issue_comment_confirm_routes_through_refiner(fake_client):
    """A `/confirm` on a PR with a known session hands off via the refiner."""
    from agents.spec_generator.session_store import InMemorySessionStore

    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    sessions = InMemorySessionStore({7: "sess-known"})
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "title": "T",
            "body": "B",
            "pull_request": {"url": "x"},
        },
        "comment": {"body": "/confirm", "user": {"login": "alice"}},
    }
    outcome = handle_webhook(
        "issue_comment", payload, orch, github, session_store=sessions
    )
    assert outcome is not None
    assert getattr(outcome, "status", "") == "handed_off"
    # PR-targeted label applied (the event carries a PR).
    label_names = [name for _, name in github.pr_labels_added]
    assert "intent-confirmed" in label_names


def test_handle_webhook_skips_own_bot_comments(fake_client):
    """The agent must not iterate on its own comments — infinite-loop hazard."""
    from agents.spec_generator.session_store import InMemorySessionStore

    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    sessions = InMemorySessionStore({7: "sess-known"})
    payload = {
        "action": "created",
        "issue": {"number": 7, "title": "T", "body": "B"},
        "comment": {
            "body": "I've drafted a design spec...",
            "user": {"login": "spec-generator-bot[bot]"},
        },
    }
    result = handle_webhook(
        "issue_comment", payload, orch, github, session_store=sessions
    )
    assert result is not None
    assert getattr(result, "status", "") == "skipped"
    assert fake_client.commands == []


def test_handle_webhook_persists_session_id_after_drafting(fake_client):
    """Drafting must save the session id for later reporter comments."""
    from agents.spec_generator.session_store import InMemorySessionStore

    fake_client.set_command_result(
        "speckit.specify",
        {"parts": [{"type": "text", "text": "# Spec\n- captured\n"}]},
    )
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient(labels_by_issue={42: ["feature-request"]})
    sessions = InMemorySessionStore()
    handle_webhook(
        "issues",
        _issue_payload(labels=["feature-request"]),
        orch,
        github,
        session_store=sessions,
    )
    # Drafter created `sess-1` (FakeOpenCodeClient counter); it must now be
    # findable in the store for the next comment on issue 42.
    assert sessions.get(42) == "sess-1"


def test_handle_webhook_sensitive_escalation_writes_label_and_comment(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    payload = _issue_payload(
        body="Paste of my secret password: hunter2 and AKIAIOSFODNN7EXAMPLE."
    )
    result = handle_webhook("issues", payload, orch, github)
    assert result is not None
    assert result.status == "escalated"
    assert any(
        name == "needs-security-triage"
        for _, name in github.issue_labels_added
    )
    # Comment was posted to the issue, OpenCode was never called.
    assert github.issue_comments_posted
    assert fake_client.commands == []


def test_handle_webhook_with_no_issue_number_returns_skipped(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    github = FakeIssueClient()
    payload = {"action": "opened", "issue": {"title": "T", "body": "B"}}
    result = handle_webhook("issues", payload, orch, github)
    assert result is not None
    assert result.status == "skipped"


def test_handle_webhook_falls_back_to_payload_labels_when_live_read_fails(
    fake_client, monkeypatch
):
    """A failing `gh issue view` (e.g. fictional issue number in a smoke
    test) must not crash the run — the classifier still gets a labelset
    drawn from the webhook payload itself."""
    fake_client.set_command_result(
        "speckit.specify",
        {"parts": [{"type": "text", "text": "# Spec\n- captured\n"}]},
    )
    orch = Orchestrator(oc_client=fake_client)

    class _BrokenReader(FakeIssueClient):
        def issue_labels(self, issue):
            raise RuntimeError("gh: HTTP 404")

    github = _BrokenReader()
    result = handle_webhook(
        "issues",
        _issue_payload(labels=["feature-request"]),
        orch,
        github,
    )
    assert result is not None
    assert result.status == "drafted"
