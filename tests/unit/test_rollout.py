"""Unit tests for the canary rollout gate (Phase F)."""

from agents.implementation.rollout import Rollout, RolloutDecision, RolloutStage


def test_kill_switch_skips_everything():
    rollout = Rollout(stage=RolloutStage.DEFAULT_ON, enabled=False)
    assert rollout.decide("any-pr") is RolloutDecision.SKIP


def test_shadow_stage_shadows_every_target():
    rollout = Rollout(stage=RolloutStage.SHADOW)
    assert rollout.decide("pr-1") is RolloutDecision.SHADOW


def test_fixtures_stage_acts_only_on_fixtures():
    rollout = Rollout(stage=RolloutStage.FIXTURES, fixtures=frozenset({"spec-fix-1"}))
    assert rollout.decide("spec-fix-1") is RolloutDecision.ACT
    assert rollout.decide("real-pr") is RolloutDecision.SKIP


def test_opt_in_stage_acts_only_on_opted_in_targets():
    rollout = Rollout(stage=RolloutStage.OPT_IN, opt_in=frozenset({"team-repo"}))
    assert rollout.decide("team-repo") is RolloutDecision.ACT
    assert rollout.decide("other-repo") is RolloutDecision.SKIP


def test_default_on_acts_on_everything():
    rollout = Rollout(stage=RolloutStage.DEFAULT_ON)
    assert rollout.decide("any-pr") is RolloutDecision.ACT


def test_from_env_reads_the_stage_and_kill_switch():
    rollout = Rollout.from_env({"AGENTS_ENABLED": "false", "ROLLOUT_STAGE": "opt_in"})
    assert rollout.stage is RolloutStage.OPT_IN
    assert rollout.enabled is False


def test_from_env_defaults_to_enabled_shadow():
    """Missing config -> the safest posture: enabled, but shadow-only."""
    rollout = Rollout.from_env({})
    assert rollout.enabled is True
    assert rollout.stage is RolloutStage.SHADOW


def test_from_env_falls_back_to_shadow_on_an_unknown_stage():
    assert Rollout.from_env({"ROLLOUT_STAGE": "bogus"}).stage is RolloutStage.SHADOW
