"""Render the agent's GitHub PR comments in a consistent bot voice (Phase D).

Pure formatting — no I/O. Every comment carries `AGENT_MARKER`, an invisible
HTML marker, so the orchestrator can recognise its own comments (dedup, Phase E).
"""

from __future__ import annotations

AGENT_MARKER = "<!-- impl-agent -->"


def _wrap(body: str) -> str:
    return f"{body.strip()}\n\n{AGENT_MARKER}\n"


def iteration_update(summary: str, preview_url: str | None = None) -> str:
    """Posted after the agent has pushed an iteration in response to feedback."""
    lines = ["I've pushed an update addressing your feedback.", "", summary.strip()]
    if preview_url:
        lines += ["", f"**Preview:** {preview_url}"]
    return _wrap("\n".join(lines))


def implementation_ready(summary: str, preview_url: str | None = None) -> str:
    """Posted after the agent implements a freshly intent-confirmed spec."""
    lines = ["I've implemented this spec and pushed the code.", "", summary.strip()]
    if preview_url:
        lines += ["", f"**Preview:** {preview_url}"]
    return _wrap("\n".join(lines))


def escalation_notice(reason: str, details: str = "") -> str:
    """Posted when the agent escalates the PR to a human teammate."""
    lines = [
        "I've flagged this for a human teammate to take a look.",
        "",
        f"**Reason:** {reason}",
    ]
    if details.strip():
        lines += ["", details.strip()]
    return _wrap("\n".join(lines))


def human_commit_ping(sha: str, author: str) -> str:
    """Posted when a human pushes to the agent's branch — asks the reporter to
    re-review and re-confirm the change before the PR moves on (design §5.3)."""
    short = sha[:8] if sha else "(unknown)"
    lines = [
        f"A human teammate (`{author}`) pushed commit `{short}` to this branch.",
        "",
        "Please re-review and re-confirm the change before it moves on.",
    ]
    return _wrap("\n".join(lines))
