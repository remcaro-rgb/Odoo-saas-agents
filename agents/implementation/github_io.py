"""GitHub write-back + the webhook handler (Phase D).

The orchestrator's write boundary with GitHub: post the agent's PR comments, add
escalation labels, resolve a PR's head branch, list a PR's changed files.
`GitHubClient` is a Protocol seam — unit tests run against `FakeGitHubClient`
(which also doubles as the shadow-mode client, recording instead of posting);
`GhCliClient` shells out to the `gh` CLI.

`handle_webhook` is the top-level entry point: it maps a webhook to an `Event`,
runs the matching flow (reporter iteration for an `issue_comment`; the implement
flow for an intent-confirmed PR), and writes the outcome back to the PR.
"""

from __future__ import annotations

import subprocess
from typing import Any, Protocol, runtime_checkable

from .commenter import escalation_notice, implementation_ready
from .core import FlowResult, IterationResult, Orchestrator
from .events import Event, EventType
from .github_adapter import event_from_webhook

ESCALATION_LABEL = "needs-human"

# A spec PR keeps its design / fix spec under this directory.
SPEC_DIR = "docs/superpowers/specs/"


def resolve_spec_path(changed_files: list[str]) -> str | None:
    """Pick the one design / fix spec a confirmed PR adds.

    Returns the spec's repo-relative path, or `None` when the PR's changed files
    hold zero or more than one spec (ambiguous — the caller escalates). The
    ``_TEMPLATE-*.md`` skeletons are never treated as a spec.
    """
    in_specs = [
        path
        for path in changed_files
        if path.startswith(SPEC_DIR)
        and path.endswith(".md")
        and not path.rsplit("/", 1)[-1].startswith("_TEMPLATE")
    ]
    typed = [p for p in in_specs if p.endswith(("-design.md", "-fix.md"))]
    pool = typed or in_specs
    return pool[0] if len(pool) == 1 else None


@runtime_checkable
class GitHubClient(Protocol):
    """The GitHub PR operations the orchestrator's write side needs."""

    def post_comment(self, pr: int, body: str) -> None: ...
    def add_label(self, pr: int, label: str) -> None: ...
    def pr_head_branch(self, pr: int) -> str: ...
    def pr_changed_files(self, pr: int) -> list[str]: ...


class FakeGitHubClient:
    """In-memory `GitHubClient` — records calls, returns canned data.

    Used by unit tests, and doubles as the shadow-mode client (Phase F): the
    agent runs but nothing is posted to GitHub.
    """

    def __init__(
        self,
        branches: dict[int, str] | None = None,
        changed_files: dict[int, list[str]] | None = None,
    ) -> None:
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, str]] = []
        self._branches: dict[int, str] = dict(branches or {})
        self._changed_files: dict[int, list[str]] = dict(changed_files or {})

    def post_comment(self, pr: int, body: str) -> None:
        self.comments.append((pr, body))

    def add_label(self, pr: int, label: str) -> None:
        self.labels.append((pr, label))

    def pr_head_branch(self, pr: int) -> str:
        return self._branches.get(pr, f"agent/pr-{pr}")

    def pr_changed_files(self, pr: int) -> list[str]:
        return list(self._changed_files.get(pr, []))


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

    def pr_changed_files(self, pr: int) -> list[str]:
        # `additions > 0` keeps added / modified files and drops pure deletions,
        # so a spec rename (delete old + add new) does not read as two specs.
        out = self._gh(
            "pr", "view", str(pr), "--json", "files",
            "--jq", ".files[] | select(.additions > 0) | .path",
        )
        return [line.strip() for line in out.splitlines() if line.strip()]


def handle_webhook(
    event_name: str,
    payload: dict[str, Any],
    orchestrator: Orchestrator,
    github: GitHubClient,
) -> IterationResult | FlowResult | None:
    """GitHub entry point: map a webhook to an `Event`, run the matching flow,
    and write the outcome back to the PR.

    Routes the two flows Phase D wires up — a reporter `issue_comment` and an
    intent-confirmed (labeled) PR. Other webhooks return `None`.
    """
    event = event_from_webhook(event_name, payload)
    if event is None:
        return None
    if event.type is EventType.ISSUE_COMMENT:
        return _handle_issue_comment(event, orchestrator, github)
    if event.type is EventType.INTENT_CONFIRMED:
        return _handle_intent_confirmed(event, orchestrator, github)
    return None


def _handle_issue_comment(
    event: Event, orchestrator: Orchestrator, github: GitHubClient
) -> IterationResult | None:
    """Run the reporter-iteration flow for a comment on a PR, then write back."""
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


def _escalation_detail(result: FlowResult) -> str:
    """A human-readable reason for an escalated implement flow.

    A planning-stage escalation carries `/analyze` findings as strings; a
    coder-stage escalation carries Odoo-rule `Finding`s on the implement result
    — surface whichever the flow actually produced rather than a generic line.
    """
    if result.stage == "planning" and result.planning.findings:
        return "; ".join(result.planning.findings)
    if result.implement is not None:
        errors = [f for f in result.implement.findings if f.severity == "error"]
        if errors:
            return "; ".join(f"[{f.rule}] {f.message}" for f in errors)
    return "the implementation could not be completed automatically"


def _handle_intent_confirmed(
    event: Event, orchestrator: Orchestrator, github: GitHubClient
) -> FlowResult | None:
    """Run the implement flow for an intent-confirmed PR, then write back.

    The spec to implement is the single design / fix spec among the PR's changed
    files; if that can't be resolved unambiguously, the PR is escalated instead.
    """
    if event.pr is None:
        return None  # malformed payload — no PR to act on or write back to
    spec_path = resolve_spec_path(github.pr_changed_files(event.pr))
    if spec_path is None:
        github.post_comment(
            event.pr,
            escalation_notice(
                "spec-file-not-found",
                "This PR's changed files don't contain exactly one design or "
                "fix spec, so I can't tell which spec to implement.",
            ),
        )
        github.add_label(event.pr, ESCALATION_LABEL)
        return None
    # The implement flow checks out event.branch and reads event.spec_path.
    event.spec_path = spec_path
    result = orchestrator.implement(event)
    if result.status == "implemented":
        github.post_comment(
            event.pr, implementation_ready(f"Implemented `{spec_path}`.")
        )
    else:
        github.post_comment(
            event.pr,
            escalation_notice("implementation-escalated", _escalation_detail(result)),
        )
        github.add_label(event.pr, ESCALATION_LABEL)
    return result
