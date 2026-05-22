"""Unit tests for the GitHub write-back + webhook handler (Phase D)."""

from agents.implementation.core import Orchestrator
from agents.implementation.github_io import (
    FakeGitHubClient,
    GitHubClient,
    handle_webhook,
)
from agents.implementation.speckit_driver import SpecKitDriver
from agents.implementation.workspace import InMemoryWorkspace


def _issue_comment_payload(body: str, pr: int = 42) -> dict:
    return {
        "action": "created",
        "issue": {"number": pr, "pull_request": {"url": f"u/{pr}"}},
        "comment": {"body": body, "user": {"login": "reporter-alice"}},
    }


def test_fake_github_client_satisfies_the_protocol():
    assert isinstance(FakeGitHubClient(), GitHubClient)


def test_handle_webhook_runs_reporter_iteration_and_writes_back(fake_client):
    ws = InMemoryWorkspace()  # no saved session -> a change request escalates
    github = FakeGitHubClient(branches={42: "agent/spec-1500"})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook(
        "issue_comment",
        _issue_comment_payload("Please rename the field"),
        orch,
        github,
    )
    assert result is not None
    assert result.status == "escalated"
    assert ws.branch == "agent/spec-1500"          # the PR head branch was resolved
    assert github.comments and github.comments[0][0] == 42
    assert (42, "needs-human") in github.labels


def test_handle_webhook_noise_comment_writes_nothing(fake_client):
    ws = InMemoryWorkspace()
    github = FakeGitHubClient(branches={42: "agent/spec-1500"})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook(
        "issue_comment", _issue_comment_payload("thanks, appreciate it!"), orch, github
    )
    assert result is not None
    assert result.status == "ignored"
    assert github.comments == []
    assert github.labels == []


def test_handle_webhook_ignores_unrelated_webhooks(fake_client):
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    assert handle_webhook("star", {"action": "created"}, orch, github) is None
    assert github.comments == []
    assert github.labels == []


def test_handle_webhook_ignores_a_comment_with_no_pr_number(fake_client):
    """A malformed issue_comment payload (no PR number) is not actionable —
    handle_webhook must not run a coder iteration it cannot write back."""
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = {
        "action": "created",
        "issue": {"pull_request": {"url": "u"}},  # no "number"
        "comment": {"body": "Please rename the field", "user": {"login": "alice"}},
    }
    assert handle_webhook("issue_comment", payload, orch, github) is None
    assert github.comments == []
    assert github.labels == []
    assert fake_client.commands == []


def test_handle_webhook_ignores_intent_confirmed_for_now(fake_client):
    """intent_confirmed -> implement wiring is a later step; handle_webhook
    returns None for it rather than acting (regression-lock the gap)."""
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = {
        "action": "labeled",
        "label": {"name": "intent-confirmed"},
        "pull_request": {"number": 17, "head": {"ref": "agent/spec-1500"}},
    }
    assert handle_webhook("pull_request", payload, orch, github) is None
