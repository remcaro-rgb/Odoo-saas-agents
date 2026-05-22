"""Drive Spec-Kit's slash-commands inside a headless OpenCode session (Phase B).

`SpecKitDriver` issues `/speckit.plan`, `/speckit.tasks`, `/speckit.analyze`, and
`/speckit.implement` against an OpenCode session and parses what comes back. It
takes any object with the OpenCode client surface (`run_command`, ...), so the
unit tests drive it with a fake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A finding marker in an /analyze report: a CRITICAL/HIGH severity label in
# structural position — i.e. followed by ":", "|" (table cell), "]" or "-".
# This deliberately does NOT match the words in prose, so a clean report that
# says "no critical issues found" is not falsely flagged as incoherent.
_FINDING = re.compile(r"\b(?:CRITICAL|HIGH)\b\s*[:\-\]|]", re.IGNORECASE)


@dataclass
class AnalyzeResult:
    """The outcome of `/speckit.analyze` — does the spec/plan/tasks set cohere?"""

    coherent: bool
    findings: list[str]
    raw: str


def _text_of(result: dict[str, Any]) -> str:
    """Concatenate the text parts of an OpenCode command/message result."""
    chunks: list[str] = []
    for part in result.get("parts", []) or []:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text", "")))
        elif isinstance(part, str):
            chunks.append(part)
    return "\n".join(chunks)


class SpecKitDriver:
    """Issues the Spec-Kit commands and interprets their output."""

    PLAN = "speckit.plan"
    TASKS = "speckit.tasks"
    ANALYZE = "speckit.analyze"
    IMPLEMENT = "speckit.implement"

    def __init__(self, client: Any) -> None:
        self.client = client

    def run_plan(self, session_id: str, arguments: str = "") -> dict[str, Any]:
        return self.client.run_command(session_id, self.PLAN, arguments)

    def run_tasks(self, session_id: str, arguments: str = "") -> dict[str, Any]:
        return self.client.run_command(session_id, self.TASKS, arguments)

    def run_analyze(self, session_id: str) -> AnalyzeResult:
        """Run `/speckit.analyze` and classify the report as coherent or not.

        Empty output is treated as *not* coherent: a missing /analyze report is
        not an implicit pass — the orchestrator must escalate, not proceed.
        """
        result = self.client.run_command(session_id, self.ANALYZE, "")
        text = _text_of(result)
        if not text.strip():
            return AnalyzeResult(
                coherent=False,
                findings=["/analyze produced no output"],
                raw="",
            )
        findings = [
            line.strip() for line in text.splitlines() if _FINDING.search(line)
        ]
        return AnalyzeResult(coherent=not findings, findings=findings, raw=text)

    def run_implement(
        self,
        session_id: str,
        arguments: str = "",
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Run `/speckit.implement`. ``model`` routes hard tasks to the frontier
        model (plan decision 6)."""
        return self.client.run_command(
            session_id, self.IMPLEMENT, arguments, model=model
        )
