"""Unit tests for spec-generator pushback (Tier 2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.implementation.observability import EventLog
from agents.implementation.rollout import RolloutDecision
from agents.spec_generator.pushback import push_spec


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_shadow_short_circuits_before_any_side_effect(tmp_path):
    log = EventLog()
    pr = push_spec(
        workspace_root=str(tmp_path),
        spec_path="docs/specs/x-design.md",
        spec_body="# spec body",
        branch="agent/spec-0001-x",
        push_url="https://example.invalid",
        issue=1,
        title="x",
        decision=RolloutDecision.SHADOW,
        log=log,
    )
    assert pr is None
    # No file was written to the worktree.
    assert not (tmp_path / "docs" / "specs" / "x-design.md").exists()
    # The shadow-push intent was logged.
    assert any(r["event"] == "shadow-push" for r in log.records)


def test_act_writes_commits_and_attempts_push(tmp_path, monkeypatch):
    # Init a real git repo so the commit step succeeds.
    _git(tmp_path, "init", "--initial-branch=main", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("seed")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "seed")

    push_calls: list[list[str]] = []
    pr_calls: list[list[str]] = []

    real_run = subprocess.run

    def _fake_run(cmd, *args, **kw):
        # Allow all `git` commands to run for real (we want commit to land),
        # but intercept `git push` (no remote) and `gh` (no network).
        if cmd[:2] == ["git", "-C"] and len(cmd) > 3 and cmd[3] == "push":
            push_calls.append(list(cmd))
            return subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr=""
            )
        if cmd[:1] == ["gh"]:
            pr_calls.append(list(cmd))
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="https://github.com/o/r/pull/42\n",
                    stderr="",
                )
        return real_run(cmd, *args, **kw)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    log = EventLog()
    pr = push_spec(
        workspace_root=str(tmp_path),
        spec_path="docs/specs/0001-x-design.md",
        spec_body="# Spec body\n\nSome content.\n",
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
        app_id="999",
    )
    # File materialized.
    assert (tmp_path / "docs" / "specs" / "0001-x-design.md").exists()
    # Commit landed (the branch now exists and points at our new commit).
    branches = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--list"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "agent/spec-0001-x" in branches
    # We attempted a push to the supplied URL.
    assert any("push" in args for args in push_calls)
    # PR creation was invoked.
    assert any(c[1:3] == ["pr", "create"] for c in pr_calls)
    assert pr == 42


def test_act_idempotent_when_no_diff(tmp_path, monkeypatch):
    """Re-running the agent on an unchanged spec must not crash."""
    _git(tmp_path, "init", "--initial-branch=main", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    Path(tmp_path, "docs", "specs").mkdir(parents=True)
    spec = tmp_path / "docs" / "specs" / "x-design.md"
    spec.write_text("# spec body")
    _git(tmp_path, "add", "docs/specs/x-design.md")
    _git(tmp_path, "commit", "-q", "-m", "seed spec")
    _git(tmp_path, "branch", "agent/spec-0001-x")

    real_run = subprocess.run

    def _fake_run(cmd, *args, **kw):
        if cmd[:1] == ["gh"]:
            # Pretend a PR already exists.
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="42\n", stderr="")
        return real_run(cmd, *args, **kw)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    log = EventLog()
    pr = push_spec(
        workspace_root=str(tmp_path),
        spec_path="docs/specs/x-design.md",
        spec_body="# spec body",  # same content -> no diff
        branch="agent/spec-0001-x",
        push_url="https://x:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    assert pr == 42
    assert any(r["event"] == "nothing-to-commit" for r in log.records)


def test_act_aborts_cleanly_when_git_unavailable(tmp_path):
    """A non-git workspace surfaces the git error, doesn't silently corrupt."""
    log = EventLog()
    with pytest.raises(subprocess.CalledProcessError):
        push_spec(
            workspace_root=str(tmp_path),  # no git repo here
            spec_path="x.md",
            spec_body="content",
            branch="agent/spec-1-x",
            push_url="https://x@github.com/o/r.git",
            issue=1,
            title="x",
            decision=RolloutDecision.ACT,
            log=log,
        )
