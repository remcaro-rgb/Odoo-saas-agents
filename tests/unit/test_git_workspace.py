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
