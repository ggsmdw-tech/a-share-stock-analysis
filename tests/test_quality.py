from datetime import timedelta

import pandas as pd

from app import build_relative_performance_frame
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import PriceHistory, Security
from stock_analysis.quality import assess_data_quality


def _history(periods: int = 180, *, status: str = "ok") -> PriceHistory:
    dates = pd.bdate_range("2025-01-01", periods=periods)
    close = pd.Series(range(10, 10 + periods), dtype=float)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 100_000,
            "amount": close * 100_000,
        }
    )
    return PriceHistory(Security("600519", "测试股票", "SSE"), frame, "tencent", status=status)


def test_quality_report_requires_fresh_public_data_and_core_indicators():
    history = _history()
    indicators = calculate_indicators(history)
    report = assess_data_quality(
        history,
        indicators,
        {"pe": 20, "pb": 2, "roe": 15},
        today=history.as_of,
    )

    assert report.actionable is True
    assert report.level == "较高"
    assert report.score == 100.0
    assert report.financial_coverage == 0.5
    assert all(item.status in {"通过", "部分"} for item in report.checks)


def test_quality_report_rejects_stale_cache():
    history = _history(status="stale-cache")
    report = assess_data_quality(history, today=history.as_of + timedelta(days=1))

    assert report.actionable is False
    assert report.level == "不可直接判断"
    assert any("来源" in warning for warning in report.warnings)


def test_quality_report_rejects_short_or_invalid_ohlc_history():
    history = _history(100)
    history.data.loc[3, "low"] = 999
    report = assess_data_quality(history, today=history.as_of)

    assert report.actionable is False
    assert any(item.name == "行情完整性" and item.status == "不足" for item in report.checks)


def test_quality_report_rejects_missing_volume_disguised_as_zero():
    history = _history()
    history.data["volume"] = 0
    report = assess_data_quality(history, today=history.as_of)

    assert report.actionable is False
    assert any("成交量无效" in item.detail for item in report.checks)


def test_relative_performance_uses_common_dates_and_starts_at_100():
    security = Security("600519", "测试股票", "SSE")
    stock_dates = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    benchmark_dates = pd.to_datetime(["2026-01-02", "2026-01-06", "2026-01-07"])
    stock = PriceHistory(
        security,
        pd.DataFrame({"date": stock_dates, "close": [10.0, 11.0, 12.0]}),
        "tencent",
    )
    benchmark = PriceHistory(
        Security("000300", "沪深300", "SSE"),
        pd.DataFrame({"date": benchmark_dates, "close": [100.0, 101.0, 102.0]}),
        "tencent",
    )

    frame = build_relative_performance_frame(stock, benchmark)

    assert frame["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-02", "2026-01-06"]
    assert frame.iloc[0]["个股指数"] == 100.0
    assert frame.iloc[0]["沪深300指数"] == 100.0
    assert frame.iloc[-1]["个股指数"] == 120.0
