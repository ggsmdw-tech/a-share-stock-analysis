from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .models import IndicatorSnapshot
from .scoring import evaluate_stock


BUY_SIGNAL = "买入候选"
HOLDING_DAYS = (5, 20)
REQUIRED_COLUMNS = {
    "date",
    "open",
    "low",
    "close",
    "sma20",
    "sma60",
    "macd_hist",
    "rsi14",
    "volume",
    "volume_avg20",
    "volatility20",
    "drawdown",
}


@dataclass(frozen=True)
class BacktestReport:
    signals: pd.DataFrame
    signal_count: int
    win_rate_5d: float | None
    avg_return_5d: float | None
    win_rate_20d: float | None
    avg_return_20d: float | None
    max_drawdown_20d: float | None
    message: str = ""


def _empty_signals() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_date": pd.Series(dtype="string"),
            "entry_date": pd.Series(dtype="string"),
            "score": pd.Series(dtype="float"),
            "entry_price": pd.Series(dtype="float"),
            "return_5d": pd.Series(dtype="float"),
            "return_20d": pd.Series(dtype="float"),
            "max_drawdown_20d": pd.Series(dtype="float"),
        }
    )


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _summary_value(frame: pd.DataFrame, column: str, operation: str) -> float | None:
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    if operation == "win_rate":
        return float((values > 0).mean())
    if operation == "mean":
        return float(values.mean())
    if operation == "min":
        return float(values.min())
    raise ValueError(f"不支持的统计方式: {operation}")


def _report(records: list[dict[str, object]], message: str = "") -> BacktestReport:
    signals = pd.DataFrame.from_records(records) if records else _empty_signals()
    if records:
        signals = signals[
            [
                "signal_date",
                "entry_date",
                "score",
                "entry_price",
                "return_5d",
                "return_20d",
                "max_drawdown_20d",
            ]
        ]
    return BacktestReport(
        signals=signals,
        signal_count=len(signals),
        win_rate_5d=_summary_value(signals, "return_5d", "win_rate"),
        avg_return_5d=_summary_value(signals, "return_5d", "mean"),
        win_rate_20d=_summary_value(signals, "return_20d", "win_rate"),
        avg_return_20d=_summary_value(signals, "return_20d", "mean"),
        max_drawdown_20d=_summary_value(signals, "max_drawdown_20d", "min"),
        message=message,
    )


def backtest_buy_signals(snapshot: IndicatorSnapshot) -> BacktestReport:
    """Evaluate historical short-term buy signals without using future data.

    A signal is evaluated from the indicators available at that day's close. The
    simulated entry is the next trading day's open, and returns use later closes.
    Consecutive buy-candidate days count as one signal event.
    """
    if snapshot.status != "ok":
        return _report([], snapshot.message or "当前行情状态不可用于历史验证。")

    missing = sorted(REQUIRED_COLUMNS - set(snapshot.frame.columns))
    if missing:
        return _report([], f"历史验证缺少必要字段：{', '.join(missing)}")

    frame = (
        snapshot.frame.copy()
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if len(frame) <= max(HOLDING_DAYS) or frame["date"].isna().any():
        return _report([], "可用于历史验证的行情长度不足，至少需要21个有效交易日。")

    records: list[dict[str, object]] = []
    previous_buy = False
    last_signal_index = len(frame) - max(HOLDING_DAYS) - 1
    for index in range(last_signal_index + 1):
        row = frame.iloc[index]
        signal_date = row["date"].date()
        point_snapshot = IndicatorSnapshot(
            security=snapshot.security,
            frame=frame.iloc[: index + 1],
            latest=row.to_dict(),
            status=snapshot.status,
            message=snapshot.message,
        )
        result = evaluate_stock(point_snapshot, {}, "short", today=signal_date)
        is_buy = result.data_status == "ok" and result.signal == BUY_SIGNAL

        if is_buy and not previous_buy:
            entry_row = frame.iloc[index + 1]
            close_5 = _finite_float(frame.iloc[index + 5]["close"])
            close_20 = _finite_float(frame.iloc[index + 20]["close"])
            entry_price = _finite_float(entry_row["open"])
            future_lows = pd.to_numeric(
                frame.iloc[index + 1 : index + max(HOLDING_DAYS) + 1]["low"],
                errors="coerce",
            )
            if (
                entry_price is not None
                and entry_price > 0
                and close_5 is not None
                and close_20 is not None
                and not future_lows.isna().any()
            ):
                max_drawdown = min(0.0, float((future_lows / entry_price - 1).min()))
                records.append(
                    {
                        "signal_date": signal_date.isoformat(),
                        "entry_date": entry_row["date"].date().isoformat(),
                        "score": float(result.score),
                        "entry_price": entry_price,
                        "return_5d": close_5 / entry_price - 1,
                        "return_20d": close_20 / entry_price - 1,
                        "max_drawdown_20d": max_drawdown,
                    }
                )
        previous_buy = is_buy

    message = "" if records else "在可评估的历史区间内，没有出现可统计的短线买入候选信号。"
    return _report(records, message)
