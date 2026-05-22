"""Per-spec preview environments on Fly.io (Phase D, design §6).

The Implementation Agent gives a reporter a live preview of a PR's branch: one
Fly app + its own Postgres, seeded from a masked staging snapshot, isolated from
staging and production. `PreviewManager` drives that lifecycle —
spawn / seed / redeploy / destroy.

`FlyClient` is a Protocol seam, matching the `Workspace` / `GitHubClient` pattern:
unit tests run against `FakeFlyClient`; `FlyctlClient` shells out to `flyctl`
(integration-verified against a live Fly org, not unit-tested — a thin wrapper).

Site-specific values — the base image, the preview DNS domain, the Fly region,
the snapshot-restore command — are injected at construction, so this module
carries no deployment-specific configuration of its own.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

# Default snapshot-restore command template — `{snapshot}` / `{db}` are filled
# per call. Override at construction to match the app image's restore tooling.
_DEFAULT_RESTORE_COMMAND = "odoo-saas-restore --snapshot {snapshot} --database {db}"


@dataclass(frozen=True)
class PreviewEnv:
    """A provisioned preview environment for one spec's PR."""

    spec_id: int
    app: str            # the Fly app running Odoo
    postgres: str       # the Fly Postgres app backing it
    url: str            # the public preview URL
    created_at: str     # ISO-8601 UTC — drives the staleness sweep (design §6.5)


@runtime_checkable
class FlyClient(Protocol):
    """The Fly.io operations the preview lifecycle needs.

    A deliberately small seam — only what spawn / seed / redeploy / destroy use.
    """

    def create_app(self, name: str) -> None: ...
    def create_postgres(self, name: str, *, region: str, volume_gb: int) -> None: ...
    def attach_postgres(self, app: str, postgres: str) -> None: ...
    def set_secrets(self, app: str, secrets: dict[str, str]) -> None: ...
    def deploy(self, app: str, image: str, *, strategy: str = "rolling") -> None: ...
    def run_command(self, app: str, command: str) -> str: ...
    def destroy_app(self, name: str) -> None: ...


class FakeFlyClient:
    """In-memory `FlyClient` — records calls, returns canned data.

    Used by unit tests, and doubles as a shadow-mode client (the lifecycle runs
    but nothing is provisioned on Fly).
    """

    def __init__(self) -> None:
        self.apps: list[str] = []
        self.postgres: list[str] = []
        self.attachments: list[tuple[str, str]] = []
        self.secrets: dict[str, dict[str, str]] = {}
        self.deploys: list[tuple[str, str, str]] = []  # (app, image, strategy)
        self.commands: list[tuple[str, str]] = []      # (app, command)
        self.destroyed: list[str] = []
        self._command_output = ""
        self._destroy_failures: set[str] = set()

    def set_command_output(self, output: str) -> None:
        self._command_output = output

    def fail_destroy_of(self, name: str) -> None:
        """Make a later `destroy_app(name)` raise — for testing teardown paths."""
        self._destroy_failures.add(name)

    def create_app(self, name: str) -> None:
        self.apps.append(name)

    def create_postgres(self, name: str, *, region: str, volume_gb: int) -> None:
        self.postgres.append(name)

    def attach_postgres(self, app: str, postgres: str) -> None:
        self.attachments.append((app, postgres))

    def set_secrets(self, app: str, secrets: dict[str, str]) -> None:
        self.secrets.setdefault(app, {}).update(secrets)

    def deploy(self, app: str, image: str, *, strategy: str = "rolling") -> None:
        self.deploys.append((app, image, strategy))

    def run_command(self, app: str, command: str) -> str:
        self.commands.append((app, command))
        return self._command_output

    def destroy_app(self, name: str) -> None:
        if name in self._destroy_failures:
            raise RuntimeError(f"flyctl: destroying {name} failed")
        self.destroyed.append(name)


class FlyctlClient:
    """A `FlyClient` backed by the `flyctl` CLI — integration-verified against a
    live Fly org (not unit-tested; it is a thin subprocess wrapper)."""

    def __init__(self, *, org: str | None = None) -> None:
        self.org = org

    def _fly(self, *args: str) -> str:
        """Run a `flyctl` subcommand. A non-zero exit surfaces as
        ``subprocess.CalledProcessError`` (``check=True``)."""
        result = subprocess.run(
            ["flyctl", *args], capture_output=True, text=True, check=True
        )
        return result.stdout

    def _org_args(self) -> list[str]:
        return ["--org", self.org] if self.org else []

    def create_app(self, name: str) -> None:
        self._fly("apps", "create", name, *self._org_args())

    def create_postgres(self, name: str, *, region: str, volume_gb: int) -> None:
        self._fly(
            "postgres", "create", "--name", name, "--region", region,
            "--volume-size", str(volume_gb), *self._org_args(),
        )

    def attach_postgres(self, app: str, postgres: str) -> None:
        self._fly("postgres", "attach", postgres, "--app", app, "--yes")

    def set_secrets(self, app: str, secrets: dict[str, str]) -> None:
        pairs = [f"{key}={value}" for key, value in secrets.items()]
        self._fly("secrets", "set", *pairs, "--app", app)

    def deploy(self, app: str, image: str, *, strategy: str = "rolling") -> None:
        self._fly("deploy", "--app", app, "--image", image, "--strategy", strategy)

    def run_command(self, app: str, command: str) -> str:
        return self._fly("ssh", "console", "--app", app, "--command", command)

    def destroy_app(self, name: str) -> None:
        self._fly("apps", "destroy", name, "--yes")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PreviewManager:
    """Drives a per-spec preview environment's lifecycle over a `FlyClient`.

    Naming is conventional and deterministic, so any lifecycle call can rebuild
    the handles from a `spec_id` alone:

      app       odoo-saas-preview-spec-<id>
      postgres  odoo-saas-preview-spec-<id>-db
      url       https://preview-<id>.<domain>
    """

    def __init__(
        self,
        fly: FlyClient,
        *,
        base_image: str,
        domain: str,
        region: str = "iad",
        db_volume_gb: int = 5,
        restore_command: str = _DEFAULT_RESTORE_COMMAND,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.fly = fly
        self.base_image = base_image
        self.domain = domain
        self.region = region
        self.db_volume_gb = db_volume_gb
        self.restore_command = restore_command
        self._now: Callable[[], str] = now or _utc_now_iso

    def env_for(self, spec_id: int) -> PreviewEnv:
        """Build the (deterministic) `PreviewEnv` handles for a spec id."""
        app = f"odoo-saas-preview-spec-{spec_id}"
        return PreviewEnv(
            spec_id=spec_id,
            app=app,
            postgres=f"{app}-db",
            url=f"https://preview-{spec_id}.{self.domain}",
            created_at=self._now(),
        )

    def spawn(self, spec_id: int) -> PreviewEnv:
        """Provision a fresh preview env: app + its own Postgres + base deploy.

        Seeding (`seed`) is a separate step — `spawn` leaves an empty tenant DB.
        """
        env = self.env_for(spec_id)
        self.fly.create_app(env.app)
        self.fly.create_postgres(
            env.postgres, region=self.region, volume_gb=self.db_volume_gb
        )
        self.fly.attach_postgres(env.app, env.postgres)
        self.fly.set_secrets(
            env.app,
            {"PLATFORM": "preview", "TENANT_DBNAME": f"preview_{spec_id}"},
        )
        self.fly.deploy(env.app, self.base_image)
        return env

    def seed(self, env: PreviewEnv, snapshot_ref: str) -> None:
        """Restore a masked staging snapshot into the preview's tenant DB.

        The snapshot must already be masked (per agentlab's pipeline); this only
        restores it. The restore command itself ships in the app image.
        """
        command = self.restore_command.format(
            snapshot=snapshot_ref, db=f"preview_{env.spec_id}"
        )
        self.fly.run_command(env.app, command)

    def redeploy(self, env: PreviewEnv, image: str) -> None:
        """Deploy a new image to the existing app — same URL, rolling strategy."""
        self.fly.deploy(env.app, image, strategy="rolling")

    def destroy(self, env: PreviewEnv) -> None:
        """Tear the preview env down — both the app and its Postgres.

        The Postgres teardown runs even if the app teardown fails, so a partial
        failure can't orphan a (billable) database with no paired app.
        """
        try:
            self.fly.destroy_app(env.app)
        finally:
            self.fly.destroy_app(env.postgres)
