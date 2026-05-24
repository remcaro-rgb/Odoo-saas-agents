"""GitHub write-back + the webhook handler for the Spec Generator.

The orchestrator's write boundary with GitHub: post the agent's issue / PR
comments, add labels, read live label sets. `IssueClient` is a Protocol seam —
unit tests run against `FakeIssueClient`, the SHADOW client wraps a real one
and records intents instead of firing them, and `GhCliIssueClient` shells out
to the `gh` CLI for the ACT path.

`handle_webhook` is the top-level entry point: it maps a webhook to a Spec
Generator `Event`, runs the orchestrator's `draft_spec` flow, and writes the
resulting `DraftResult.comments` / `labels` back to GitHub.

Tier 1 scope: the `issues.opened` and `issues.labeled` paths. The
`issue_comment.created` path is wired but currently delegates straight to a
"skipped" result (the reporter Q&A loop is Tier 2 — see `refiner.py`).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Protocol, runtime_checkable

from .core import DraftResult, Orchestrator, SkipReason
from .events import Event, EventType
from .github_adapter import event_from_webhook


@runtime_checkable
class IssueClient(Protocol):
    """The GitHub operations the Spec Generator's write side needs."""

    def issue_labels(self, issue: int) -> list[str]: ...
    def post_issue_comment(self, issue: int, body: str) -> None: ...
    def post_pr_comment(self, pr: int, body: str) -> None: ...
    def add_issue_label(self, issue: int, label: str) -> None: ...
    def add_pr_label(self, pr: int, label: str) -> None: ...
    def issue_comments(self, issue: int) -> list[str]: ...
    def pr_comments(self, pr: int) -> list[str]: ...


class FakeIssueClient:
    """In-memory `IssueClient` — records calls, returns canned data.

    Used by unit tests, and doubles as the in-memory shadow client for
    SHADOW-mode runs that don't even need a real reader (the fixture
    contains everything).
    """

    def __init__(
        self,
        labels_by_issue: dict[int, list[str]] | None = None,
        comments_by_issue: dict[int, list[str]] | None = None,
        comments_by_pr: dict[int, list[str]] | None = None,
    ) -> None:
        self.issue_comments_posted: list[tuple[int, str]] = []
        self.pr_comments_posted: list[tuple[int, str]] = []
        self.issue_labels_added: list[tuple[int, str]] = []
        self.pr_labels_added: list[tuple[int, str]] = []
        self._labels: dict[int, list[str]] = {
            int(k): list(v) for k, v in (labels_by_issue or {}).items()
        }
        self._issue_comments: dict[int, list[str]] = {
            int(k): list(v) for k, v in (comments_by_issue or {}).items()
        }
        self._pr_comments: dict[int, list[str]] = {
            int(k): list(v) for k, v in (comments_by_pr or {}).items()
        }

    def issue_labels(self, issue: int) -> list[str]:
        return list(self._labels.get(issue, []))

    def post_issue_comment(self, issue: int, body: str) -> None:
        self.issue_comments_posted.append((issue, body))

    def post_pr_comment(self, pr: int, body: str) -> None:
        self.pr_comments_posted.append((pr, body))

    def add_issue_label(self, issue: int, label: str) -> None:
        self.issue_labels_added.append((issue, label))

    def add_pr_label(self, pr: int, label: str) -> None:
        self.pr_labels_added.append((pr, label))

    def issue_comments(self, issue: int) -> list[str]:
        return list(self._issue_comments.get(issue, []))

    def pr_comments(self, pr: int) -> list[str]:
        return list(self._pr_comments.get(pr, []))


class GhCliIssueClient:
    """A `IssueClient` backed by the `gh` CLI.

    Integration-verified against a live repo (not unit-tested; it is a thin
    subprocess wrapper). Mirrors `GhCliClient` in the implementation agent —
    the same `_gh` helper shape, the same warn-and-continue posture for label
    failures (the comment is the primary signal; the label is secondary).
    """

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh(self, *args: str) -> str:
        result = subprocess.run(
            ["gh", *args, "--repo", self.repo],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def issue_labels(self, issue: int) -> list[str]:
        out = self._gh(
            "issue", "view", str(issue), "--json", "labels",
            "--jq", ".labels[].name",
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def post_issue_comment(self, issue: int, body: str) -> None:
        self._gh("issue", "comment", str(issue), "--body", body)

    def post_pr_comment(self, pr: int, body: str) -> None:
        self._gh("pr", "comment", str(pr), "--body", body)

    def add_issue_label(self, issue: int, label: str) -> None:
        try:
            self._gh("issue", "edit", str(issue), "--add-label", label)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "no detail"
            sys.stderr.write(
                f"warning: gh issue edit --add-label {label} failed "
                f"(exit {exc.returncode}): {tail}\n"
            )

    def add_pr_label(self, pr: int, label: str) -> None:
        try:
            self._gh("pr", "edit", str(pr), "--add-label", label)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "no detail"
            sys.stderr.write(
                f"warning: gh pr edit --add-label {label} failed "
                f"(exit {exc.returncode}): {tail}\n"
            )

    def issue_comments(self, issue: int) -> list[str]:
        out = self._gh(
            "issue", "view", str(issue), "--json", "comments",
            "--jq", ".comments[].body",
        )
        return [line for line in out.splitlines() if line]

    def pr_comments(self, pr: int) -> list[str]:
        out = self._gh(
            "pr", "view", str(pr), "--json", "comments",
            "--jq", ".comments[].body",
        )
        return [line for line in out.splitlines() if line]


class ShadowIssueClient:
    """A shadow-mode `IssueClient`: real reads, recorded-but-not-sent writes.

    The SHADOW rollout stage (see `rollout.py`) runs the orchestrator for
    real — it reads live label sets and drives OpenCode — but posts and
    labels nothing. Reads delegate to a real `IssueClient`; writes are
    appended to `issue_comments_posted` / `pr_comments_posted` /
    `issue_labels_added` / `pr_labels_added` so the composition root can
    log what *would* have been posted.
    """

    def __init__(self, reader: IssueClient) -> None:
        self._reader = reader
        self.issue_comments_posted: list[tuple[int, str]] = []
        self.pr_comments_posted: list[tuple[int, str]] = []
        self.issue_labels_added: list[tuple[int, str]] = []
        self.pr_labels_added: list[tuple[int, str]] = []

    def issue_labels(self, issue: int) -> list[str]:
        return self._reader.issue_labels(issue)

    def post_issue_comment(self, issue: int, body: str) -> None:
        self.issue_comments_posted.append((issue, body))

    def post_pr_comment(self, pr: int, body: str) -> None:
        self.pr_comments_posted.append((pr, body))

    def add_issue_label(self, issue: int, label: str) -> None:
        self.issue_labels_added.append((issue, label))

    def add_pr_label(self, pr: int, label: str) -> None:
        self.pr_labels_added.append((pr, label))

    def issue_comments(self, issue: int) -> list[str]:
        return self._reader.issue_comments(issue)

    def pr_comments(self, pr: int) -> list[str]:
        return self._reader.pr_comments(pr)


def handle_webhook(
    event_name: str,
    payload: dict[str, Any],
    orchestrator: Orchestrator,
    github: IssueClient,
) -> DraftResult | None:
    """GitHub entry point: map a webhook to an Event and dispatch.

    Reads live labels off the issue before classifying so the orchestrator's
    classifier can break ties using the *current* labelset, not just whatever
    the webhook payload happened to carry.
    """
    event = event_from_webhook(event_name, payload)
    if event is None:
        return None

    if event.type is EventType.ISSUE_COMMENT:
        # Reporter Q&A ships in Tier 2 (`refiner.py`). For Tier 1 we record
        # the skip so observability captures every webhook we saw.
        return DraftResult(
            status="skipped",
            issue=event.issue,
            skip_reason=SkipReason.NOT_A_TRIGGER,
            notes=["issue_comment routing reserved for Tier 2 (refiner.py)"],
        )

    if event.issue is None:
        return DraftResult(
            status="skipped",
            issue=None,
            skip_reason=SkipReason.NOT_A_TRIGGER,
            notes=["event missing an issue number"],
        )

    live_labels = _read_labels(github, event)
    result = orchestrator.draft_spec(event, live_labels=live_labels)
    _apply_writes(github, result)
    return result


def _read_labels(github: IssueClient, event: Event) -> list[str]:
    """Read the issue's live labels with a safe payload-based fallback.

    The live read is the source of truth (handles a human labeling AFTER the
    webhook fired), but a transient `gh` failure — or a smoke-test fixture
    pointing at an issue that doesn't exist in the live repo — must not crash
    the run. When the read fails we extract whatever labels the webhook
    payload itself carried and continue. The classifier still gets *a*
    labelset; the worst case is a slightly-stale view that the next
    `issues.labeled` webhook refreshes.
    """
    if event.issue is None:
        return []
    try:
        return github.issue_labels(event.issue)
    except Exception:  # noqa: BLE001
        issue = (event.raw or {}).get("issue") or {}
        labels = issue.get("labels") or []
        return [
            (item.get("name") or "").strip()
            for item in labels
            if isinstance(item, dict) and item.get("name")
        ]


def _apply_writes(github: IssueClient, result: DraftResult) -> None:
    """Push the orchestrator's recorded comments + labels through the client.

    SHADOW clients record-only; ACT clients hit GitHub. The `target` field
    decides issue-vs-PR (the spec PR is opened by the workspace push, not
    handled here in Tier 1).
    """
    for target, body in result.comments:
        if target == "issue" and result.issue is not None:
            github.post_issue_comment(result.issue, body)
        elif target == "pr":
            # Tier 1 has no PR yet — the drafter doesn't push. Skip the PR
            # comment but keep the record (it stays in `result.comments`).
            continue
    for target, label in result.labels:
        if target == "issue" and result.issue is not None:
            github.add_issue_label(result.issue, label)
