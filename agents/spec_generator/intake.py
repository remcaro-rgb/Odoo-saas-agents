"""Issue -> structured `Intake` (Tier 1).

The classifier and the drafter both want the *same* normalised view of an
issue: a title, a body, the reporter, the language the body is in, plus any
labels and attachments. `IntakeBuilder` produces that view from an `Event`.

Pure — no I/O. Language detection is a coarse ASCII-vs-not heuristic for Tier 1;
a real `langdetect` integration ships in Tier 2 alongside the multilingual
reporter-reply path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .events import Event

# Markdown image references: ``![alt](url)`` — the canonical issue attachment.
_IMG_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# A bare URL — used as a low-fidelity attachment proxy when the reporter
# pastes a video link without markdown image syntax.
_URL = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Intake:
    """The orchestrator's structured view of an issue.

    `kind_hint` is whatever routing label the webhook carried — the
    classifier may keep it or override it. `language` is `"en"` for ASCII-only
    bodies and `"other"` otherwise (Tier 1 heuristic; replaced in Tier 2).
    """

    issue: int
    title: str
    body: str
    reporter: str
    language: str
    labels: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    kind_hint: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


def _detect_language(text: str) -> str:
    """Coarse Tier-1 language flag.

    The reporter-reply path needs to know whether to translate ``commenter.py``
    output. ASCII-only bodies are tagged `"en"`; anything else (a non-ASCII
    character anywhere) routes through the multilingual reply path. Replaced
    in Tier 2 by a `langdetect` integration that handles the long tail.
    """
    return "en" if text.isascii() else "other"


def _extract_attachments(body: str) -> tuple[str, ...]:
    """Pull out attachment URLs from a markdown issue body.

    Markdown image refs win — they're the canonical GitHub upload syntax. A
    bare http(s) URL also counts as a low-fidelity attachment proxy so a
    reporter pasting a screen-recording link without image syntax is still
    captured. De-duplicated while preserving order.
    """
    urls: list[str] = []
    for match in _IMG_REF.finditer(body):
        urls.append(match.group(1))
    for match in _URL.finditer(body):
        url = match.group(0)
        # Strip a trailing ``)`` left over from `![alt](url)` patterns the
        # image regex missed when nested parens scramble the capture.
        url = url.rstrip(").,")
        if url not in urls:
            urls.append(url)
    return tuple(urls)


class IntakeBuilder:
    """Builds an `Intake` from a Spec Generator `Event`.

    Stateless — exposed as a class only to mirror the implementation agent's
    seam shape (so a future injected fixture / mock has a familiar surface).
    """

    def build(self, event: Event, labels: Iterable[str] | None = None) -> Intake:
        """Normalize an `event` into an `Intake`.

        `labels` is the live label set on the issue (read by the orchestrator
        from the GitHub side), preserved so the classifier can break ties.
        A missing `event.issue` raises — without it the spec branch can't be
        named.
        """
        if event.issue is None:
            raise ValueError("event has no issue number — cannot build an Intake")
        body = event.body or ""
        title = event.title or ""
        return Intake(
            issue=int(event.issue),
            title=title.strip(),
            body=body,
            reporter=event.actor or "unknown",
            language=_detect_language(f"{title}\n{body}"),
            labels=tuple(labels or ()),
            attachments=_extract_attachments(body),
            kind_hint=event.kind_hint,
            raw=dict(event.raw),
        )
