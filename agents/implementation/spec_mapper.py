"""Map a project-format spec onto Spec-Kit's `spec.md` shape (Phase B, decision 1.7).

The project keeps its own `_TEMPLATE-design.md` / `_TEMPLATE-fix.md` structure;
Spec-Kit's commands expect a `spec.md`. Rather than fork Spec-Kit's templates, the
driver maps between the two here — heading-based, deterministic, model-agnostic.
"""

from __future__ import annotations

import re

from .events import SpecKind

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_sections(md: str) -> dict[str, str]:
    """Split markdown into ``{heading_text: body}``.

    The heading text drops the leading ``#`` marks. Content before the first
    heading is ignored. Bodies are stripped.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in md.splitlines():
        match = _HEADING.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(body).strip()
            current = match.group(2).strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body).strip()
    return sections


def _find(sections: dict[str, str], *needles: str) -> str:
    """Return the body of the first section whose heading contains any needle."""
    for heading, body in sections.items():
        low = heading.lower()
        if any(needle in low for needle in needles):
            return body
    return ""


def to_speckit(project_md: str) -> str:
    """Render a project-format spec as a Spec-Kit `spec.md`."""
    sections = parse_sections(project_md)
    title = next(iter(sections), "Feature")
    goal = _find(sections, "goal")
    non_goals = _find(sections, "non-goal")
    test_plan = _find(sections, "test plan", "test")

    parts = [
        f"# Feature Specification: {title}",
        "",
        "> Mapped from a project spec by the Implementation Agent's spec mapper.",
        "",
        "## User Scenarios & Testing",
        "",
        goal or "_No goal section found in the source spec._",
    ]
    if non_goals:
        parts += ["", "### Out of scope", "", non_goals]
    parts += [
        "",
        "## Requirements",
        "",
        "Functional requirements derived from the goal above; "
        "refine with `/speckit.clarify`.",
        "",
        "## Success Criteria",
        "",
        test_plan or "_No test plan in the source spec — define measurable criteria._",
    ]
    return "\n".join(parts) + "\n"


def detect_spec_kind(path: str, content: str = "") -> SpecKind:
    """Classify a spec as a design spec or a fix-brief.

    The project convention is `<slug>-fix.md` for fix-briefs; everything else is
    treated as a design spec. ``content`` is reserved for future content sniffing.
    """
    name = (path or "").lower()
    if name.endswith("-fix.md") or "-fix." in name:
        return SpecKind.FIX
    return SpecKind.DESIGN
