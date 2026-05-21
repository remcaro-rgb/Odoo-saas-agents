"""Unit tests for the Workspace seam (Phase B)."""

import pytest

from agents.implementation.workspace import InMemoryWorkspace, Workspace


def test_write_then_read_roundtrips():
    ws = InMemoryWorkspace()
    ws.write("models/widget.py", "class Widget: pass")
    assert ws.read("models/widget.py") == "class Widget: pass"


def test_read_missing_file_raises():
    ws = InMemoryWorkspace()
    with pytest.raises(FileNotFoundError):
        ws.read("nope.py")


def test_exists_reports_presence():
    ws = InMemoryWorkspace({"a.txt": "hi"})
    assert ws.exists("a.txt")
    assert not ws.exists("b.txt")


def test_list_files_filters_by_prefix():
    ws = InMemoryWorkspace({"models/a.py": "", "models/b.py": "", "views/c.xml": ""})
    assert ws.list_files("models/") == ["models/a.py", "models/b.py"]


def test_checkout_sets_the_current_branch():
    ws = InMemoryWorkspace()
    ws.checkout("agent/spec-1500")
    assert ws.branch == "agent/spec-1500"


def test_commit_records_paths_and_message_and_returns_a_sha():
    ws = InMemoryWorkspace()
    ws.write("plan.md", "the plan")
    sha = ws.commit(["plan.md"], "[impl-agent] plan: widget")
    assert sha
    assert ws.commits[-1].paths == ("plan.md",)
    assert ws.commits[-1].message == "[impl-agent] plan: widget"


def test_escalate_records_the_reason_and_details():
    ws = InMemoryWorkspace()
    ws.escalate("spec-refinement-needed", "analyze found a contradiction")
    assert ws.escalations[-1].reason == "spec-refinement-needed"
    assert "contradiction" in ws.escalations[-1].details


def test_in_memory_workspace_satisfies_the_protocol():
    assert isinstance(InMemoryWorkspace(), Workspace)
