from __future__ import annotations

import numpy as np
import pandas as pd

from .models import IndicatorSnapshot, PriceHistory


MINIMUM_BARS = 120


def calculate_indicators(price_history: PriceHistory) -> IndicatorSnapshot:
    if price_history.data.empty:
        raise ValueError(price_history.message or "没有可用行情数据")
    frame = price_history.data.copy()
    if len(frame) < MINIMUM_BARS:
        raise ValueError(f"历史数据不足，至少需要 {MINIMUM_BARS} 个交易日")
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)

    frame["sma5"] = close.rolling(5).mean()
    frame["sma20"] = close.rolling(20).mean()
    frame["sma60"] = close.rolling(60).mean()
    frame["sma120"] = close.rolling(120).mean()
    frame["ema12"] = close.ewm(span=12, adjust=False).mean()
    frame["ema26"] = close.ewm(span=26, adjust=False).mean()
    frame["macd"] = frame["ema12"] - frame["ema26"]
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    frame["macd_hist"] = frame["macd"] - frame["macd_signal"]

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    frame["rsi14"] = 100 - (100 / (1 + relative_strength))
    frame.loc[(loss == 0) & (gain > 0), "rsi14"] = 100

    frame["bb_mid"] = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    frame["bb_upper"] = frame["bb_mid"] + 2 * std20
    frame["bb_lower"] = frame["bb_mid"] - 2 * std20
    frame["return1"] = close.pct_change()
    frame["volatility20"] = frame["return1"].rolling(20).std() * np.sqrt(252)
    frame["volume_avg20"] = volume.rolling(20).mean()
    frame["drawdown"] = close / close.cummax() - 1

    latest = frame.iloc[-1].to_dict()
    return IndicatorSnapshot(price_history.security, frame, latest, price_history.status, price_history.message)
