import numpy as np
import pandas as pd
import pytest

from stock_analysis.backtest import backtest_buy_signals
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import PriceHistory, Security


def build_uptrend_snapshot():
    dates = pd.bdate_range("2025-01-01", periods=220)
    close = np.linspace(10.0, 30.0, len(dates))
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(len(dates), 100_000.0),
            "amount": close * 100_000,
        }
    )
    security = Security("600519", "测试股票", "SSE")
    return frame, calculate_indicators(PriceHistory(security, frame, "test-fixture"))


def test_backtest_uses_next_open_and_forward_closes():
    _, snapshot = build_uptrend_snapshot()
    report = backtest_buy_signals(snapshot)

    assert report.signal_count == 1
    signal = report.signals.iloc[0]
    signal_index = snapshot.frame.index[
        snapshot.frame["date"] == pd.Timestamp(signal["signal_date"])
    ][0]
    expected_entry = snapshot.frame.iloc[signal_index + 1]["open"]
    expected_return_5d = snapshot.frame.iloc[signal_index + 5]["close"] / expected_entry - 1

    assert signal["entry_price"] == pytest.approx(expected_entry)
    assert signal["return_5d"] == pytest.approx(expected_return_5d)
    assert report.avg_return_5d == pytest.approx(expected_return_5d)


def test_signal_score_does_not_use_prices_after_signal_date():
    raw_frame, baseline_snapshot = build_uptrend_snapshot()
    baseline_report = backtest_buy_signals(baseline_snapshot)
    first_signal = baseline_report.signals.iloc[0]
    signal_index = raw_frame.index[
        raw_frame["date"] == pd.Timestamp(first_signal["signal_date"])
    ][0]

    altered = raw_frame.copy()
    altered.loc[signal_index + 1 :, "close"] = 1.0
    altered.loc[signal_index + 1 :, "open"] = 0.99
    altered.loc[signal_index + 1 :, "high"] = 1.01
    altered.loc[signal_index + 1 :, "low"] = 0.98
    altered_snapshot = calculate_indicators(
        PriceHistory(baseline_snapshot.security, altered, "test-fixture")
    )
    altered_report = backtest_buy_signals(altered_snapshot)

    assert altered_report.signals.iloc[0]["signal_date"] == first_signal["signal_date"]
    assert altered_report.signals.iloc[0]["score"] == pytest.approx(first_signal["score"])
