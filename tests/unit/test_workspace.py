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


# -- apply_session_diff (Tier-4 container -> Action workspace sync) ------------
def test_apply_session_diff_with_added_file_writes_the_content():
    ws = InMemoryWorkspace()
    n = ws.apply_session_diff(
        [{"file": "models/new.py", "status": "added", "content": "class X: pass\n"}]
    )
    assert n == 1
    assert ws.files["models/new.py"] == "class X: pass\n"


def test_apply_session_diff_with_modified_file_updates_the_content():
    ws = InMemoryWorkspace({"x.py": "old"})
    n = ws.apply_session_diff(
        [{"file": "x.py", "status": "modified", "content": "new"}]
    )
    assert n == 1
    assert ws.files["x.py"] == "new"


def test_apply_session_diff_with_deleted_status_removes_the_file():
    ws = InMemoryWorkspace({"x.py": "old"})
    n = ws.apply_session_diff([{"file": "x.py", "status": "deleted"}])
    assert n == 1
    assert "x.py" not in ws.files


def test_apply_session_diff_with_empty_list_returns_zero():
    ws = InMemoryWorkspace()
    assert ws.apply_session_diff([]) == 0


def test_apply_session_diff_drops_entries_targeting_guardrail_paths():
    """Defense in depth — provisioning's sparse-checkout is the first line of
    defense (the container can't read protected paths). `apply_session_diff`
    cannot trust the diff payload to be honest, so it filters the guardrails
    again before writing."""
    ws = InMemoryWorkspace({"safe.txt": "ok"})
    n = ws.apply_session_diff(
        [
            {
                "file": ".github/workflows/evil.yml",
                "status": "added",
                "content": "name: evil\n",
            },
            {
                "file": "saas_tenant_gate/security/bypass.xml",
                "status": "modified",
                "content": "<weakened />",
            },
            {
                "file": "infra/main.tf",
                "status": "added",
                "content": "resource …",
            },
            {"file": "Dockerfile", "status": "modified", "content": "FROM evil"},
            {
                "file": "custom-addons/widget/__init__.py",
                "status": "added",
                "content": "from . import models",
            },
        ]
    )
    # Only the legitimate addon entry made it through.
    assert n == 1
    assert "custom-addons/widget/__init__.py" in ws.files
    assert ".github/workflows/evil.yml" not in ws.files
    assert "saas_tenant_gate/security/bypass.xml" not in ws.files
    assert "infra/main.tf" not in ws.files
    assert "Dockerfile" not in ws.files
