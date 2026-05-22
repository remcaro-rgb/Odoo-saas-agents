"""Unit tests for notifications + escalation routing (Phase E)."""

from agents.implementation.notifier import (
    DEVOPS_CHANNEL,
    FakeNotifier,
    Notifier,
    notify_alert,
    notify_escalation,
)


def test_fake_notifier_satisfies_the_protocol():
    assert isinstance(FakeNotifier(), Notifier)


def test_fake_notifier_records_sends():
    notifier = FakeNotifier()
    notifier.send("#ops", "hello", severity="warn")
    assert notifier.sent == [("#ops", "hello", "warn")]


def test_notify_escalation_pages_the_devops_channel():
    notifier = FakeNotifier()
    notify_escalation(notifier, 1501, "Gate 1 failed 3x")
    assert len(notifier.sent) == 1
    channel, message, severity = notifier.sent[0]
    assert channel == DEVOPS_CHANNEL
    assert severity == "page"
    assert "1501" in message
    assert "Gate 1 failed 3x" in message


def test_notify_alert_warns_the_devops_channel():
    notifier = FakeNotifier()
    notify_alert(notifier, "spend at 80% of cap")
    channel, message, severity = notifier.sent[0]
    assert channel == DEVOPS_CHANNEL
    assert severity == "warn"
    assert "spend at 80%" in message
