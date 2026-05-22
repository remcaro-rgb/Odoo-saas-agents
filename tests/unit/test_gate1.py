"""Unit tests for the Gate-1 build/lint/test quality gate (Phase C)."""

from agents.implementation.gate1 import (
    CheckRunner,
    FakeCheckRunner,
    Gate1,
    SubprocessCheckRunner,
)


def test_fake_check_runner_satisfies_the_protocol():
    assert isinstance(FakeCheckRunner(), CheckRunner)


def test_gate1_runs_every_configured_check():
    runner = FakeCheckRunner()
    Gate1(runner).run("custom-addons/widget")
    # the three default checks — lint, build, tests — each issued one command.
    assert len(runner.commands) == 3


def test_gate1_passes_when_all_checks_exit_zero():
    result = Gate1(FakeCheckRunner()).run("custom-addons/widget")
    assert result.passed
    assert result.failures == ()


def test_gate1_fails_when_a_check_exits_nonzero():
    runner = FakeCheckRunner()
    runner.set_result("ruff check", 1, "widget/models/x.py:3:1: F401 unused import")
    result = Gate1(runner).run("custom-addons/widget")
    assert not result.passed
    assert [check.name for check in result.failures] == ["lint"]
    assert "F401" in result.logs


def test_gate1_logs_only_carry_the_failed_checks():
    runner = FakeCheckRunner()
    runner.set_result("ruff check", 1, "LINT_FAILURE_TEXT")
    runner.set_result("test-enable", 0, "PASSING_TEST_TEXT")
    result = Gate1(runner).run("custom-addons/widget")
    assert "LINT_FAILURE_TEXT" in result.logs
    assert "PASSING_TEST_TEXT" not in result.logs


def test_gate1_substitutes_the_addon_path_and_module_name():
    runner = FakeCheckRunner()
    Gate1(runner).run("custom-addons/partner_notes/")
    joined = " ".join(runner.commands)
    assert "custom-addons/partner_notes" in joined  # {addon}
    assert "partner_notes" in joined                # {module}, from the last path segment


def test_gate1_accepts_a_custom_check_set():
    runner = FakeCheckRunner()
    runner.set_result("flake8", 1, "boom")
    result = Gate1(runner, checks=(("lint", "flake8 {addon}"),)).run(
        "custom-addons/widget"
    )
    assert len(result.checks) == 1
    assert not result.passed


def test_subprocess_check_runner_reports_a_missing_tool_as_a_failed_check():
    """A missing executable is a failed check (non-zero exit), never a crash."""
    exit_code, output = SubprocessCheckRunner().run("__no_such_gate1_tool__ --version")
    assert exit_code != 0
    assert "__no_such_gate1_tool__" in output
