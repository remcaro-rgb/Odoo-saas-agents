"""Gate-1 — the build / lint / test quality gate (Phase C, design §5.1).

After the deterministic Odoo-rule checks (`odoo_rules` / `coder.validate_odoo`)
pass, Gate-1 actually *builds, lints and tests* the generated addon in agentlab —
the heavier, environment-backed gate. It is deterministic: it re-runs the tests
itself, so a model cannot pass off hallucinated test results.

`CheckRunner` is a Protocol seam (matching `Workspace` / `FlyClient`): unit tests
run against `FakeCheckRunner`; `SubprocessCheckRunner` actually executes the
commands (in agentlab — integration-verified, not unit-tested). The check command
set is injectable, so the exact `odoo` invocation can track the agentlab setup.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Default Gate-1 checks for an Odoo addon. `{addon}` is the addon path, `{module}`
# its module name, `{db}` the agentlab test database. `build` and `tests` run
# Odoo (in agentlab); `lint` is static. Override via `Gate1(checks=...)`.
_DEFAULT_CHECKS: tuple[tuple[str, str], ...] = (
    ("lint", "ruff check {addon}"),
    ("build", "odoo --stop-after-init --without-demo=all -d {db} -i {module}"),
    (
        "tests",
        "odoo --stop-after-init --test-enable --test-tags /{module} "
        "-d {db} -i {module}",
    ),
)


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one Gate-1 check."""

    name: str         # "lint" | "build" | "tests"
    passed: bool
    output: str = ""  # the check command's combined stdout/stderr


@dataclass(frozen=True)
class Gate1Result:
    """The outcome of a full Gate-1 run."""

    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """True only if every check passed."""
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """The checks that failed."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def logs(self) -> str:
        """Combined output of the *failed* checks — for an escalation comment
        or a corrective re-prompt."""
        return "\n\n".join(
            f"[{check.name}]\n{check.output}".rstrip() for check in self.failures
        )


@runtime_checkable
class CheckRunner(Protocol):
    """Runs one Gate-1 check command; returns ``(exit_code, combined_output)``."""

    def run(self, command: str) -> tuple[int, str]: ...


class FakeCheckRunner:
    """In-memory `CheckRunner` — records commands, returns canned results.

    By default every command "passes" (exit 0); `set_result` overrides a command
    (matched by substring) to fail or to return specific output.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._results: dict[str, tuple[int, str]] = {}

    def set_result(
        self, command_substring: str, exit_code: int, output: str = ""
    ) -> None:
        self._results[command_substring] = (exit_code, output)

    def run(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        for substring, result in self._results.items():
            if substring in command:
                return result
        return (0, "")


class SubprocessCheckRunner:
    """A `CheckRunner` that runs the check via `subprocess` — integration-verified
    against agentlab, not unit-tested (a thin process wrapper)."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd

    def run(self, command: str) -> tuple[int, str]:
        try:
            result = subprocess.run(  # noqa: S603 — commands are config, not agent input
                shlex.split(command),
                cwd=self.cwd,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            # A missing executable (ruff / odoo not on PATH) is a failed check,
            # not a crash — 127 is the conventional "command not found" code.
            return (127, f"{command!r} could not be run: {exc}")
        return (result.returncode, result.stdout + result.stderr)


class Gate1:
    """Runs the build / lint / test checks on a generated addon over a
    `CheckRunner`. A check passes when its command exits 0."""

    def __init__(
        self,
        runner: CheckRunner,
        *,
        checks: tuple[tuple[str, str], ...] = _DEFAULT_CHECKS,
        test_db: str = "agentlab_gate1",
    ) -> None:
        self.runner = runner
        self.checks = checks
        self.test_db = test_db

    def run(self, addon_path: str) -> Gate1Result:
        """Run every configured check against the addon at `addon_path`, in order."""
        addon = addon_path.rstrip("/")
        module = addon.rsplit("/", 1)[-1]
        results: list[CheckResult] = []
        for name, template in self.checks:
            command = template.format(addon=addon, module=module, db=self.test_db)
            exit_code, output = self.runner.run(command)
            # A check passes only on exit 0 — any non-zero (incl. signals) fails it.
            results.append(CheckResult(name, exit_code == 0, output))
        return Gate1Result(tuple(results))
