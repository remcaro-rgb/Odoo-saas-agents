"""Integration tests for GitWorkspace — exercised against a throwaway git repo.

Hermetic and offline: a temp repo per test, real `git` via subprocess, no network.
"""

import subprocess

import pytest

from agents.implementation.git_workspace import GitWorkspace
from agents.implementation.workspace import Workspace


@pytest.fixture()
def repo(tmp_path):
    """A fresh git repo in a temp dir, with a local identity for committing."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    return tmp_path


def test_git_workspace_satisfies_the_protocol(repo):
    assert isinstance(GitWorkspace(str(repo)), Workspace)


def test_write_then_read_roundtrips(repo):
    ws = GitWorkspace(str(repo))
    ws.write("models/widget.py", "class Widget: pass")
    assert ws.read("models/widget.py") == "class Widget: pass"


def test_read_missing_file_raises(repo):
    with pytest.raises(FileNotFoundError):
        GitWorkspace(str(repo)).read("nope.py")


def test_exists_reflects_the_filesystem(repo):
    ws = GitWorkspace(str(repo))
    assert not ws.exists("a.txt")
    ws.write("a.txt", "hi")
    assert ws.exists("a.txt")


def test_list_files_filters_by_prefix_and_skips_git(repo):
    ws = GitWorkspace(str(repo))
    ws.write("models/a.py", "")
    ws.write("models/b.py", "")
    ws.write("views/c.xml", "")
    assert ws.list_files("models/") == ["models/a.py", "models/b.py"]


def test_checkout_creates_and_switches_branch(repo):
    GitWorkspace(str(repo)).checkout("agent/spec-1500")
    current = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current == "agent/spec-1500"


def test_commit_writes_a_real_commit(repo):
    ws = GitWorkspace(str(repo))
    ws.checkout("agent/spec-1")
    ws.write("plan.md", "the plan")
    sha = ws.commit(["plan.md"], "[impl-agent] plan: widget")
    assert sha
    subject = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "[impl-agent] plan: widget"


def test_escalate_is_recorded(repo):
    ws = GitWorkspace(str(repo))
    ws.escalate("spec-refinement-needed", "analyze found a contradiction")
    assert ws.escalations[-1].reason == "spec-refinement-needed"
    assert "contradiction" in ws.escalations[-1].details


# -- apply_session_diff (Tier-4 sync, GitWorkspace half) ----------------------
def _seed(repo):
    """A repo with one initial commit so subsequent patches have a parent ref."""
    (repo / "initial.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "initial.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)


def _patch_for_added(repo, rel, content):
    """Capture a real git diff for adding `rel`, then leave the repo clean."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    patch = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached"],
        check=True, capture_output=True, text=True,
    ).stdout
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "HEAD", rel], check=True)
    target.unlink()
    return patch


def test_git_workspace_apply_session_diff_applies_a_unified_diff_patch(repo):
    """GitWorkspace.apply_session_diff delegates to pushback's git-apply
    plumbing — applies a `SnapshotFileDiff[]` (file + patch + status) to
    the real worktree."""
    _seed(repo)
    ws = GitWorkspace(str(repo))
    patch = _patch_for_added(repo, "agent_wrote.txt", "hello from the agent\n")
    n = ws.apply_session_diff(
        [{"file": "agent_wrote.txt", "status": "added", "patch": patch}]
    )
    assert n == 1
    assert (repo / "agent_wrote.txt").read_text() == "hello from the agent\n"


def test_git_workspace_apply_session_diff_drops_protected_paths(repo):
    """A diff that targets a guardrail path is filtered out before
    `git apply` ever sees it — defense in depth on the GitWorkspace half."""
    _seed(repo)
    ws = GitWorkspace(str(repo))
    patch = _patch_for_added(repo, ".github/workflows/evil.yml", "name: evil\n")
    n = ws.apply_session_diff(
        [{"file": ".github/workflows/evil.yml", "status": "added", "patch": patch}]
    )
    assert n == 0
    assert not (repo / ".github/workflows/evil.yml").exists()
