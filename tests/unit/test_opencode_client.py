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
