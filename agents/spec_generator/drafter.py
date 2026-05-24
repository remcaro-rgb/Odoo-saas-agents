"""Turn an `Intake` into a drafted spec (Tier 1: design spec only).

The drafter is the LLM-facing layer between intake/classifier and the GitHub
write side. For `feature` intakes it runs `/speckit.specify` and turns the
result into a `DraftedSpec`. Bug intakes (`draft_fix_brief`) ship in Tier 4.

Tier 1 keeps the OpenCode session ownership inside `draft_design_spec` — one
session per issue. Tier 2's `refiner.py` will reuse the session across
reporter Q&A.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .intake import Intake
from .speckit_driver import SpecifyResult, SpecKitFrontDriver

# A safe slug for a spec filename: lowercase letters / digits / hyphens, no
# leading-or-trailing hyphen. Anything else collapses to a single `-`.
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class DraftedSpec:
    """What the drafter produced for a single intake.

    `branch` is the `agent/spec-<issue>-<slug>` branch the spec PR will live
    on. `path` is the repo-relative file path under `docs/superpowers/specs/`.
    `captured_items` and `open_questions` feed the bot's summary comment.
    """

    issue: int
    branch: str
    path: str
    body: str
    captured_items: tuple[str, ...]
    open_questions: tuple[str, ...]
    session_id: str | None


def _slug(text: str, *, limit: int = 40) -> str:
    """Slugify `text` for a branch / filename. Falls back to `"spec"` for empty."""
    lowered = (text or "").lower()
    cleaned = _NON_SLUG.sub("-", lowered).strip("-")
    if not cleaned:
        return "spec"
    return cleaned[:limit].rstrip("-") or "spec"


def feature_branch(issue: int, title: str) -> str:
    """The agent's branch name for a spec PR — stable per issue."""
    return f"agent/spec-{issue:04d}-{_slug(title)}"


def spec_path(issue: int, title: str) -> str:
    """Repo-relative path the design spec is written to."""
    return f"docs/superpowers/specs/spec-{issue:04d}-{_slug(title)}-design.md"


class Drafter:
    """Coordinates `/speckit.specify` -> `DraftedSpec`.

    Stateless. The caller owns the OpenCode session lifecycle so the same
    session can be re-entered by Tier 2's refiner.
    """

    def __init__(self, driver: SpecKitFrontDriver) -> None:
        self.driver = driver

    def draft_design_spec(
        self,
        *,
        intake: Intake,
        session_id: str,
        model: str | None = None,
    ) -> DraftedSpec:
        """Run `/speckit.specify` for a feature intake and shape the result.

        `intake.body` is the directive passed to Spec-Kit; the agent has
        already classified it as `feature`. An empty assistant reply produces
        a `DraftedSpec` with empty `body` so the orchestrator can escalate
        rather than open a PR with no content.
        """
        prompt = self._prompt(intake)
        result: SpecifyResult = self.driver.run_specify(
            session_id, prompt, model=model
        )
        captured = self._summarize_captured(intake, result.spec_text)
        return DraftedSpec(
            issue=intake.issue,
            branch=feature_branch(intake.issue, intake.title),
            path=spec_path(intake.issue, intake.title),
            body=result.spec_text,
            captured_items=captured,
            open_questions=result.open_questions,
            session_id=session_id,
        )

    @staticmethod
    def _prompt(intake: Intake) -> str:
        """Compose the `$ARGUMENTS` body for `/speckit.specify`.

        We prepend a one-line provenance header — "from issue #N (reporter: X)"
        — so the resulting spec.md retains traceability into its origin issue
        without us editing the body after the fact.
        """
        lines = [
            f"From GitHub Issue #{intake.issue} "
            f"(reporter: {intake.reporter}, language: {intake.language}).",
            "",
            f"# {intake.title}" if intake.title else "(untitled issue)",
            "",
            intake.body.strip() or "(empty body — the reporter did not provide details)",
        ]
        if intake.attachments:
            lines += ["", "Attachments referenced:"]
            lines += [f"- {url}" for url in intake.attachments]
        return "\n".join(lines)

    @staticmethod
    def _summarize_captured(
        intake: Intake, spec_text: str
    ) -> tuple[str, ...]:
        """The `captured_items` shown to the reporter in the summary comment.

        Tier 1 keeps this simple: report what we know from the *intake* (so
        the reporter sees we read their issue correctly), not what the spec
        text claims (the spec text is what they're about to review anyway).
        """
        items: list[str] = []
        items.append(f"issue #{intake.issue} — {intake.title or '(untitled)'}")
        if intake.attachments:
            items.append(
                f"{len(intake.attachments)} attachment(s) referenced"
            )
        if intake.kind_hint:
            items.append(f"routing hint: `{intake.kind_hint}`")
        # The spec text itself is the source of truth for everything else; we
        # leave that for the reporter to inspect via the PR diff link.
        if spec_text:
            length = len(spec_text)
            items.append(
                f"drafted spec ({length} chars; see PR for full text)"
            )
        return tuple(items)
