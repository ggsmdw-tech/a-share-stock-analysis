import pytest

from stock_analysis.models import StrategyEvaluation
from stock_analysis.position import recommend_position_action


def _result(score: float, signal: str) -> StrategyEvaluation:
    return StrategyEvaluation(
        strategy_name="测试策略",
        as_of=None,
        data_status="ok",
        score=score,
        signal=signal,
        confidence=70.0,
    )


def test_buy_candidate_uses_current_holding_ratio_for_top_up():
    guidance = recommend_position_action(_result(75, "买入候选"), 0.02)

    assert guidance.suggested_buy_ratio == 0.08
    assert guidance.suggested_sell_ratio_total == 0
    assert guidance.target_ratio == 0.10
    assert guidance.max_ratio == 0.25


def test_hold_signal_reduces_only_position_above_maximum():
    guidance = recommend_position_action(_result(60, "观望/持有"), 0.30)

    assert guidance.suggested_buy_ratio == 0
    assert guidance.suggested_sell_ratio_total == 0.05
    assert guidance.suggested_sell_ratio_current == pytest.approx(1 / 6, abs=1e-5)


def test_sell_signal_reduces_fraction_of_existing_position():
    guidance = recommend_position_action(_result(40, "减仓/卖出倾向"), 0.20)

    assert guidance.suggested_buy_ratio == 0
    assert guidance.suggested_sell_ratio_total == 0.05
    assert guidance.suggested_sell_ratio_current == 0.25


def test_insufficient_data_has_no_position_percentage():
    result = StrategyEvaluation(
        strategy_name="测试策略",
        as_of=None,
        data_status="insufficient",
        score=None,
        signal="数据不足/不可判断",
        confidence=0,
        warnings=["行情不可用"],
    )

    guidance = recommend_position_action(result, 0.20)

    assert guidance.action == "数据不足/不可判断"
    assert guidance.suggested_buy_ratio is None
    assert guidance.suggested_sell_ratio_total is None
