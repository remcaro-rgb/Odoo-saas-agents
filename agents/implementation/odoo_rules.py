"""Deterministic Odoo rule checks (Phase C, §4.3).

Pure functions: file content in, `Finding`s out. They never call a model — they
operate on files and diffs, so they enforce the *same* Odoo correctness bar
whichever model OpenCode is running. `coder.py` orchestrates them.
"""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_REQUIRED_MANIFEST_KEYS = ("name", "version", "depends", "data", "license")
_MODEL_NAME = re.compile(r"""_name\s*=\s*['"]([^'"]+)['"]""")
_MUTABLE_DEFAULT = re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\})")


@dataclass(frozen=True)
class Finding:
    """A single rule violation."""

    rule: str
    message: str
    severity: str = "error"  # "error" | "warning"
    path: str = ""


def check_manifest(content: str, path: str = "__manifest__.py") -> list[Finding]:
    """`__manifest__.py` parses as a dict and declares the required keys."""
    try:
        data = ast.literal_eval(content)
    except (ValueError, SyntaxError) as exc:
        return [Finding("manifest.parse", f"__manifest__.py does not parse: {exc}", "error", path)]
    if not isinstance(data, dict):
        return [Finding("manifest.parse", "__manifest__.py is not a dict literal", "error", path)]
    return [
        Finding(
            "manifest.missing_key",
            f"__manifest__.py is missing required key '{key}'",
            "error",
            path,
        )
        for key in _REQUIRED_MANIFEST_KEYS
        if key not in data
    ]


def check_model_access(files: dict[str, str]) -> list[Finding]:
    """Every new `models.Model` has a matching `ir.model.access.csv` entry."""
    model_names: set[str] = set()
    for file_path, content in files.items():
        if file_path.endswith(".py") and "models.Model" in content:
            model_names.update(_MODEL_NAME.findall(content))

    acl = "".join(
        content for p, content in files.items() if p.endswith("ir.model.access.csv")
    )

    findings: list[Finding] = []
    for name in sorted(model_names):
        token = "model_" + name.replace(".", "_")
        if token not in acl:
            findings.append(
                Finding(
                    "security.missing_acl",
                    f"model '{name}' has no ir.model.access.csv entry",
                    "error",
                )
            )
    return findings


def check_view_xml(content: str, path: str = "") -> list[Finding]:
    """A view file is well-formed XML."""
    try:
        ET.fromstring(content)
    except ET.ParseError as exc:
        return [
            Finding("view.malformed_xml", f"view XML is not well-formed: {exc}", "error", path)
        ]
    return []


def check_orm_antipatterns(content: str, path: str = "") -> list[Finding]:
    """Flag raw-SQL string formatting and mutable default arguments."""
    findings: list[Finding] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()

        if ".execute(" in stripped:
            after = stripped.split(".execute(", 1)[1]
            if after.startswith(("f'", 'f"')) or ".format(" in after:
                findings.append(
                    Finding(
                        "orm.raw_sql_formatting",
                        f"raw SQL built with string formatting at line {lineno} — "
                        "pass query parameters instead",
                        "error",
                        path,
                    )
                )

        if _MUTABLE_DEFAULT.search(stripped):
            findings.append(
                Finding(
                    "orm.mutable_default",
                    f"mutable default argument at line {lineno}",
                    "warning",
                    path,
                )
            )
    return findings
