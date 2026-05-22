"""Unit tests for per-PR cost tracking + the spend cap (Phase E)."""

from agents.implementation.cost import DEFAULT_CAP_USD, Budget, session_cost


def test_a_fresh_budget_is_empty():
    budget = Budget()
    assert budget.spent_usd == 0.0
    assert budget.remaining_usd == DEFAULT_CAP_USD
    assert not budget.warn
    assert not budget.exceeded


def test_add_accumulates_spend():
    budget = Budget(cap_usd=20.0)
    budget.add(5.0)
    budget.add(3.0)
    assert budget.spent_usd == 8.0
    assert budget.remaining_usd == 12.0


def test_warn_fires_at_eighty_percent_of_the_cap():
    budget = Budget(cap_usd=20.0)
    budget.add(15.0)
    assert not budget.warn  # 75%
    budget.add(1.0)
    assert budget.warn      # 80%
    assert not budget.exceeded


def test_exceeded_fires_at_the_cap():
    budget = Budget(cap_usd=20.0)
    budget.add(20.0)
    assert budget.exceeded
    assert budget.remaining_usd == 0.0


def test_add_ignores_a_negative_charge():
    budget = Budget()
    budget.add(-5.0)
    assert budget.spent_usd == 0.0


def test_session_cost_sums_the_message_costs():
    # Message shape mirrors OpenCode's GET /session/:id/message response.
    messages = [
        {"info": {"role": "user"}},
        {"info": {"role": "assistant", "cost": 0.42, "tokens": {"total": 100}}},
        {"info": {"role": "assistant", "cost": 0.10, "tokens": {"total": 50}}},
    ]
    assert session_cost(messages) == 0.52


def test_session_cost_of_messages_without_cost_is_zero():
    assert session_cost([{"info": {}}, {"parts": []}]) == 0.0
