"""Unit tests for the implement→PR-branch push (Tier 2 push-back)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agents.implementation.observability import EventLog
from agents.implementation.pushback import (
    apply_session_diff,
    commit_and_push,
    push_implementation,
)
from agents.implementation.rollout import RolloutDecision


# -- helpers -------------------------------------------------------------------
def _git(path: Path, *args: str, input: bytes | None = None) -> str:
    """Run a git command against `path` and return its stdout (strip-free)."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True if input is None else False,
        input=input,
    ).stdout if input is None else subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, input=input,
    ).stdout.decode()


def _init_repo(path: Path) -> None:
    """A fresh git repo at `path` with a small initial commit on `main`."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "initial.txt").write_text("initial\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def _generate_patch(path: Path, rel: str, after: str) -> str:
    """Produce a real git-style unified diff for adding-or-modifying `rel` to
    `after`, then leave the repo clean so the diff can be re-applied."""
    target = path / rel
    before = target.read_text() if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after)
    if before is None:
        subprocess.run(["git", "-C", str(path), "add", rel], check=True)
        diff = subprocess.run(
            ["git", "-C", str(path), "diff", "--cached"],
            check=True, capture_output=True, text=True,
        ).stdout
        subprocess.run(["git", "-C", str(path), "reset", "-q", "HEAD", rel], check=True)
        target.unlink()
    else:
        diff = subprocess.run(
            ["git", "-C", str(path), "diff", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout
        target.write_text(before)
    return diff


class _FakeOpenCode:
    """Implements only the `get_diff(session_id)` surface."""

    def __init__(self, diffs: list[dict[str, Any]]) -> None:
        self._diffs = diffs

    def get_diff(
        self, session_id: str, *, message_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._diffs


def _log() -> EventLog:
    return EventLog(sink=lambda _: None)


def _events(log: EventLog) -> list[str]:
    return [record["event"] for record in log.records]


# -- apply_session_diff --------------------------------------------------------
def test_apply_session_diff_with_an_empty_list_returns_zero(tmp_path):
    _init_repo(tmp_path)
    assert apply_session_diff(str(tmp_path), []) == 0


def test_apply_session_diff_skips_entries_without_a_patch_string(tmp_path):
    _init_repo(tmp_path)
    diffs = [{"file": "x", "additions": 1, "deletions": 0, "status": "modified"}]
    assert apply_session_diff(str(tmp_path), diffs) == 0


def test_apply_session_diff_creates_an_added_file(tmp_path):
    _init_repo(tmp_path)
    patch = _generate_patch(tmp_path, "new.txt", "hello, new\n")
    n = apply_session_diff(
        str(tmp_path),
        [{"file": "new.txt", "patch": patch, "status": "added"}],
    )
    assert n == 1
    assert (tmp_path / "new.txt").read_text() == "hello, new\n"


def test_apply_session_diff_modifies_an_existing_file(tmp_path):
    _init_repo(tmp_path)
    patch = _generate_patch(tmp_path, "initial.txt", "initial\nadded line\n")
    n = apply_session_diff(
        str(tmp_path),
        [{"file": "initial.txt", "patch": patch, "status": "modified"}],
    )
    assert n == 1
    assert (tmp_path / "initial.txt").read_text() == "initial\nadded line\n"


def test_apply_session_diff_is_idempotent_when_patch_already_applied(tmp_path):
    """Coder._sync_from_session applies the diff to the workspace; then
    pushback.push_implementation fetches the same diff and tries to apply it
    again. Without idempotency the second `git apply` errors and crashes the
    run. With it, the reverse-check fallback treats the patch as a no-op."""
    _init_repo(tmp_path)
    patch = _generate_patch(tmp_path, "synced.txt", "agent-write\n")
    diff = [{"file": "synced.txt", "patch": patch, "status": "added"}]
    # First apply lands the change.
    assert apply_session_diff(str(tmp_path), diff) == 1
    assert (tmp_path / "synced.txt").read_text() == "agent-write\n"
    # Second apply must not error — reverse-check sees the patch is already
    # in the tree and returns cleanly.
    assert apply_session_diff(str(tmp_path), diff) == 1
    assert (tmp_path / "synced.txt").read_text() == "agent-write\n"


def test_apply_session_diff_filters_protected_paths(tmp_path):
    """A diff entry targeting a guardrail path is dropped before git apply
    runs — defense in depth on top of provisioning's sparse-checkout."""
    _init_repo(tmp_path)
    # Build a real patch for a protected path so the test is realistic.
    bad_patch = _generate_patch(
        tmp_path, ".github/workflows/evil.yml", "name: evil\n"
    )
    good_patch = _generate_patch(tmp_path, "ok.txt", "ok\n")
    n = apply_session_diff(
        str(tmp_path),
        [
            {"file": ".github/workflows/evil.yml", "patch": bad_patch, "status": "added"},
            {"file": "ok.txt", "patch": good_patch, "status": "added"},
        ],
    )
    assert n == 1                                    # only the safe one
    assert (tmp_path / "ok.txt").read_text() == "ok\n"
    assert not (tmp_path / ".github/workflows/evil.yml").exists()


# -- commit_and_push -----------------------------------------------------------
def test_commit_and_push_with_no_staged_changes_returns_none(tmp_path):
    """If nothing changed, do not create an empty commit and do not push."""
    _init_repo(tmp_path)
    sha = commit_and_push(
        str(tmp_path), "main",
        push_url="file://" + str(tmp_path / "does-not-exist"),
        message="should not be created", app_id="123",
    )
    assert sha is None


def test_commit_and_push_commits_as_the_bot_and_pushes_to_the_remote(tmp_path):
    # bare remote + working repo wired to it
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True
    )
    work = tmp_path / "work"
    _init_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "main"], check=True
    )

    # a change to commit
    (work / "extra.txt").write_text("an extra file\n")

    sha = commit_and_push(
        str(work), "main", push_url=str(remote),
        message="[impl-agent] foo: implement spec", app_id="42",
    )
    assert sha is not None and len(sha) == 40

    head = subprocess.run(
        ["git", "-C", str(remote), "log", "-1", "--format=%H%n%an%n%ae%n%s"],
        check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert head[0] == sha
    assert head[1] == "implementation-bot[bot]"
    assert "42+implementation-bot[bot]@users.noreply.github.com" in head[2]
    assert head[3] == "[impl-agent] foo: implement spec"


# -- push_implementation (the orchestrating function) --------------------------
def test_push_implementation_in_shadow_logs_and_does_not_touch_the_workspace(tmp_path):
    _init_repo(tmp_path)
    sentinel = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    fake = _FakeOpenCode([{"file": "x.txt", "patch": "ignored", "status": "added"}])
    log = _log()
    sha = push_implementation(
        workspace_root=str(tmp_path), oc_client=fake,
        session_id="ses_x", branch="agent/spec-1",
        push_url="file:///dev/null", feature="spec-1",
        decision=RolloutDecision.SHADOW, log=log,
    )
    assert sha is None
    assert "push.shadowed" in _events(log)
    # HEAD did not move, no commit created
    after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert after == sentinel


def test_push_implementation_act_with_no_diff_logs_no_diff(tmp_path):
    _init_repo(tmp_path)
    fake = _FakeOpenCode([])
    log = _log()
    sha = push_implementation(
        workspace_root=str(tmp_path), oc_client=fake,
        session_id="ses_x", branch="agent/spec-1",
        push_url="file:///dev/null", feature="spec-1",
        decision=RolloutDecision.ACT, log=log,
    )
    assert sha is None
    assert "push.no-diff" in _events(log)


def test_push_implementation_act_applies_diff_commits_and_pushes(tmp_path):
    # bare remote + working repo
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True
    )
    work = tmp_path / "work"
    _init_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "main"], check=True
    )

    patch = _generate_patch(work, "added-by-agent.txt", "agent wrote me\n")
    fake = _FakeOpenCode(
        [{"file": "added-by-agent.txt", "patch": patch, "status": "added"}]
    )
    log = _log()
    sha = push_implementation(
        workspace_root=str(work), oc_client=fake,
        session_id="ses_x", branch="main",
        push_url=str(remote), feature="spec-1",
        decision=RolloutDecision.ACT, log=log, app_id="42",
    )
    assert sha is not None and len(sha) == 40
    assert "push.committed" in _events(log)
    # remote received the commit and the added file is in its tree
    log_head = subprocess.run(
        ["git", "-C", str(remote), "log", "-1", "--name-only", "--format="],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert "added-by-agent.txt" in log_head
