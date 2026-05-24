"""Bug reproduction on agentlab via Playwright (Tier 4).

When the classifier routes an intake to `IntakeKind.BUG`, the orchestrator
asks `Reproducer.attempt` whether the issue's reproduction steps actually
break a fresh agentlab tenant. The outcome is one of:

  - `repro_confirmed` — the steps reproduced the symptom; the orchestrator
    runs `draft_fix_brief` with the logs + screenshots.
  - `needs_repro_info` — the steps were ambiguous / incomplete; the
    orchestrator posts targeted questions and does not draft.
  - `needs_fixture` — the issue references customer data the reporter can't
    sanitise on their own; the orchestrator routes to `security-leads`.
  - `agentlab_unavailable` — infrastructure failure; the orchestrator
    escalates to `needs-human` with the agentlab error included.

The actual Playwright invocation lives behind an `AgentlabClient` Protocol so
unit tests run fully offline. The production `HttpShimAgentlabClient` makes a
small HTTP call to an agentlab-side shim that runs the Playwright container
and returns a structured ``ReproOutcome`` JSON.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .intake import Intake


class ReproOutcome(StrEnum):
    """The Playwright runner's verdict."""

    REPRO_CONFIRMED = "repro_confirmed"
    NEEDS_REPRO_INFO = "needs_repro_info"
    NEEDS_FIXTURE = "needs_fixture"
    AGENTLAB_UNAVAILABLE = "agentlab_unavailable"


@dataclass
class ReproResult:
    """Structured outcome of `Reproducer.attempt`."""

    outcome: ReproOutcome
    summary: str = ""
    logs: str = ""              # truncated tail of agentlab stdout/stderr
    screenshots: tuple[str, ...] = ()  # URLs the shim uploads
    questions: tuple[str, ...] = ()    # populated for NEEDS_REPRO_INFO
    raw: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class AgentlabClient(Protocol):
    """The agentlab-side surface the reproducer needs."""

    def attempt_repro(self, *, intake: Intake) -> ReproResult: ...


# Markers a reporter typically uses for repro steps. A body that's missing
# both is the classic "needs more info" case.
_REPRO_STEPS = re.compile(
    r"(?:^|\n)\s*(?:repro|reproduction|steps?\s+to\s+reproduce|str)\s*[:\-]",
    re.IGNORECASE,
)
_EXPECTED = re.compile(
    r"(?:^|\n)\s*(?:expected|expected\s+behaviour|expected\s+behavior)\s*[:\-]",
    re.IGNORECASE,
)
_ACTUAL = re.compile(
    r"(?:^|\n)\s*(?:actual|actual\s+result|what\s+happened)\s*[:\-]",
    re.IGNORECASE,
)
_REFERENCES_CUSTOMER_DATA = re.compile(
    r"\b(?:tenant\s+id|customer\s+data|production\s+db|prod\s+database|"
    r"see\s+our\s+(?:db|database))\b",
    re.IGNORECASE,
)


def classify_repro_readiness(intake: Intake) -> tuple[ReproOutcome | None, list[str]]:
    """Cheap pre-flight: is the issue body even ready to be reproduced?

    Returns `(skip_outcome, questions)`:
      - If the issue references customer data the reporter can't sanitise on
        their own -> `NEEDS_FIXTURE` + empty questions list (the bot posts
        a routing notice, not questions).
      - If the body has no obvious reproduction steps AND no expected/actual
        sections -> `NEEDS_REPRO_INFO` + the bot's targeted-question list.
      - Otherwise -> `(None, [])` so the orchestrator proceeds to the
        Playwright runner.
    """
    body = intake.body or ""
    if _REFERENCES_CUSTOMER_DATA.search(body):
        return ReproOutcome.NEEDS_FIXTURE, []

    has_steps = bool(_REPRO_STEPS.search(body))
    has_expected = bool(_EXPECTED.search(body))
    has_actual = bool(_ACTUAL.search(body))
    if not has_steps and not (has_expected and has_actual):
        questions: list[str] = []
        if not has_steps:
            questions.append(
                "Can you list the exact steps that trigger the bug? "
                "(URLs clicked, menus opened, fields filled, etc.)"
            )
        if not has_expected:
            questions.append("What did you expect to happen?")
        if not has_actual:
            questions.append(
                "What happened instead? (paste any error message or screenshot)"
            )
        return ReproOutcome.NEEDS_REPRO_INFO, questions

    return None, []


class FakeAgentlabClient:
    """In-memory `AgentlabClient` for unit tests.

    `outcome_map` keys are issue numbers; default is `REPRO_CONFIRMED`.
    """

    def __init__(self, outcome_map: dict[int, ReproResult] | None = None) -> None:
        self.outcome_map = dict(outcome_map or {})
        self.calls: list[int] = []

    def attempt_repro(self, *, intake: Intake) -> ReproResult:
        self.calls.append(intake.issue)
        return self.outcome_map.get(
            intake.issue,
            ReproResult(
                outcome=ReproOutcome.REPRO_CONFIRMED,
                summary="fake repro confirmed",
            ),
        )


@dataclass
class HttpShimAgentlabClient:
    """Talks to an agentlab-side HTTP shim that runs the Playwright container.

    The shim contract (provisioned in Tier 4):

      POST <base_url>/repro
      body:  {"issue": 12, "title": "...", "body": "...", "attachments": [...]}
      reply: {"outcome": "repro_confirmed",
              "summary": "...", "logs": "...", "screenshots": [...]}

    Authentication is a static shared secret `Authorization: Bearer <token>`.
    Timeouts default to 6 minutes — Playwright on agentlab is multi-step.
    """

    base_url: str
    token: str
    timeout_seconds: float = 360.0

    def attempt_repro(self, *, intake: Intake) -> ReproResult:
        payload = {
            "issue": intake.issue,
            "title": intake.title,
            "body": intake.body,
            "attachments": list(intake.attachments),
            "reporter": intake.reporter,
        }
        try:
            req = urllib.request.Request(
                url=f"{self.base_url.rstrip('/')}/repro",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return ReproResult(
                outcome=ReproOutcome.AGENTLAB_UNAVAILABLE,
                summary=f"shim unreachable: {type(exc).__name__}: {exc}",
            )

        try:
            outcome = ReproOutcome(raw.get("outcome", "agentlab_unavailable"))
        except ValueError:
            outcome = ReproOutcome.AGENTLAB_UNAVAILABLE
        screenshots = tuple(raw.get("screenshots") or ())
        questions = tuple(raw.get("questions") or ())
        return ReproResult(
            outcome=outcome,
            summary=str(raw.get("summary") or ""),
            logs=str(raw.get("logs") or ""),
            screenshots=screenshots,
            questions=questions,
            raw=raw,
        )


class Reproducer:
    """Glue: pre-flight + delegate to the `AgentlabClient`."""

    def __init__(self, client: AgentlabClient) -> None:
        self.client = client

    def attempt(self, intake: Intake) -> ReproResult:
        early, questions = classify_repro_readiness(intake)
        if early is ReproOutcome.NEEDS_FIXTURE:
            return ReproResult(
                outcome=ReproOutcome.NEEDS_FIXTURE,
                summary=(
                    "issue body references customer data; routing to "
                    "security-leads for a sanitised fixture"
                ),
            )
        if early is ReproOutcome.NEEDS_REPRO_INFO:
            return ReproResult(
                outcome=ReproOutcome.NEEDS_REPRO_INFO,
                summary="issue body is missing reproduction details",
                questions=tuple(questions),
            )
        return self.client.attempt_repro(intake=intake)
