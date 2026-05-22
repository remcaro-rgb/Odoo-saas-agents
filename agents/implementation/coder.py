"""The Odoo specialization layer — `coder.py` (Phase C, §4.3).

A hand-written, deterministic Odoo layer that *wraps* OpenCode. It does not run a
generic coding loop (that is OpenCode's job) — it scaffolds Odoo boilerplate,
injects Odoo context, validates the result against Odoo rules, and re-prompts
OpenCode with precise corrections. It is model-agnostic: it talks to OpenCode via
the driver and inspects files, never a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gate1 import Gate1, Gate1Result
from .odoo_rules import (
    Finding,
    check_manifest,
    check_model_access,
    check_orm_antipatterns,
    check_view_xml,
)
from .speckit_driver import SpecKitDriver
from .workspace import Workspace

DEFAULT_FRONTIER_MODEL = "anthropic/claude-sonnet-4-6"

# `\b` keeps a field like `display_name = 'X'` from being misread as a model.
_MODEL_NAME = re.compile(r"""\b_name\s*=\s*['"]([^'"]+)['"]""")

_MANIFEST_TEMPLATE = """{{
    'name': {name!r},
    'version': '19.0.1.0.0',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
    ],
    'license': 'LGPL-3',
    'installable': True,
}}
"""

_ACL_HEADER = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
)


@dataclass
class ImplementResult:
    """Outcome of the Phase-C implement loop."""

    status: str  # "implemented" | "escalated"
    attempts: int
    findings: list[Finding] = field(default_factory=list)
    gate: Gate1Result | None = None  # the Gate-1 outcome, when a gate ran


# -- (1) deterministic scaffolding -------------------------------------------
def scaffold(addon_name: str, models: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """Generate correct-by-construction Odoo boilerplate for a new addon.

    Returns ``{path: content}`` — never left to a model to get wrong.
    """
    base = f"custom-addons/{addon_name}"
    files: dict[str, str] = {
        f"{base}/__manifest__.py": _MANIFEST_TEMPLATE.format(
            name=addon_name.replace("_", " ").title()
        ),
        f"{base}/__init__.py": "from . import models\n",
        f"{base}/models/__init__.py": "",
    }
    acl_lines = [_ACL_HEADER]
    for model in models:
        token = "model_" + model.replace(".", "_")
        ident = "access_" + model.replace(".", "_")
        acl_lines.append(f"{ident},{ident},{token},base.group_user,1,1,1,1\n")
    files[f"{base}/security/ir.model.access.csv"] = "".join(acl_lines)
    return files


# -- (2) Odoo context injection ----------------------------------------------
def inject_context(workspace: Workspace, addon_prefix: str) -> str:
    """Summarize an addon's current state, to prime the OpenCode session."""
    paths = workspace.list_files(addon_prefix)
    models: list[str] = []
    for path in paths:
        if path.endswith(".py"):
            models.extend(_MODEL_NAME.findall(workspace.read(path)))
    return "\n".join(
        [
            f"Odoo addon context for {addon_prefix}:",
            f"- files: {', '.join(p.rsplit('/', 1)[-1] for p in sorted(paths))}",
            f"- existing models: {', '.join(sorted(models)) or 'none'}",
        ]
    )


# -- (3) deterministic Odoo validation ---------------------------------------
def validate_odoo(files: dict[str, str]) -> list[Finding]:
    """Run every deterministic Odoo rule check over an addon's files."""
    findings: list[Finding] = []
    for path, content in files.items():
        if path.endswith("__manifest__.py"):
            findings += check_manifest(content, path)
        elif path.endswith(".py"):
            findings += check_orm_antipatterns(content, path)
        elif path.endswith(".xml") and "/views/" in path:
            findings += check_view_xml(content, path)
    findings += check_model_access(files)
    return findings


# -- (4) Odoo-aware corrective re-prompt -------------------------------------
def correct(findings: list[Finding]) -> str:
    """Turn validation findings into a precise corrective re-prompt for OpenCode."""
    lines = [
        "The implementation has Odoo-rule violations. Fix exactly these — nothing else:"
    ]
    for finding in findings:
        where = f" ({finding.path})" if finding.path else ""
        lines.append(f"- [{finding.rule}]{where} {finding.message}")
    return "\n".join(lines)


def correct_gate1(result: Gate1Result) -> str:
    """Turn Gate-1 check failures into a precise corrective re-prompt for OpenCode."""
    lines = ["Gate 1 (build / lint / tests) failed. Fix exactly these — nothing else:"]
    for check in result.failures:
        lines.append(f"\n## {check.name} check failed\n{check.output}".rstrip())
    return "\n".join(lines)


class Coder:
    """Drives the Phase-C loop: scaffold -> prime -> implement -> validate ->
    Gate 1 -> correct, then escalate.

    Workspace contract: the `Workspace` given here and the OpenCode session
    passed to `implement()` MUST be backed by the *same* working tree. OpenCode's
    `/implement` writes files into its own checkout; `implement()` then validates
    those files by reading them back through this `Workspace`. If the two are not
    one tree the validator sees stale state and the loop is meaningless.
    `InMemoryWorkspace` simulates this for tests; in production the OpenCode
    service must be pointed at the same git checkout the `GitWorkspace` manages.
    """

    def __init__(
        self,
        driver: SpecKitDriver,
        workspace: Workspace,
        *,
        frontier_model: str = DEFAULT_FRONTIER_MODEL,
        max_retries: int = 3,
        gate1: Gate1 | None = None,
    ) -> None:
        self.driver = driver
        self.workspace = workspace
        self.frontier_model = frontier_model
        self.max_retries = max_retries
        self.gate1 = gate1

    def _addon_files(self, addon_prefix: str) -> dict[str, str]:
        return {
            path: self.workspace.read(path)
            for path in self.workspace.list_files(addon_prefix)
        }

    def implement(self, session_id: str, addon_prefix: str) -> ImplementResult:
        """Scaffold (if new) -> prime -> implement -> validate -> Gate 1.

        A brand-new addon (an empty `addon_prefix`) is first given correct-by-
        construction Odoo boilerplate, so OpenCode starts from a valid manifest.
        The session is primed with the addon's Odoo context, then each attempt
        runs the deterministic Odoo rules and — once they are clean — Gate 1
        (build/lint/tests, when a `gate1` is configured). An Odoo-rule or Gate-1
        failure drives a corrective re-prompt; persistent failure past
        `max_retries` escalates.
        """
        # Brand-new addon -> lay down correct-by-construction boilerplate first.
        if not self.workspace.list_files(addon_prefix):
            addon_name = addon_prefix.rstrip("/").rsplit("/", 1)[-1]
            for path, content in scaffold(addon_name).items():
                self.workspace.write(path, content)

        # Prime the OpenCode session with the addon's current Odoo context.
        self.driver.client.send_message(
            session_id, inject_context(self.workspace, addon_prefix)
        )

        # Initial implementation pass — routine work on the default (OpenCode Go) model.
        self.driver.run_implement(session_id)

        for attempt in range(self.max_retries + 1):
            findings = validate_odoo(self._addon_files(addon_prefix))
            errors = [f for f in findings if f.severity == "error"]

            if errors:
                correction, stage, gate = correct(errors), "coder.py validation", None
            else:
                # Odoo rules are clean — run Gate 1 (build/lint/tests in agentlab).
                gate = self.gate1.run(addon_prefix) if self.gate1 is not None else None
                if gate is None or gate.passed:
                    return ImplementResult("implemented", attempt, findings, gate)
                correction, stage = correct_gate1(gate), "Gate 1"

            if attempt == self.max_retries:
                self.workspace.escalate(
                    "needs-human",
                    f"{stage} still failing after {attempt} corrective retries",
                )
                return ImplementResult("escalated", attempt, findings, gate)
            # Corrective re-prompt — a hard task: route the retry to the frontier
            # model (plan decision 6).
            self.driver.run_implement(
                session_id, correction, model=self.frontier_model
            )
        raise AssertionError("unreachable")  # pragma: no cover
