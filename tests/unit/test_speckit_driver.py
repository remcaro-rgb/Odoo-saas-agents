"""Unit tests for SpecKitDriver (Phase B) — runs against the fake OpenCode client."""

from agents.implementation.speckit_driver import AnalyzeResult, SpecKitDriver


def test_run_plan_issues_the_plan_command(fake_client):
    SpecKitDriver(fake_client).run_plan("sess-1")
    assert fake_client.commands[-1]["command"] == "speckit.plan"
    assert fake_client.commands[-1]["session_id"] == "sess-1"


def test_run_tasks_issues_the_tasks_command(fake_client):
    SpecKitDriver(fake_client).run_tasks("sess-1")
    assert fake_client.commands[-1]["command"] == "speckit.tasks"


def test_run_analyze_is_coherent_when_the_report_is_clean(fake_client):
    fake_client.set_command_result(
        "speckit.analyze",
        {"parts": [{"type": "text", "text": "All artifacts are consistent."}]},
    )
    result = SpecKitDriver(fake_client).run_analyze("sess-1")
    assert isinstance(result, AnalyzeResult)
    assert result.coherent is True
    assert result.findings == []


def test_run_analyze_flags_a_critical_finding(fake_client):
    fake_client.set_command_result(
        "speckit.analyze",
        {
            "parts": [
                {
                    "type": "text",
                    "text": "Report\n"
                    "CRITICAL: spec needs negative balances but the model is unsigned\n"
                    "End",
                }
            ]
        },
    )
    result = SpecKitDriver(fake_client).run_analyze("sess-1")
    assert result.coherent is False
    assert any("negative balances" in finding for finding in result.findings)


def test_run_implement_issues_the_command_with_the_routed_model(fake_client):
    SpecKitDriver(fake_client).run_implement(
        "sess-1", "T001", model="anthropic/claude-sonnet-4-6"
    )
    cmd = fake_client.commands[-1]
    assert cmd["command"] == "speckit.implement"
    assert cmd["arguments"] == "T001"
    assert cmd["model"] == "anthropic/claude-sonnet-4-6"


def test_run_analyze_treats_a_no_issues_report_as_coherent(fake_client):
    """A clean report that mentions 'no inconsistencies' must not be flagged."""
    fake_client.set_command_result(
        "speckit.analyze",
        {
            "parts": [
                {
                    "type": "text",
                    "text": "Analysis complete. No inconsistencies found. "
                    "No contradictions between spec and plan. No critical issues.",
                }
            ]
        },
    )
    result = SpecKitDriver(fake_client).run_analyze("sess-1")
    assert result.coherent is True
    assert result.findings == []


def test_run_analyze_treats_empty_output_as_not_coherent(fake_client):
    """A missing /analyze report is not an implicit pass."""
    fake_client.set_command_result("speckit.analyze", {"parts": []})
    result = SpecKitDriver(fake_client).run_analyze("sess-1")
    assert result.coherent is False
    assert result.findings
