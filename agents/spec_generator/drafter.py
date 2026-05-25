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
from .repro import ReproResult
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
    `title_prefix` (Tier 5) is the optional tag the dup detector prepends —
    e.g. ``[possible-dup]``; ``app._intake_title_from_result`` picks it up
    when building the PR title.
    """

    issue: int
    branch: str
    path: str
    body: str
    captured_items: tuple[str, ...]
    open_questions: tuple[str, ...]
    session_id: str | None
    title_prefix: str = ""


def _render_fix_brief(intake: Intake, repro: ReproResult) -> str:
    """Render the fix-brief markdown body.

    Matches the shape of `docs/superpowers/specs/_TEMPLATE-fix.md` (which
    the Implementation Agent's `/speckit.fix` already drives against). The
    template is deliberately minimal — the Implementation Agent fills in
    the actual fix; the Spec Generator's job is to package the *bug
    context* (steps, expected, actual, attachments, repro logs) cleanly.
    """
    parts: list[str] = [
        f"# Fix-brief — {intake.title or '(untitled bug)'}",
        "",
        f"_From GitHub Issue #{intake.issue} (reporter: {intake.reporter})._",
        "",
        "## Repro outcome",
        f"- **Status:** `{repro.outcome.value}`",
    ]
    if repro.summary:
        parts.append(f"- **Summary:** {repro.summary}")
    if repro.screenshots:
        parts.append("- **Screenshots:**")
        parts += [f"  - {url}" for url in repro.screenshots]
    parts += ["", "## Issue body", "", intake.body.strip() or "(empty)"]
    if intake.attachments:
        parts += ["", "## Reporter attachments"]
        parts += [f"- {url}" for url in intake.attachments]
    if repro.logs:
        # Trim the tail to keep the brief readable — 2 KB is plenty for the
        # Implementation Agent to pattern-match on.
        tail = repro.logs[-2000:]
        parts += [
            "",
            "## Playwright log tail",
            "",
            "```",
            tail.rstrip(),
            "```",
        ]
    parts += [
        "",
        "## Acceptance",
        "- The reproduction steps no longer trigger the symptom.",
        "- A regression test pins the fix.",
        "",
    ]
    return "\n".join(parts) + "\n"


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


def fix_brief_path(issue: int, title: str) -> str:
    """Repo-relative path the fix-brief is written to (Tier 4)."""
    return f"docs/superpowers/specs/fix-{issue:04d}-{_slug(title)}-fix.md"


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

    def draft_fix_brief(
        self,
        *,
        intake: Intake,
        repro: ReproResult,
    ) -> DraftedSpec:
        """Deterministic fix-brief template fill-in for a confirmed bug (Tier 4).

        Unlike the design-spec path, fix-briefs do NOT call `/speckit.specify`
        — the structure is opinionated enough that a template + LLM fill-in
        would just be an expensive copy of the same content. The
        Implementation Agent then drives `/speckit.fix` against this brief.
        """
        body = _render_fix_brief(intake, repro)
        captured: tuple[str, ...] = (
            f"issue #{intake.issue} — {intake.title or '(untitled)'}",
            f"reproduction: {repro.outcome.value}",
        )
        if repro.screenshots:
            captured = captured + (
                f"{len(repro.screenshots)} screenshot(s) captured by Playwright",
            )
        return DraftedSpec(
            issue=intake.issue,
            branch=feature_branch(intake.issue, intake.title),
            path=fix_brief_path(intake.issue, intake.title),
            body=body,
            captured_items=captured,
            open_questions=(),
            session_id=None,
        )

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
