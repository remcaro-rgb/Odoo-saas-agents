"""Auto-confirm sweep (Tier 3).

The cron job (`deploy/workflows/spec-generator-sweep.yml`, daily at 09:00 UTC)
walks open spec PRs in the `awaiting-reporter-confirm` label state and asks:

  - Has it been >= 24h since the last commit on the spec file?
  - Are there zero unresolved open-questions on the spec?

If both are true, the sweep applies `intent-confirmed` (handing off to the
Implementation Agent) and posts a comment telling the reporter they have a
7-day window to `/reopen` if they spot something.

Pure logic — the GitHub side reads come through an `IssueClient`-extending
``SweepClient`` Protocol. The default implementation reuses the same `gh` CLI
patterns as `github_io.GhCliIssueClient`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from .refiner import AWAITING_CONFIRM_LABEL, INTENT_CONFIRMED_LABEL

# Sentinel marker the drafter writes for items it could not pin down. Mirrors
# `speckit_driver._OPEN_QUESTION` — duplicated here so the sweep can scan a
# spec file's content without dragging the OpenCode-client module into a
# cron-only path.
_OPEN_QUESTION = re.compile(
    r"\[\s*NEEDS\s+(?:CLARIFICATION|INPUT)\s*:",
    re.IGNORECASE,
)

AUTO_CONFIRM_AFTER = timedelta(hours=24)
REOPEN_WINDOW_DAYS = 7


@dataclass
class SweepDecision:
    """What the sweep would do for one PR."""

    pr: int
    branch: str
    spec_path: str | None
    action: str  # "confirm" | "skip" | "no-spec"
    reason: str
    notes: list[str] = field(default_factory=list)
    # Tier 6 dashboard: milliseconds since the spec file's last edit. Only
    # populated for ``action == "confirm"`` (the only decision shape where
    # an elapsed measurement is meaningful — skip/no-spec carry no useful
    # latency signal). Surfaces as the "Median time draft -> intent-confirmed
    # (ms)" panel in Axiom.
    elapsed_ms: int | None = None


@dataclass
class SweepResult:
    """The outcome of one sweep pass."""

    decisions: list[SweepDecision] = field(default_factory=list)

    @property
    def confirmed(self) -> list[SweepDecision]:
        return [d for d in self.decisions if d.action == "confirm"]


@runtime_checkable
class SweepClient(Protocol):
    """The GitHub surface the sweep needs (read + label-write only)."""

    def open_spec_prs(self) -> list[dict]: ...
    def spec_file_last_modified_at(self, pr: int, spec_path: str) -> datetime | None: ...
    def spec_file_contents(self, pr: int, spec_path: str) -> str: ...
    def add_pr_label(self, pr: int, label: str) -> None: ...
    def post_pr_comment(self, pr: int, body: str) -> None: ...


class GhCliSweepClient:
    """`SweepClient` backed by the `gh` CLI — the production wiring.

    Same construction pattern as `github_io.GhCliIssueClient`; the two
    could share a base class but Tier-3-scope says keep them parallel
    until a refactor cycle.
    """

    SPEC_DIR_PREFIX = "docs/superpowers/specs/"

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

    def open_spec_prs(self) -> list[dict]:
        """Open PRs with the `awaiting-reporter-confirm` label."""
        out = self._gh(
            "pr", "list",
            "--state", "open",
            "--label", AWAITING_CONFIRM_LABEL,
            "--json", "number,headRefName,title",
        )
        import json as _json
        try:
            return _json.loads(out or "[]")
        except _json.JSONDecodeError:
            return []

    def spec_file_last_modified_at(
        self, pr: int, spec_path: str
    ) -> datetime | None:
        """The commit timestamp of the most recent commit touching `spec_path`.

        Returns `None` when no commit history is available (e.g. the spec
        file was never committed on the branch — a malformed PR).
        """
        # `gh api repos/:owner/:repo/commits?path=&sha=` lists commits
        # touching a given path on a given ref. We take the first row.
        try:
            out = self._gh(
                "api",
                f"repos/{self.repo}/commits?path={spec_path}&sha=refs/pull/{pr}/head&per_page=1",
            )
        except subprocess.CalledProcessError:
            return None
        import json as _json
        try:
            data = _json.loads(out or "[]")
        except _json.JSONDecodeError:
            return None
        if not data:
            return None
        date_str = (
            data[0].get("commit", {}).get("committer", {}).get("date")
            or data[0].get("commit", {}).get("author", {}).get("date")
        )
        if not date_str:
            return None
        # GitHub ISO-8601 always ends with `Z` for UTC.
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))

    def spec_file_contents(self, pr: int, spec_path: str) -> str:
        """Raw contents of the spec file on the PR branch."""
        try:
            out = self._gh(
                "api",
                f"repos/{self.repo}/contents/{spec_path}?ref=refs/pull/{pr}/head",
                "-q",
                ".content",
            )
        except subprocess.CalledProcessError:
            return ""
        # The API returns base64 in `.content` — decode.
        import base64
        try:
            return base64.b64decode(out.strip()).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    def add_pr_label(self, pr: int, label: str) -> None:
        self._gh("pr", "edit", str(pr), "--add-label", label)

    def post_pr_comment(self, pr: int, body: str) -> None:
        self._gh("pr", "comment", str(pr), "--body", body)


class FakeSweepClient:
    """In-memory `SweepClient` for unit tests."""

    def __init__(
        self,
        *,
        prs: list[dict] | None = None,
        last_modified: dict[tuple[int, str], datetime] | None = None,
        contents: dict[tuple[int, str], str] | None = None,
    ) -> None:
        self._prs = list(prs or [])
        self._last_modified = dict(last_modified or {})
        self._contents = dict(contents or {})
        self.labels_added: list[tuple[int, str]] = []
        self.comments_posted: list[tuple[int, str]] = []

    def open_spec_prs(self) -> list[dict]:
        return list(self._prs)

    def spec_file_last_modified_at(
        self, pr: int, spec_path: str
    ) -> datetime | None:
        return self._last_modified.get((pr, spec_path))

    def spec_file_contents(self, pr: int, spec_path: str) -> str:
        return self._contents.get((pr, spec_path), "")

    def add_pr_label(self, pr: int, label: str) -> None:
        self.labels_added.append((pr, label))

    def post_pr_comment(self, pr: int, body: str) -> None:
        self.comments_posted.append((pr, body))


def has_open_questions(spec_text: str) -> bool:
    """True if the spec text contains any `[NEEDS CLARIFICATION/INPUT: ...]`."""
    return bool(_OPEN_QUESTION.search(spec_text or ""))


def derive_spec_path(branch: str) -> str | None:
    """The spec file path the PR's branch implies.

    Mirrors `drafter.spec_path` — `agent/spec-NNNN-<slug>` ->
    `docs/superpowers/specs/spec-NNNN-<slug>-design.md`. Returns `None`
    when the branch is not an `agent/spec-*` ref.
    """
    if not branch.startswith("agent/spec-"):
        return None
    name = branch[len("agent/") :]
    return f"docs/superpowers/specs/{name}-design.md"


def auto_confirm_comment() -> str:
    """The bot's post-confirmation comment, with the reopen window callout."""
    return (
        "I haven't heard from you in 24 hours and there are no open "
        "questions on the spec — I'm marking it `intent-confirmed` and "
        "handing it off to the Implementation Agent.\n\n"
        f"If you spot something, you have **{REOPEN_WINDOW_DAYS} days** to "
        "comment `/reopen` and pull the PR back into draft for another "
        "round of clarification.\n\n"
        "<!-- spec-gen-agent -->\n"
    )


def run_sweep(
    *,
    client: SweepClient,
    now: datetime | None = None,
    act: bool = True,
) -> SweepResult:
    """Walk open `awaiting-reporter-confirm` PRs and auto-confirm the quiet ones.

    `act=False` makes the sweep dry-run — every decision is computed and
    returned, but the agent posts no comment and adds no label. Useful for
    SHADOW canary and for unit tests against the live `gh` client.
    """
    now = now or datetime.now(UTC)
    decisions: list[SweepDecision] = []
    for pr_row in client.open_spec_prs():
        pr_number = int(pr_row.get("number") or 0)
        branch = str(pr_row.get("headRefName") or "")
        if pr_number == 0:
            continue
        spec_path = derive_spec_path(branch)
        if spec_path is None:
            decisions.append(SweepDecision(
                pr=pr_number, branch=branch, spec_path=None,
                action="no-spec",
                reason=f"branch {branch!r} is not an agent/spec-* ref",
            ))
            continue

        last_modified = client.spec_file_last_modified_at(pr_number, spec_path)
        if last_modified is None:
            decisions.append(SweepDecision(
                pr=pr_number, branch=branch, spec_path=spec_path,
                action="skip",
                reason="no commit history for the spec file",
            ))
            continue

        if (now - last_modified) < AUTO_CONFIRM_AFTER:
            decisions.append(SweepDecision(
                pr=pr_number, branch=branch, spec_path=spec_path,
                action="skip",
                reason=(
                    f"last edit {last_modified.isoformat()} is less than "
                    f"{AUTO_CONFIRM_AFTER} ago"
                ),
            ))
            continue

        spec_text = client.spec_file_contents(pr_number, spec_path)
        if has_open_questions(spec_text):
            decisions.append(SweepDecision(
                pr=pr_number, branch=branch, spec_path=spec_path,
                action="skip",
                reason="spec still has [NEEDS CLARIFICATION] markers",
            ))
            continue

        elapsed = (now - last_modified).total_seconds() * 1000
        decisions.append(SweepDecision(
            pr=pr_number, branch=branch, spec_path=spec_path,
            action="confirm",
            reason="silent + no open questions",
            elapsed_ms=int(elapsed),
        ))
        if act:
            client.add_pr_label(pr_number, INTENT_CONFIRMED_LABEL)
            client.post_pr_comment(pr_number, auto_confirm_comment())

    return SweepResult(decisions=decisions)
