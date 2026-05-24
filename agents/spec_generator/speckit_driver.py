"""Drive Spec-Kit's front-half slash-commands inside a headless OpenCode session.

`SpecKitFrontDriver` issues `/speckit.specify` (turn an intake into a spec.md)
and `/speckit.clarify` (interactively resolve ambiguities) against an OpenCode
session. Sibling of `agents.implementation.speckit_driver.SpecKitDriver` — that
one runs the *back* half (`/plan`, `/tasks`, `/analyze`, `/implement`); this one
runs the *front* half.

The Tier-1 surface is intentionally narrow: kick off `/speckit.specify`, read
back the spec text, and parse out open-question markers. Reporter Q&A
(`/speckit.clarify`) ships in Tier 2 — see the `run_clarify` stub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The `/speckit.specify` template emits sentinel markers for items it
# couldn't pin down from the input. We surface these as open questions in the
# reporter's summary comment. Stock Spec-Kit uses `[NEEDS CLARIFICATION: ...]`
# and `[NEEDS INPUT: ...]` — we accept both spellings.
_OPEN_QUESTION = re.compile(
    r"\[\s*NEEDS\s+(?:CLARIFICATION|INPUT)\s*:\s*([^\]]+?)\s*\]",
    re.IGNORECASE,
)

# Stub headings the template writes when it has nothing to fill in — captured
# items above each become the `captured_items` list in the summary.
_CAPTURED_LINE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class SpecifyResult:
    """The outcome of `/speckit.specify` — the drafted spec body + extracts."""

    spec_text: str
    open_questions: tuple[str, ...]
    raw: dict[str, Any]


def _text_of(result: dict[str, Any]) -> str:
    """Concatenate text parts from an OpenCode command result.

    Mirrors `agents.implementation.speckit_driver._text_of` — same wire shape.
    """
    chunks: list[str] = []
    for part in result.get("parts", []) or []:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text", "")))
        elif isinstance(part, str):
            chunks.append(part)
    return "\n".join(chunks)


def _extract_open_questions(text: str) -> tuple[str, ...]:
    """Pull `[NEEDS CLARIFICATION: ...]` markers out of the spec text."""
    seen: list[str] = []
    for match in _OPEN_QUESTION.finditer(text):
        question = match.group(1).strip()
        if question and question not in seen:
            seen.append(question)
    return tuple(seen)


def _extract_captured_items(text: str, *, limit: int = 6) -> tuple[str, ...]:
    """Pull the first few bullet items from the spec.

    The Spec-Kit template puts captured assumptions / scope items in
    bullet-list form at the top of the document. We grab the first `limit`
    bullets so the reporter sees what we extracted. A bullet whose text is
    just a `[NEEDS CLARIFICATION: ...]` marker is filtered out — that's an
    open question, not a captured item.
    """
    items: list[str] = []
    for match in _CAPTURED_LINE.finditer(text):
        item = match.group(1).strip()
        if not item or _OPEN_QUESTION.fullmatch(item):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return tuple(items)


class SpecKitFrontDriver:
    """Issues Spec-Kit front-half commands and interprets their output."""

    SPECIFY = "speckit.specify"
    CLARIFY = "speckit.clarify"

    def __init__(self, client: Any) -> None:
        self.client = client

    def run_specify(
        self,
        session_id: str,
        intake_body: str,
        *,
        model: str | None = None,
    ) -> SpecifyResult:
        """Run `/speckit.specify` with `intake_body` as `$ARGUMENTS`.

        The intake body is the *issue's body* — `/speckit.specify`'s contract
        is "I receive a feature description in $ARGUMENTS and produce a
        spec.md in the project workspace". The returned `spec_text` is the
        assistant's textual reply; the actual spec file lands on disk in
        the workspace (the orchestrator picks it up there).

        Empty assistant output is *not* an implicit pass — `SpecifyResult`
        with an empty `spec_text` and zero captured items signals the
        orchestrator to escalate.
        """
        result = self.client.run_command(
            session_id, self.SPECIFY, intake_body, model=model
        )
        spec_text = _text_of(result)
        return SpecifyResult(
            spec_text=spec_text,
            open_questions=_extract_open_questions(spec_text),
            raw=result if isinstance(result, dict) else {},
        )

    def run_clarify(
        self,
        session_id: str,
        reporter_reply: str,
        *,
        model: str | None = None,
    ) -> SpecifyResult:
        """Run `/speckit.clarify` with `reporter_reply` as `$ARGUMENTS` (Tier 2).

        Ships in Tier 2 alongside `refiner.py`. The signature is locked now so
        Tier 1 callers can wire the driver fully and Tier 2 work is just the
        body of the loop. Returns a `SpecifyResult` so the same parsing path
        works for the iterated spec.
        """
        result = self.client.run_command(
            session_id, self.CLARIFY, reporter_reply, model=model
        )
        spec_text = _text_of(result)
        return SpecifyResult(
            spec_text=spec_text,
            open_questions=_extract_open_questions(spec_text),
            raw=result if isinstance(result, dict) else {},
        )
