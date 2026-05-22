"""Notifications + escalation routing (Phase E, design §5 / §14.3).

`Notifier` is a Protocol seam: unit tests + shadow mode run against
`FakeNotifier`; `SlackNotifier` posts to a Slack incoming webhook
(integration-verified, not unit-tested — a thin HTTP wrapper). The
`notify_escalation` / `notify_alert` helpers encode the standard routes so
callers don't repeat the channel + severity.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

#: Escalations and operational alerts route to this channel (design §14.3).
DEVOPS_CHANNEL = "#devops-implementations"


@runtime_checkable
class Notifier(Protocol):
    """Sends a notification to a channel at a severity (info / warn / page)."""

    def send(self, channel: str, message: str, *, severity: str = "info") -> None: ...


class FakeNotifier:
    """In-memory `Notifier` — records sends. For unit tests and shadow mode."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []  # (channel, message, severity)

    def send(self, channel: str, message: str, *, severity: str = "info") -> None:
        self.sent.append((channel, message, severity))


class SlackNotifier:
    """A `Notifier` backed by a Slack incoming webhook — integration-verified
    against a live webhook, not unit-tested (a thin HTTP wrapper)."""

    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, channel: str, message: str, *, severity: str = "info") -> None:
        httpx.post(
            self.webhook_url,
            json={"channel": channel, "text": f"[{severity}] {message}"},
            timeout=self.timeout,
        )


def notify_escalation(notifier: Notifier, pr: int, reason: str) -> None:
    """Route a PR escalation to the DevOps channel — pages on-call."""
    notifier.send(
        DEVOPS_CHANNEL, f"PR #{pr} escalated — {reason}", severity="page"
    )


def notify_alert(notifier: Notifier, message: str) -> None:
    """Route an operational alert (spend warning, slow preview, ...) to the
    DevOps channel as a warning."""
    notifier.send(DEVOPS_CHANNEL, message, severity="warn")
