from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_analysis.backtest import backtest_strategy
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import IndicatorSnapshot, PriceHistory, Security
from stock_analysis.strategy import DEFAULT_STRATEGY, evaluate_strategy


def build_strategy_frame(periods: int = 70) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=periods)
    close = np.linspace(10.0, 12.0, periods)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(periods, 130_000.0),
            "amount": close * 130_000,
            "sma20": close - 0.5,
            "sma60": close - 1.0,
            "sma60_slope20": np.full(periods, 0.02),
            "macd_hist": np.full(periods, 0.2),
            "macd_hist_change": np.full(periods, 0.03),
            "rsi14": np.full(periods, 60.0),
            "roc20": np.full(periods, 0.08),
            "roc60": np.full(periods, 0.18),
            "volume_ratio20": np.full(periods, 1.3),
            "volume_avg20": np.full(periods, 100_000.0),
            "volatility20": np.full(periods, 0.25),
            "drawdown": np.zeros(periods),
            "atr14": np.full(periods, 1.0),
            "atr_ratio": np.full(periods, 0.1),
            "high20_prev": close - 0.1,
            "return1": np.full(periods, 0.01),
        }
    )


def build_snapshot(frame: pd.DataFrame) -> IndicatorSnapshot:
    security = Security("600519", "测试股票", "SSE")
    return IndicatorSnapshot(security, frame, frame.iloc[-1].to_dict(), "ok", "")


def test_indicator_extensions_are_present_and_use_prior_bars():
    periods = 180
    dates = pd.bdate_range("2025-01-01", periods=periods)
    close = 20 + np.linspace(0, 3, periods) + np.sin(np.arange(periods) / 5)
    raw = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(periods, 100_000.0),
            "amount": close * 100_000,
        }
    )
    snapshot = calculate_indicators(PriceHistory(Security("600519", "测试股票", "SSE"), raw, "test"))

    expected_columns = {
        "roc20",
        "roc60",
        "atr14",
        "atr_ratio",
        "sma60_slope20",
        "macd_hist_change",
        "volume_ratio20",
        "high20_prev",
    }
    assert expected_columns.issubset(snapshot.frame.columns)
    index = 130
    assert snapshot.frame.loc[index, "high20_prev"] == pytest.approx(
        raw.loc[index - 20 : index - 1, "high"].max()
    )


def test_strategy_requires_all_buy_filters_and_explains_conditions():
    frame = build_strategy_frame(1)
    result = evaluate_strategy(build_snapshot(frame), today=date(2025, 1, 1))

    assert result.signal == "买入候选"
    assert result.score is not None and result.score >= 70
    assert len(result.entry_conditions) == 5
    assert result.exit_conditions
    assert result.risk_controls


def test_score_can_pass_but_trend_filter_blocks_buy_signal():
    frame = build_strategy_frame(1)
    frame.loc[0, "close"] = 9.4
    result = evaluate_strategy(build_snapshot(frame), today=date(2025, 1, 1))

    assert result.score is not None and result.score >= 70
    assert result.signal != "买入候选"
    assert any("趋势" in condition and "未通过" in condition for condition in result.entry_conditions)


def test_high_volatility_blocks_buy_signal_even_when_score_is_high():
    frame = build_strategy_frame(1)
    frame.loc[0, "volatility20"] = 0.70
    result = evaluate_strategy(build_snapshot(frame), today=date(2025, 1, 1))

    assert result.score is not None and result.score >= 70
    assert result.signal != "买入候选"
    assert any("风险过滤未通过" in warning for warning in result.warnings)


def test_strategy_backtest_uses_costs_and_stop_loss_priority():
    frame = build_strategy_frame()
    frame.loc[2, "high"] = 13.0
    frame.loc[2, "low"] = 8.0
    report = backtest_strategy(build_snapshot(frame), DEFAULT_STRATEGY)

    assert report.signal_count == 1
    trade = report.signals.iloc[0]
    assert "止损优先" in trade["exit_reason"]
    assert trade["fees"] > 0
    assert trade["shares"] % DEFAULT_STRATEGY.lot_size == 0
    assert trade["shares"] * trade["effective_entry_price"] <= (
        DEFAULT_STRATEGY.capital_per_trade * DEFAULT_STRATEGY.max_position_ratio + 1e-6
    )
    assert trade["net_return"] < trade["gross_return"]
    assert report.profit_factor is not None
    assert report.sample_warning


def test_strategy_backtest_uses_next_open_and_has_out_of_sample_stats():
    frame = build_strategy_frame()
    frame.loc[2, "high"] = 13.0
    report = backtest_strategy(build_snapshot(frame), DEFAULT_STRATEGY)

    trade = report.signals.iloc[0]
    assert trade["entry_date"] == frame.iloc[1]["date"].date().isoformat()
    assert trade["entry_price"] == pytest.approx(frame.iloc[1]["open"])
    assert report.oos_signal_count == 0


def test_trend_exit_at_holding_limit_does_not_extend_holding_period():
    frame = build_strategy_frame()
    frame.loc[20, "close"] = 8.0
    frame.loc[20, "sma20"] = 9.0
    frame.loc[20, "macd_hist"] = -0.2
    frame.loc[20, "high"] = 8.1
    frame.loc[20, "low"] = 7.9
    report = backtest_strategy(build_snapshot(frame), DEFAULT_STRATEGY)

    trade = report.signals.iloc[0]
    assert trade["holding_days"] <= DEFAULT_STRATEGY.max_holding_days
    assert "持有期末收盘" in trade["exit_reason"] or trade["exit_reason"] == "止损"


def test_strategy_does_not_exit_on_entry_day_under_t_plus_one():
    frame = build_strategy_frame()
    frame.loc[1, "low"] = 0.1
    frame.loc[1, "high"] = 20.0
    report = backtest_strategy(build_snapshot(frame), DEFAULT_STRATEGY)

    trade = report.signals.iloc[0]
    assert trade["exit_date"] != frame.iloc[1]["date"].date().isoformat()


def test_strategy_signal_does_not_use_prices_after_signal_date():
    baseline_frame = build_strategy_frame()
    baseline_frame.loc[2, "high"] = 13.0
    baseline = backtest_strategy(build_snapshot(baseline_frame), DEFAULT_STRATEGY)
    altered_frame = baseline_frame.copy()
    altered_frame.loc[1:, ["open", "high", "low", "close"]] = 1.0
    altered = backtest_strategy(build_snapshot(altered_frame), DEFAULT_STRATEGY)

    assert altered.signals.iloc[0]["signal_date"] == baseline.signals.iloc[0]["signal_date"]
    assert altered.signals.iloc[0]["score"] == pytest.approx(baseline.signals.iloc[0]["score"])
