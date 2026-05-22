"""The 4-stage canary rollout gate (Phase F, design §13).

The agent is rolled out gradually — shadow -> fixtures -> opt-in -> default-on.
`Rollout.decide` tells the composition root, per target, whether to ACT (post /
push for real), SHADOW (run but draft only — wire the Fake clients, post
nothing), or SKIP (don't run). `enabled` is the `AGENTS_ENABLED` kill switch
(design §13, Rollback): when false the agent does nothing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class RolloutStage(StrEnum):
    """The canary stages, in rollout order (design §13)."""

    SHADOW = "shadow"          # drafts everything, but posts / pushes nothing
    FIXTURES = "fixtures"      # live on test-fixture specs only
    OPT_IN = "opt_in"          # live on targets the team has opted in
    DEFAULT_ON = "default_on"  # live everywhere


class RolloutDecision(StrEnum):
    """What the agent should do for a given target."""

    ACT = "act"        # run for real — post comments, push commits
    SHADOW = "shadow"  # run, but draft only — wire the Fake clients, post nothing
    SKIP = "skip"      # do not run at all


@dataclass
class Rollout:
    """The canary gate. `decide(target)` maps the current stage to an action.

    `fixtures` / `opt_in` are the in-scope target sets for those two stages — a
    target is a stable id (a repo slug, a PR number as str, a spec id).
    """

    stage: RolloutStage
    enabled: bool = True  # the AGENTS_ENABLED kill switch
    fixtures: frozenset[str] = frozenset()
    opt_in: frozenset[str] = frozenset()

    def decide(self, target: str) -> RolloutDecision:
        """Decide ACT / SHADOW / SKIP for `target` under the current stage."""
        if not self.enabled:
            return RolloutDecision.SKIP
        if self.stage is RolloutStage.SHADOW:
            return RolloutDecision.SHADOW
        if self.stage is RolloutStage.FIXTURES:
            in_scope = target in self.fixtures
        elif self.stage is RolloutStage.OPT_IN:
            in_scope = target in self.opt_in
        else:  # DEFAULT_ON
            in_scope = True
        return RolloutDecision.ACT if in_scope else RolloutDecision.SKIP

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Rollout:
        """Build a `Rollout` from the environment — `AGENTS_ENABLED` (the kill
        switch) and `ROLLOUT_STAGE`. Missing or unknown config falls back to the
        safest posture: enabled, but SHADOW-only."""
        source = env if env is not None else os.environ
        enabled = source.get("AGENTS_ENABLED", "true").strip().lower() != "false"
        try:
            stage = RolloutStage(source.get("ROLLOUT_STAGE", "shadow").strip().lower())
        except ValueError:
            stage = RolloutStage.SHADOW
        return cls(stage=stage, enabled=enabled)
