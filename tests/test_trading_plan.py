import pytest

from stock_analysis.trading_plan import calculate_position_size


def test_position_size_respects_risk_budget_and_lot_size():
    result = calculate_position_size(10, 9, 12, 100_000, 0.01, 0.25)

    assert result.valid is True
    assert result.shares_by_risk == 1000
    assert result.shares_by_position == 2400
    assert result.suggested_shares == 1000
    assert result.estimated_max_loss > 1000
    assert result.risk_reward is not None


def test_position_size_rejects_invalid_stop_and_target():
    result = calculate_position_size(10, 10, 9, 100_000, 0.01, 0.25)

    assert result.valid is False
    assert any("止损价应低于计划买入价" in warning for warning in result.warnings)
    assert any("止盈价应高于计划买入价" in warning for warning in result.warnings)


def test_position_size_warns_when_no_full_lot_is_affordable():
    result = calculate_position_size(100, 90, 120, 1_000, 0.01, 0.05)

    assert result.valid is True
    assert result.suggested_shares == 0
    assert any("最小交易单位" in warning for warning in result.warnings)
