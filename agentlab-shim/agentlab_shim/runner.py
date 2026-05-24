"""Playwright reproduction runner.

Pure-logic orchestrator: pre-flight classification, then (if pre-flight
passes) a Chromium navigation through the reporter's "Steps:" block.
Returns one of four outcomes matching ``ReproOutcome`` on the agent side:

- ``repro_confirmed``      — every step ran without raising, AND the
  reporter's "Expected:" string is NOT present on the final page.
- ``needs_repro_info``     — body lacks structured steps or the bug
  cannot be reproduced (the page actually behaves as "Expected:" — i.e.
  the issue is stale).
- ``needs_fixture``        — body references a customer / tenant id; we
  cannot run real customer data through agentlab.
- ``agentlab_unavailable`` — Playwright crashed, agentlab refused login,
  or the navigation timed out.

Playwright is imported lazily so unit tests that monkeypatch
``execute_steps`` don't need it installed.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

#: Patterns that signal customer / tenant data; matching ones short-circuit
#: to ``needs_fixture`` before any agentlab call.
_FIXTURE_HINTS = re.compile(
    r"\b(?:tenant[\s_-]?id|tenant\s*[:#]\s*\d+|customer[\s_-]?id|"
    r"acme[\s_-]?(?:corp|inc|llc)|"
    r"\b[A-Z][A-Za-z]+\s+(?:Inc\.?|Corp\.?|LLC|Ltd\.?))\b",
    re.IGNORECASE,
)

#: Steps must contain at least one of these structural markers for a repro
#: attempt to be worth trying. Otherwise we ask for more info.
_STEP_MARKERS = re.compile(
    r"^\s*(?:\d+\.|[-*]|Step\s*\d+|goto|click|fill|wait)\s",
    re.IGNORECASE | re.MULTILINE,
)

#: One Playwright primitive per line. Format:
#:   ``goto: <url>``                 -> page.goto(url)
#:   ``click: <selector>``           -> page.click(selector)
#:   ``fill:  <selector> = <value>`` -> page.fill(selector, value)
#:   ``wait:  <selector>``           -> page.wait_for_selector(selector)
#:
#: A leading numbered-list / bullet prefix (e.g. ``1.``, ``- ``, ``* ``) is
#: tolerated so a reporter's Markdown list `1. goto: ...` parses cleanly.
_LIST_PREFIX = r"(?:\d+\.\s*|[-*]\s+|Step\s*\d+\s*[:.)]\s*)?"
_GOTO = re.compile(
    rf"^\s*{_LIST_PREFIX}goto\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE
)
_CLICK = re.compile(
    rf"^\s*{_LIST_PREFIX}click\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_FILL = re.compile(
    rf"^\s*{_LIST_PREFIX}fill\s*:\s*(.+?)\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_WAIT = re.compile(
    rf"^\s*{_LIST_PREFIX}wait\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)

_EXPECTED = re.compile(r"^\s*Expected\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class ReproRequest:
    """Inputs the runner receives from ``main.py``."""

    issue: int
    title: str
    body: str
    attachments: tuple[str, ...]
    reporter: str
    base_url: str
    timeout_ms: int = 60_000


class ReproResponse(BaseModel):
    """Response shape the agent's `HttpShimAgentlabClient` parses."""

    outcome: str = Field(
        description=(
            "repro_confirmed | needs_repro_info | needs_fixture | "
            "agentlab_unavailable"
        )
    )
    summary: str = ""
    logs: str = ""
    screenshots: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


@dataclass
class _StepLog:
    """One executed Playwright primitive — kept for the response's `logs` field."""

    op: str
    arg: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        tag = "OK " if self.ok else "ERR"
        return f"[{tag}] {self.op} {self.arg}{(' — ' + self.detail) if self.detail else ''}"


# ---------------------------------------------------------------------------
# Pre-flight classification (cheap, no Playwright call)
# ---------------------------------------------------------------------------

def pre_flight(request: ReproRequest) -> ReproResponse | None:
    """Classify the request before spinning Chromium.

    Returns a `ReproResponse` if we should short-circuit; `None` to proceed
    with a real navigation.
    """
    text = f"{request.title}\n{request.body}".strip()

    if _FIXTURE_HINTS.search(text):
        return ReproResponse(
            outcome="needs_fixture",
            summary=(
                "the issue references customer or tenant data; the bug "
                "cannot be reproduced on agentlab without a sanitized "
                "fixture from the security-leads team."
            ),
            questions=[
                "Please ask security-leads to produce a sanitized fixture "
                "for this scenario and re-open with a generic data set.",
            ],
        )

    if not _STEP_MARKERS.search(request.body):
        return ReproResponse(
            outcome="needs_repro_info",
            summary=(
                "the issue body does not contain structured reproduction "
                "steps; cannot run an automated reproduction without them."
            ),
            questions=[
                "Please post a numbered list of steps starting with the URL.",
                "Format example:\n  1. goto: /web/login\n  2. fill: input[name=login] = admin\n"
                "  3. click: button[type=submit]\n  4. wait: .o_navbar",
                "Include an `Expected:` line at the end describing what "
                "should appear, plus `Actual:` for what you saw instead.",
            ],
        )

    return None


# ---------------------------------------------------------------------------
# Step parsing
# ---------------------------------------------------------------------------

@dataclass
class _ParsedSteps:
    primitives: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    expected: str = ""


def parse_steps(body: str) -> _ParsedSteps:
    """Translate the reporter's Steps block into Playwright primitives.

    Preserves source order — `fill` then `click` must run in that sequence
    for an Odoo login form. Best-effort: unknown lines are silently dropped;
    the runner surfaces parse failures as hints if too few primitives lift.
    """
    parsed = _ParsedSteps()
    # Collect every match across all primitive regexes WITH its start
    # offset, then sort by offset to recover source order.
    hits: list[tuple[int, str, tuple[str, ...]]] = []
    for match in _GOTO.finditer(body):
        hits.append((match.start(), "goto", (match.group(1),)))
    for match in _CLICK.finditer(body):
        hits.append((match.start(), "click", (match.group(1),)))
    for match in _FILL.finditer(body):
        hits.append((match.start(), "fill", (match.group(1), match.group(2))))
    for match in _WAIT.finditer(body):
        hits.append((match.start(), "wait", (match.group(1),)))
    hits.sort(key=lambda h: h[0])
    parsed.primitives = [(op, args) for _, op, args in hits]
    m = _EXPECTED.search(body)
    if m:
        parsed.expected = m.group(1).strip()
    return parsed


# ---------------------------------------------------------------------------
# Playwright execution
# ---------------------------------------------------------------------------

def execute_steps(
    parsed: _ParsedSteps, *, base_url: str, timeout_ms: int
) -> tuple[list[_StepLog], list[str], str | None]:
    """Run the parsed primitives in a fresh Chromium context.

    Returns ``(step_logs, screenshots_b64, final_html)``. ``final_html`` is
    ``None`` if Playwright raised before any navigation completed; otherwise
    it's the rendered HTML of the last page (used for the "Expected:" check).
    """
    # Imported lazily so unit tests that don't run Playwright don't need it.
    from playwright.sync_api import (  # type: ignore[import-not-found]
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )

    logs: list[_StepLog] = []
    screenshots: list[str] = []
    final_html: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.set_default_timeout(timeout_ms)
            page = context.new_page()
            for op, args in parsed.primitives:
                try:
                    if op == "goto":
                        url = args[0]
                        if url.startswith("/"):
                            url = f"{base_url.rstrip('/')}{url}"
                        page.goto(url, timeout=timeout_ms)
                        logs.append(_StepLog(op, url, ok=True))
                    elif op == "click":
                        page.click(args[0], timeout=timeout_ms)
                        logs.append(_StepLog(op, args[0], ok=True))
                    elif op == "fill":
                        page.fill(args[0], args[1], timeout=timeout_ms)
                        logs.append(_StepLog(op, f"{args[0]} = {args[1]}", ok=True))
                    elif op == "wait":
                        page.wait_for_selector(args[0], timeout=timeout_ms)
                        logs.append(_StepLog(op, args[0], ok=True))
                except (PlaywrightError, PlaywrightTimeout) as exc:
                    # A single failing step doesn't abort the run — record
                    # and continue. The orchestrator decides whether the
                    # overall outcome is `repro_confirmed` (good — the bug
                    # is real) or `agentlab_unavailable` (bad — Playwright
                    # itself broke before we could finish).
                    logs.append(_StepLog(op, " ".join(args), ok=False, detail=str(exc)[:200]))
            try:
                final_html = page.content()
            except (PlaywrightError, PlaywrightTimeout):
                final_html = None
            try:
                shot = page.screenshot(full_page=False)
                screenshots.append(base64.b64encode(shot).decode("ascii"))
            except (PlaywrightError, PlaywrightTimeout):
                pass
            context.close()
        finally:
            browser.close()

    return logs, screenshots, final_html


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_repro(request: ReproRequest) -> ReproResponse:
    """Pre-flight, then (if it passes) drive Playwright. Returns the response."""
    early = pre_flight(request)
    if early is not None:
        return early

    parsed = parse_steps(request.body)
    if not parsed.primitives:
        # The pre-flight saw step markers but parser didn't lift anything
        # concrete — surface the ambiguity instead of pretending we ran.
        return ReproResponse(
            outcome="needs_repro_info",
            summary=(
                "found step-like markers in the body but could not extract "
                "any executable primitives (goto/click/fill/wait)."
            ),
            questions=[
                "Re-format the steps as primitives — see the format example "
                "in the earlier comment.",
            ],
        )

    try:
        logs, screenshots, final_html = execute_steps(
            parsed, base_url=request.base_url, timeout_ms=request.timeout_ms
        )
    except Exception as exc:  # noqa: BLE001
        return ReproResponse(
            outcome="agentlab_unavailable",
            summary=f"Playwright run failed: {type(exc).__name__}: {exc}",
        )

    return _classify_run(parsed, logs, screenshots, final_html)


def _classify_run(
    parsed: _ParsedSteps,
    logs: list[_StepLog],
    screenshots: list[str],
    final_html: str | None,
) -> ReproResponse:
    """Decide repro_confirmed vs agentlab_unavailable from the run output.

    Heuristics:
      - Every step erred -> agentlab_unavailable.
      - Some steps erred, some passed -> still `repro_confirmed` (the bug
        likely IS that some interaction fails — that's the whole point).
      - All steps passed AND the reporter's "Expected:" string is found on
        the final page -> `needs_repro_info` ("bug appears resolved").
      - Otherwise -> `repro_confirmed`.
    """
    log_text = "\n".join(log.line() for log in logs)[-4096:]

    if logs and all(not log.ok for log in logs):
        return ReproResponse(
            outcome="agentlab_unavailable",
            summary="every reproduction step failed inside Playwright",
            logs=log_text,
            screenshots=screenshots,
        )

    if (
        parsed.expected
        and final_html is not None
        and parsed.expected.lower() in final_html.lower()
        and all(log.ok for log in logs)
    ):
        return ReproResponse(
            outcome="needs_repro_info",
            summary=(
                "the reporter's `Expected:` outcome appeared after running "
                "the steps — the bug may already be resolved."
            ),
            logs=log_text,
            screenshots=screenshots,
            questions=[
                "Could you re-test on the latest deployment? The expected "
                "behaviour is currently present in our reproduction.",
            ],
        )

    return ReproResponse(
        outcome="repro_confirmed",
        summary=(
            f"ran {len(parsed.primitives)} step(s); the bug is present "
            f"({sum(1 for log in logs if not log.ok)} step(s) failed)"
        ),
        logs=log_text,
        screenshots=screenshots,
    )


__all__ = [
    "ReproRequest",
    "ReproResponse",
    "execute_steps",
    "parse_steps",
    "pre_flight",
    "run_repro",
]


# Re-export for `_classify_run` to keep mypy/ruff happy on `Any`.
_AnyAlias = Any
