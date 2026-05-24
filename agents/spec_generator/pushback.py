"""Push the drafted spec to a new branch and open the spec PR (Tier 2).

The drafter produces a `DraftedSpec` in memory; this module:

1. Writes the spec body to ``<workspace>/<path>`` (creating parent dirs).
2. Commits as ``spec-generator-bot[bot]`` (the GitHub App noreply email).
3. Pushes to ``<branch>`` using a short-lived App installation token.
4. Opens the spec PR via ``gh pr create`` (idempotent — re-runs are a no-op
   if the PR already exists).

Shadow-aware: in SHADOW, every external side-effect is logged-only — no
file is written to the worktree, no commit is created, no push is made.
The event log captures *what would have happened* so the SHADOW audit is
faithful.

Mirrors the implementation agent's ``pushback.push_implementation`` shape
but is materially simpler — Spec Generator writes one file, never patches.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agents.implementation.observability import EventLog
from agents.implementation.rollout import RolloutDecision

BOT_NAME = "spec-generator-bot[bot]"


def _git(
    workspace_root: str,
    *args: str,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <workspace_root> <args>``. Surfaces stderr on failure."""
    proc = subprocess.run(
        ["git", "-C", workspace_root, *args],
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd=list(proc.args) + ([f"# stderr: {stderr}"] if stderr else []),
            output=proc.stdout,
            stderr=stderr,
        )
    return proc


def _bot_email(app_id: str | None) -> str:
    """The canonical ``<id>+spec-generator-bot[bot]@users.noreply.github.com``."""
    if not app_id:
        return "spec-generator-bot[bot]@users.noreply.github.com"
    return f"{app_id}+spec-generator-bot[bot]@users.noreply.github.com"


def push_spec(
    *,
    workspace_root: str,
    spec_path: str,
    spec_body: str,
    branch: str,
    push_url: str,
    issue: int,
    title: str,
    decision: RolloutDecision,
    log: EventLog,
    app_id: str | None = None,
    base_branch: str = "main",
) -> int | None:
    """Write, commit, push the spec; open the PR. Returns the PR number or `None`.

    ``push_url`` is the URL with the bot token embedded — built by the
    composition root from ``GH_TOKEN``. SHADOW short-circuits before any
    side-effect.
    """
    if decision is RolloutDecision.SHADOW:
        log.emit(
            "shadow-push",
            branch=branch,
            spec_path=spec_path,
            bytes=len(spec_body or ""),
            issue=issue,
        )
        return None

    # 1. Materialize the spec file.
    full_path = Path(workspace_root) / spec_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(spec_body, encoding="utf-8")
    log.emit("wrote-spec", path=spec_path, bytes=len(spec_body))

    # 2. Configure the bot identity (per-repo, no global).
    _git(workspace_root, "config", "user.name", BOT_NAME)
    _git(workspace_root, "config", "user.email", _bot_email(app_id))

    # 3. Switch to (or create) the agent branch.
    proc = _git(workspace_root, "branch", "--show-current", check=False)
    current = (proc.stdout or "").strip()
    if current != branch:
        # `git switch` creates the branch off HEAD if it doesn't exist, or
        # checks out the existing one. `-c` errors if the branch exists; we
        # use a two-step to be idempotent.
        ls = _git(workspace_root, "rev-parse", "--verify", branch, check=False)
        if ls.returncode == 0:
            _git(workspace_root, "switch", branch)
        else:
            _git(workspace_root, "switch", "-c", branch)

    # 4. Stage + commit. Skip the commit if nothing changed (a re-run on
    # the same spec is idempotent — there is nothing to push).
    _git(workspace_root, "add", spec_path)
    diff = _git(workspace_root, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        log.emit("nothing-to-commit", path=spec_path)
        return _ensure_pr(
            workspace_root=workspace_root,
            branch=branch,
            base_branch=base_branch,
            issue=issue,
            title=title,
            spec_path=spec_path,
            log=log,
        )
    _git(
        workspace_root,
        "commit",
        "-m",
        f"Spec Generator — draft spec for issue #{issue}",
    )
    log.emit("committed-spec", branch=branch, path=spec_path)

    # 5. Push. Strip any existing remote-tracking so the explicit URL wins.
    _git(workspace_root, "push", "--force-with-lease", push_url, f"HEAD:{branch}")
    log.emit("pushed-branch", branch=branch)

    return _ensure_pr(
        workspace_root=workspace_root,
        branch=branch,
        base_branch=base_branch,
        issue=issue,
        title=title,
        spec_path=spec_path,
        log=log,
    )


def _ensure_pr(
    *,
    workspace_root: str,
    branch: str,
    base_branch: str,
    issue: int,
    title: str,
    spec_path: str,
    log: EventLog,
) -> int | None:
    """Find an existing PR for `branch`, or open a new one. Returns PR number.

    The Action runner has `gh` available; we shell out rather than depend
    on a Python GitHub SDK (consistent with `github_io.GhCliIssueClient`).
    """
    existing = _gh_pr_for_branch(workspace_root, branch)
    if existing is not None:
        log.emit("pr-exists", pr=existing, branch=branch)
        return existing

    body = (
        f"Spec Generator — drafted in response to issue #{issue}.\n\n"
        f"This PR contains the design spec at `{spec_path}`.\n\n"
        f"Comment `/confirm` on issue #{issue} (or here) to advance the "
        f"`intent-confirmed` label and hand the spec off to the "
        f"Implementation Agent. If no questions land in the next 24 hours, "
        f"the sweep job will confirm automatically."
    )
    proc = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", f"spec: {title}",
            "--body", body,
            "--head", branch,
            "--base", base_branch,
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        log.emit("pr-create-failed", branch=branch, stderr=stderr[:500])
        return None
    # gh prints the new PR URL — extract the trailing number.
    url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    number = _parse_pr_url(url)
    log.emit("pr-created", pr=number, url=url)
    return number


def _gh_pr_for_branch(workspace_root: str, branch: str) -> int | None:
    proc = subprocess.run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json", "number",
            "--jq", ".[0].number // empty",
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
    )
    text = (proc.stdout or "").strip()
    return int(text) if text.isdigit() else None


def _parse_pr_url(url: str) -> int | None:
    """`https://github.com/owner/repo/pull/123` -> 123. None if unparseable."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
