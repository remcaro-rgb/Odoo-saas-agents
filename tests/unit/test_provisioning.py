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


def test_provision_workspace_in_shadow_strips_the_token_from_origin(fake_client):
    """SHADOW mode must leave the container with no push auth. The bash
    script drops the token from `origin` (via `git remote set-url`) after
    the initial fetch, so any subsequent autonomous `git push` from the
    container fails 401 — closing the gap the FIXTURES-stage ACT smoke
    surfaced (the container's Fly `GITHUB_TOKEN` would otherwise push
    even in shadow)."""
    provision_workspace(
        fake_client, "sess-1", "acme/odoo", "main", shadow=True
    )
    _, text = fake_client.messages[0]
    # The original fetch still authenticates with the token.
    assert "${GITHUB_TOKEN}" in text
    # …but origin is rewritten to the token-less URL afterwards.
    assert (
        "git remote set-url origin https://github.com/acme/odoo.git" in text
    )


def test_provision_workspace_in_act_keeps_the_authed_origin(fake_client):
    """ACT mode does NOT strip the token — the container's autonomous git
    path can commit and push to the PR branch using the Fly secret. (The
    FIXTURES-stage ACT smoke on PR #30 demonstrated this push working.)"""
    provision_workspace(fake_client, "sess-1", "acme/odoo", "main")  # default
    _, text = fake_client.messages[0]
    assert "git remote set-url" not in text
