"""Unit tests for the OpenCode HTTP client helpers (Phase A)."""

from agents.implementation.opencode_client import _model_ref


def test_model_ref_splits_provider_and_model():
    assert _model_ref("opencode-go/glm-5.1") == {
        "providerID": "opencode-go",
        "modelID": "glm-5.1",
    }


def test_model_ref_handles_the_anthropic_provider():
    assert _model_ref("anthropic/claude-sonnet-4-6") == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4-6",
    }


class _FakeResponse:
    status_code = 200
    content = b"{}"

    def json(self) -> dict:
        return {}


def _capture_request(monkeypatch, client):
    captured: list[dict] = []

    def fake_request(method, path, **kw):
        captured.append({"method": method, "path": path, **kw})
        return _FakeResponse()

    monkeypatch.setattr(client._http, "request", fake_request)
    return captured


def test_run_command_sends_model_as_a_bare_string(monkeypatch):
    """OpenCode's `/session/:id/command` endpoint expects `model` as a bare
    "<provider>/<model>" string and rejects an object with
    `"Expected string | null"`. Regression — the first FIXTURES-stage ACT
    run surfaced this when the coder's frontier-model corrective re-prompt
    hit `/command` with the object shape `_model_ref` produces."""
    from agents.implementation.opencode_client import OpenCodeClient
    client = OpenCodeClient(base_url="http://example")
    captured = _capture_request(monkeypatch, client)
    try:
        client.run_command(
            "ses_x", "speckit.implement", "", model="anthropic/claude-sonnet-4-6"
        )
    finally:
        client.close()
    body = captured[0]["json"]
    assert body["model"] == "anthropic/claude-sonnet-4-6"
    assert isinstance(body["model"], str)


def test_send_message_still_sends_model_as_an_object(monkeypatch):
    """Inverse of the above — locks in that `/session/:id/message` keeps the
    object shape (`{providerID, modelID}`). The two endpoints' asymmetry is
    OpenCode's, not ours."""
    from agents.implementation.opencode_client import OpenCodeClient
    client = OpenCodeClient(base_url="http://example")
    captured = _capture_request(monkeypatch, client)
    try:
        client.send_message(
            "ses_x", "hello", model="anthropic/claude-sonnet-4-6"
        )
    finally:
        client.close()
    body = captured[0]["json"]
    assert body["model"] == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4-6",
    }
