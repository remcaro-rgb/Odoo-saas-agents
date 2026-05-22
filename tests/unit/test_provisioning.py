"""Unit tests for workspace provisioning (Phase D)."""

from agents.implementation.provisioning import provision_workspace


def test_provision_workspace_sends_a_checkout_for_the_branch(fake_client):
    provision_workspace(fake_client, "sess-1", "acme/odoo", "agent/spec-1500")
    assert fake_client.messages
    session_id, text = fake_client.messages[0]
    assert session_id == "sess-1"
    assert "acme/odoo" in text
    assert "agent/spec-1500" in text
    assert "git checkout" in text


def test_provision_workspace_references_the_token_env_not_a_literal(fake_client):
    provision_workspace(fake_client, "sess-1", "acme/odoo", "main")
    _, text = fake_client.messages[0]
    assert "${GITHUB_TOKEN}" in text


def test_provision_workspace_targets_the_given_workdir(fake_client):
    provision_workspace(
        fake_client, "sess-1", "acme/odoo", "main", workdir="/srv/checkout"
    )
    _, text = fake_client.messages[0]
    assert "cd /srv/checkout" in text


def test_provision_workspace_sparse_excludes_the_protected_paths(fake_client):
    """The checkout is sparse — the agent never receives the guardrail paths
    (infra/, .github/, Dockerfile, saas_tenant_gate/security/) in its worktree,
    so bash cannot modify the repo's copy of them."""
    provision_workspace(fake_client, "sess-1", "acme/odoo", "main")
    _, text = fake_client.messages[0]
    assert "core.sparseCheckout true" in text
    assert "!/infra/" in text
    assert "!/.github/" in text
    assert "!/Dockerfile" in text
    assert "!/saas_tenant_gate/security/" in text
