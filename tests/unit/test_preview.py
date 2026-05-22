"""Unit tests for the Fly preview-environment lifecycle (Phase D)."""

import pytest

from agents.implementation.preview import (
    FakeFlyClient,
    FlyClient,
    PreviewManager,
)

BASE_IMAGE = "ghcr.io/goliatt/odoo-saas:latest"


def _manager(fly: FakeFlyClient | None = None, **kw: object) -> PreviewManager:
    return PreviewManager(
        fly or FakeFlyClient(),
        base_image=BASE_IMAGE,
        domain="preview.goliatt.co",
        now=lambda: "2026-05-22T00:00:00+00:00",
        **kw,  # type: ignore[arg-type]
    )


def test_fake_fly_client_satisfies_the_protocol():
    assert isinstance(FakeFlyClient(), FlyClient)


def test_spawn_returns_a_preview_env_with_conventional_names():
    env = _manager().spawn(1500)
    assert env.spec_id == 1500
    assert env.app == "odoo-saas-preview-spec-1500"
    assert env.postgres == "odoo-saas-preview-spec-1500-db"
    assert env.url == "https://preview-1500.preview.goliatt.co"
    assert env.created_at == "2026-05-22T00:00:00+00:00"


def test_spawn_creates_the_app_postgres_and_deploys_the_base_image():
    fly = FakeFlyClient()
    env = _manager(fly).spawn(1500)
    assert fly.apps == ["odoo-saas-preview-spec-1500"]
    assert fly.postgres == ["odoo-saas-preview-spec-1500-db"]
    assert (env.app, env.postgres) in fly.attachments
    assert fly.deploys == [(env.app, BASE_IMAGE, "rolling")]


def test_spawn_sets_the_preview_platform_secrets():
    fly = FakeFlyClient()
    _manager(fly).spawn(1500)
    secrets = fly.secrets["odoo-saas-preview-spec-1500"]
    assert secrets["PLATFORM"] == "preview"
    assert secrets["TENANT_DBNAME"] == "preview_1500"


def test_seed_runs_a_restore_of_the_snapshot_into_the_preview_db():
    fly = FakeFlyClient()
    mgr = _manager(fly)
    env = mgr.spawn(1500)
    fly.commands.clear()
    mgr.seed(env, "agentlab-snapshots/2026-05-20")
    assert len(fly.commands) == 1
    app, command = fly.commands[0]
    assert app == "odoo-saas-preview-spec-1500"
    assert "agentlab-snapshots/2026-05-20" in command
    assert "preview_1500" in command


def test_redeploy_deploys_a_new_image_to_the_same_app():
    fly = FakeFlyClient()
    mgr = _manager(fly)
    env = mgr.spawn(1500)
    fly.deploys.clear()
    fly.apps.clear()
    mgr.redeploy(env, "ghcr.io/goliatt/odoo-saas:pr-1500")
    assert fly.apps == []  # the app is reused, not recreated
    assert fly.deploys == [
        ("odoo-saas-preview-spec-1500", "ghcr.io/goliatt/odoo-saas:pr-1500", "rolling")
    ]


def test_destroy_tears_down_both_the_app_and_the_postgres():
    fly = FakeFlyClient()
    mgr = _manager(fly)
    env = mgr.spawn(1500)
    mgr.destroy(env)
    assert "odoo-saas-preview-spec-1500" in fly.destroyed
    assert "odoo-saas-preview-spec-1500-db" in fly.destroyed


def test_destroy_still_tears_down_postgres_when_app_destroy_fails():
    """A failed app teardown must not orphan the Postgres app (cost leak)."""
    fly = FakeFlyClient()
    mgr = _manager(fly)
    env = mgr.spawn(1500)
    fly.fail_destroy_of(env.app)
    with pytest.raises(RuntimeError):
        mgr.destroy(env)
    assert env.postgres in fly.destroyed
