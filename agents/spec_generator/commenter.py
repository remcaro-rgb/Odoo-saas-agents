"""Render the Spec Generator's GitHub comments in a consistent bot voice.

Pure formatting — no I/O. Every comment carries `AGENT_MARKER`, an invisible
HTML marker, so the orchestrator can recognise its own comments (dedup, sweep).
The marker is *different* from the implementation agent's so a human reading
the PR can tell which agent owns which comment, and so the dedup checks in
each agent never cross-fire.
"""

from __future__ import annotations

AGENT_MARKER = "<!-- spec-gen-agent -->"


def _wrap(body: str) -> str:
    return f"{body.strip()}\n\n{AGENT_MARKER}\n"


def spec_drafted(
    *,
    spec_path: str,
    pr_number: int | None,
    captured_items: list[str],
    open_questions: list[str],
) -> str:
    """Posted on the issue after the spec PR is opened.

    Surfaces what the agent extracted and what it still needs the reporter to
    answer. The `/confirm` instruction tells the reporter how to advance the
    PR to `intent-confirmed` — that label is the handoff signal to the
    Implementation Agent.
    """
    lines = [
        "I've drafted a design spec for this issue.",
        "",
    ]
    if pr_number is not None:
        lines += [f"**Spec PR:** #{pr_number} (`{spec_path}`)", ""]
    else:
        lines += [f"**Spec path:** `{spec_path}`", ""]

    lines.append("**What I captured from your issue:**")
    if captured_items:
        lines += [f"- {item}" for item in captured_items]
    else:
        lines.append("- (nothing structured yet — please review the spec)")
    lines.append("")

    if open_questions:
        lines.append("**Open questions I still need from you:**")
        lines += [f"- {question}" for question in open_questions]
        lines.append("")
        lines.append(
            "Please answer the open questions above. When you're happy with "
            "the spec, comment `/confirm` on this issue (or on the spec PR) "
            "and I'll hand it off to the Implementation Agent."
        )
    else:
        lines.append(
            "I have no open questions. When you're happy with the spec, "
            "comment `/confirm` on this issue (or on the spec PR) and I'll "
            "hand it off to the Implementation Agent. If I don't hear from "
            "you for 24 hours, I'll confirm it automatically."
        )

    return _wrap("\n".join(lines))


def awaiting_reporter_confirm(
    *, captured_items: list[str], open_questions: list[str]
) -> str:
    """A shorter ``awaiting reply`` style comment for re-engage paths."""
    lines = ["I've updated the spec with your latest feedback.", ""]
    lines.append("**Updated capture:**")
    if captured_items:
        lines += [f"- {item}" for item in captured_items]
    else:
        lines.append("- (no new items captured)")
    lines.append("")
    if open_questions:
        lines.append("**Still need from you:**")
        lines += [f"- {q}" for q in open_questions]
        lines.append("")
        lines.append("Reply to address the open questions, or `/confirm` to ship as-is.")
    else:
        lines.append(
            "All earlier questions are resolved. `/confirm` to hand off to "
            "the Implementation Agent."
        )
    return _wrap("\n".join(lines))


def sensitive_escalation_notice(signals: list[str]) -> str:
    """Posted on the issue when classifier flags `sensitive` content.

    The agent does not draft a spec for sensitive issues — they route to the
    `security-leads` CODEOWNERS group, the body stays unsummarised, and the
    bot comment is intentionally vague about *what* it found so the issue
    body isn't echoed back in cleartext.
    """
    lines = [
        "I've flagged this issue for a human teammate to triage.",
        "",
        "**Reason:** the issue body looks like it may contain sensitive "
        "content (credentials, PII, or similar). I won't draft a spec "
        "automatically — the security-leads team will pick this up.",
    ]
    if signals:
        # Surface the matched *category* not the matched secret — never echo.
        categories = sorted({sig.split(":", 1)[0] for sig in signals})
        lines += ["", f"_(detector categories: {', '.join(categories)})_"]
    return _wrap("\n".join(lines))


def low_confidence_notice(kind: str, confidence: float, signals: list[str]) -> str:
    """Posted alongside `spec_drafted` when classifier confidence is low.

    The reporter can `/reclassify bug` or `/reclassify feature` to fix a
    mis-route (Tier 2 wires the reclassify intent into the refiner).
    """
    lines = [
        f"_Heads up: I classified this as **{kind}** with confidence "
        f"{confidence:.2f} — that's below my comfortable threshold._",
        "",
        "If I got the category wrong, reply with `/reclassify bug` or "
        "`/reclassify feature` and I'll redraft.",
    ]
    if signals:
        lines += ["", f"_(signals I used: {', '.join(signals)})_"]
    return _wrap("\n".join(lines))
