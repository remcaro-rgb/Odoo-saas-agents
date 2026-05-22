"""GitHub write-back + the webhook handler (Phase D).

The orchestrator's write boundary with GitHub: post the agent's PR comments, add
escalation labels, resolve a PR's head branch. `GitHubClient` is a Protocol seam
— unit tests run against `FakeGitHubClient` (which also doubles as the shadow-mode
client, recording instead of posting); `GhCliClient` shells out to the `gh` CLI.

`handle_webhook` is the top-level entry point: it maps a webhook to an `Event`,
runs the matching flow, and writes the outcome back to the PR.
"""

from __future__ import annotations

import subprocess
from typing import Any, Protocol, runtime_checkable

from .core import IterationResult, Orchestrator
from .events import EventType
from .github_adapter import event_from_webhook

ESCALATION_LABEL = "needs-human"


@runtime_checkable
class GitHubClient(Protocol):
    """The GitHub PR operations the orchestrator's write side needs."""

    def post_comment(self, pr: int, body: str) -> None: ...
    def add_label(self, pr: int, label: str) -> None: ...
    def pr_head_branch(self, pr: int) -> str: ...


class FakeGitHubClient:
    """In-memory `GitHubClient` — records calls, returns canned data.

    Used by unit tests, and doubles as the shadow-mode client (Phase F): the
    agent runs but nothing is posted to GitHub.
    """

    def __init__(self, branches: dict[int, str] | None = None) -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, str]] = []
        self._branches: dict[int, str] = dict(branches or {})

    def post_comment(self, pr: int, body: str) -> None:
        self.comments.append((pr, body))

    def add_label(self, pr: int, label: str) -> None:
        self.labels.append((pr, label))

    def pr_head_branch(self, pr: int) -> str:
        return self._branches.get(pr, f"agent/pr-{pr}")


class GhCliClient:
    """A `GitHubClient` backed by the `gh` CLI — integration-verified against a
    live repo (not unit-tested; it is a thin subprocess wrapper)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh(self, *args: str) -> str:
        """Run a `gh` subcommand against the repo. A non-zero exit surfaces as
        ``subprocess.CalledProcessError`` (``check=True``)."""
        result = subprocess.run(
            ["gh", *args, "--repo", self.repo],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def post_comment(self, pr: int, body: str) -> None:
        self._gh("pr", "comment", str(pr), "--body", body)

    def add_label(self, pr: int, label: str) -> None:
        self._gh("pr", "edit", str(pr), "--add-label", label)

    def pr_head_branch(self, pr: int) -> str:
        return self._gh(
            "pr", "view", str(pr), "--json", "headRefName", "--jq", ".headRefName"
        ).strip()


def handle_webhook(
    event_name: str,
    payload: dict[str, Any],
    orchestrator: Orchestrator,
    github: GitHubClient,
) -> IterationResult | None:
    """GitHub entry point: map a webhook to an `Event`, run the matching flow,
    and write the outcome back to the PR.

    Currently routes the reporter-iteration path (an `issue_comment` on a PR);
    other webhooks return `None`. Wiring `intent_confirmed` to the implement flow
    needs spec-path resolution and is a later step.
    """
    event = event_from_webhook(event_name, payload)
    if event is None or event.type is not EventType.ISSUE_COMMENT:
        return None
    if event.pr is None:
        return None  # malformed payload — no PR to act on or write back to
    # reporter_iteration checks out event.branch, so resolve it before the run.
    event.branch = github.pr_head_branch(event.pr)
    result = orchestrator.reporter_iteration(event)
    if result.comment:
        github.post_comment(event.pr, result.comment)
    if result.status == "escalated":
        github.add_label(event.pr, ESCALATION_LABEL)
    return result
