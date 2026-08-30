from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .models import IndicatorSnapshot, StrategyConfig
from .scoring import evaluate_stock
from .strategy import DEFAULT_STRATEGY, STRATEGY_REQUIRED_COLUMNS, evaluate_strategy


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
    strategy_name: str = "基础综合评分"
    avg_loss_5d: float | None = None
    avg_loss_20d: float | None = None
    win_rate_actual: float | None = None
    avg_net_return: float | None = None
    avg_win_actual: float | None = None
    avg_loss_actual: float | None = None
    profit_factor: float | None = None
    total_net_return: float | None = None
    max_drawdown_equity: float | None = None
    total_costs: float | None = None
    oos_signal_count: int = 0
    oos_win_rate: float | None = None
    oos_avg_net_return: float | None = None
    oos_max_drawdown: float | None = None
    sample_warning: str = ""


def _empty_signals() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_date": pd.Series(dtype="string"),
            "entry_date": pd.Series(dtype="string"),
            "score": pd.Series(dtype="float"),
            "entry_price": pd.Series(dtype="float"),
            "effective_entry_price": pd.Series(dtype="float"),
            "shares": pd.Series(dtype="int"),
            "stop_price": pd.Series(dtype="float"),
            "target_price": pd.Series(dtype="float"),
            "exit_date": pd.Series(dtype="string"),
            "exit_price": pd.Series(dtype="float"),
            "exit_reason": pd.Series(dtype="string"),
            "holding_days": pd.Series(dtype="int"),
            "gross_return": pd.Series(dtype="float"),
            "net_return": pd.Series(dtype="float"),
            "fees": pd.Series(dtype="float"),
            "return_5d": pd.Series(dtype="float"),
            "net_return_5d": pd.Series(dtype="float"),
            "return_20d": pd.Series(dtype="float"),
            "net_return_20d": pd.Series(dtype="float"),
            "max_drawdown_20d": pd.Series(dtype="float"),
            "is_oos": pd.Series(dtype="bool"),
        }
    )


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _summary_value(frame: pd.DataFrame, column: str, operation: str) -> float | None:
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    if operation == "win_rate":
        return float((values > 0).mean())
    if operation == "mean":
        return float(values.mean())
    if operation == "mean_loss":
        losses = values[values < 0]
        return None if losses.empty else float(losses.mean())
    if operation == "min":
        return float(values.min())
    raise ValueError(f"不支持的统计方式: {operation}")


def _fee(side: str, gross: float, config: StrategyConfig) -> float:
    commission = max(config.minimum_commission, gross * config.commission_rate)
    stamp_tax = gross * config.stamp_tax_rate if side == "卖出" else 0.0
    return commission + stamp_tax


def _position_size(entry_price: float, config: StrategyConfig, risk_per_share: float | None = None) -> int:
    position_budget = config.capital_per_trade * config.max_position_ratio
    position_shares = int(position_budget / entry_price / config.lot_size) * config.lot_size
    if risk_per_share is not None and risk_per_share > 0:
        risk_budget = config.capital_per_trade * config.risk_per_trade
        risk_shares = int(risk_budget / risk_per_share / config.lot_size) * config.lot_size
        position_shares = min(position_shares, risk_shares)
    return max(0, position_shares)


def _round_trip(
    raw_entry: float,
    raw_exit: float,
    config: StrategyConfig,
    shares: int | None = None,
) -> tuple[float, float, float]:
    """Return net return, round-trip costs, and effective entry price."""
    entry = raw_entry * (1 + config.slippage_rate)
    exit_price = raw_exit * (1 - config.slippage_rate)
    shares = shares if shares is not None else _position_size(entry, config)
    if shares <= 0:
        return float("nan"), 0.0, float(entry)
    buy_gross = entry * shares
    sell_gross = exit_price * shares
    costs = _fee("买入", buy_gross, config) + _fee("卖出", sell_gross, config)
    net_return = (sell_gross - _fee("卖出", sell_gross, config)) / (buy_gross + _fee("买入", buy_gross, config)) - 1
    return float(net_return), float(costs), float(entry)


def _equity_drawdown(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    equity = (1 + numeric).cumprod()
    return float((equity / equity.cummax() - 1).min())


def _profit_factor(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    profits = float(numeric[numeric > 0].sum())
    losses = float(-numeric[numeric < 0].sum())
    if losses == 0:
        return None if profits == 0 else float("inf")
    return profits / losses


def _report(
    records: list[dict[str, object]],
    *,
    strategy_name: str,
    message: str = "",
    oos_split_date: pd.Timestamp | None = None,
) -> BacktestReport:
    signals = pd.DataFrame.from_records(records) if records else _empty_signals()
    if records:
        signals = signals.sort_values("signal_date").reset_index(drop=True)
    if oos_split_date is not None and not signals.empty:
        signals["is_oos"] = pd.to_datetime(signals["signal_date"]) >= oos_split_date
    elif "is_oos" not in signals:
        signals["is_oos"] = False

    sample_warning = ""
    if 0 < len(signals) < 30:
        sample_warning = f"样本不足，胜率参考价值有限：当前只有{len(signals)}个信号，建议至少30个。"
    oos = signals[signals["is_oos"]] if not signals.empty else signals
    actual = signals["net_return"] if "net_return" in signals else pd.Series(dtype=float)
    oos_actual = oos["net_return"] if "net_return" in oos else pd.Series(dtype=float)
    full_message = "；".join(item for item in (message, sample_warning) if item)
    return BacktestReport(
        signals=signals,
        signal_count=len(signals),
        win_rate_5d=_summary_value(signals, "return_5d", "win_rate"),
        avg_return_5d=_summary_value(signals, "return_5d", "mean"),
        win_rate_20d=_summary_value(signals, "return_20d", "win_rate"),
        avg_return_20d=_summary_value(signals, "return_20d", "mean"),
        max_drawdown_20d=_summary_value(signals, "max_drawdown_20d", "min"),
        message=full_message,
        strategy_name=strategy_name,
        avg_loss_5d=_summary_value(signals, "return_5d", "mean_loss"),
        avg_loss_20d=_summary_value(signals, "return_20d", "mean_loss"),
        win_rate_actual=_summary_value(signals, "net_return", "win_rate"),
        avg_net_return=_summary_value(signals, "net_return", "mean"),
        avg_win_actual=(
            float(pd.to_numeric(actual, errors="coerce").dropna().pipe(lambda values: values[values > 0]).mean())
            if not actual.empty and (pd.to_numeric(actual, errors="coerce") > 0).any()
            else None
        ),
        avg_loss_actual=_summary_value(signals, "net_return", "mean_loss"),
        profit_factor=_profit_factor(actual),
        total_net_return=(float((1 + pd.to_numeric(actual, errors="coerce").dropna()).prod() - 1) if not actual.empty else None),
        max_drawdown_equity=_equity_drawdown(actual),
        total_costs=(float(pd.to_numeric(signals["fees"], errors="coerce").sum()) if "fees" in signals else None),
        oos_signal_count=len(oos),
        oos_win_rate=_summary_value(oos, "net_return", "win_rate"),
        oos_avg_net_return=_summary_value(oos, "net_return", "mean"),
        oos_max_drawdown=_equity_drawdown(oos_actual),
        sample_warning=sample_warning,
    )


def _point_snapshot(snapshot: IndicatorSnapshot, frame: pd.DataFrame, index: int) -> IndicatorSnapshot:
    row = frame.iloc[index]
    return IndicatorSnapshot(
        security=snapshot.security,
        frame=frame.iloc[: index + 1],
        latest=row.to_dict(),
        status=snapshot.status,
        message=snapshot.message,
    )


def _fixed_record(
    frame: pd.DataFrame,
    signal_index: int,
    score: float,
    config: StrategyConfig,
    *,
    is_oos: bool = False,
) -> dict[str, object] | None:
    entry_row = frame.iloc[signal_index + 1]
    entry_price = _finite_float(entry_row["open"])
    close_5 = _finite_float(frame.iloc[signal_index + 5]["close"])
    close_20 = _finite_float(frame.iloc[signal_index + 20]["close"])
    if entry_price is None or entry_price <= 0 or close_5 is None or close_20 is None:
        return None
    effective_entry = entry_price * (1 + config.slippage_rate)
    shares = _position_size(effective_entry, config)
    if shares <= 0:
        return None
    net_5, costs_5, effective_entry = _round_trip(entry_price, close_5, config, shares)
    net_20, costs_20, _ = _round_trip(entry_price, close_20, config, shares)
    future_lows = pd.to_numeric(
        frame.iloc[signal_index + 1 : signal_index + max(HOLDING_DAYS) + 1]["low"],
        errors="coerce",
    )
    if future_lows.isna().any():
        return None
    return {
        "signal_date": frame.iloc[signal_index]["date"].date().isoformat(),
        "entry_date": entry_row["date"].date().isoformat(),
        "score": float(score),
        "entry_price": entry_price,
        "effective_entry_price": effective_entry,
        "shares": shares,
        "stop_price": np.nan,
        "target_price": np.nan,
        "exit_date": frame.iloc[signal_index + 20]["date"].date().isoformat(),
        "exit_price": close_20,
        "exit_reason": "固定20日退出",
        "holding_days": 20,
        "gross_return": close_20 / entry_price - 1,
        "net_return": net_20,
        "fees": costs_20,
        "return_5d": close_5 / entry_price - 1,
        "net_return_5d": net_5,
        "return_20d": close_20 / entry_price - 1,
        "net_return_20d": net_20,
        "max_drawdown_20d": min(0.0, float((future_lows / effective_entry - 1).min())),
        "is_oos": is_oos,
    }


def backtest_buy_signals(snapshot: IndicatorSnapshot) -> BacktestReport:
    """Backtest the original composite-score rule with the shared cost model."""
    if snapshot.status != "ok":
        return _report([], strategy_name="基础综合评分", message=snapshot.message or "当前行情状态不可用于历史验证。")
    missing = sorted(REQUIRED_COLUMNS - set(snapshot.frame.columns))
    if missing:
        return _report([], strategy_name="基础综合评分", message=f"历史验证缺少必要字段：{', '.join(missing)}")
    frame = snapshot.frame.copy().sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if len(frame) <= max(HOLDING_DAYS) or frame["date"].isna().any():
        return _report([], strategy_name="基础综合评分", message="可用于历史验证的行情长度不足，至少需要21个有效交易日。")

    records: list[dict[str, object]] = []
    previous_buy = False
    config = DEFAULT_STRATEGY
    last_signal_index = len(frame) - max(HOLDING_DAYS) - 1
    split_date = frame.iloc[int(len(frame) * 0.7)]["date"]
    for index in range(last_signal_index + 1):
        point = _point_snapshot(snapshot, frame, index)
        result = evaluate_stock(point, {}, "short", today=frame.iloc[index]["date"].date())
        is_buy = result.data_status == "ok" and result.signal == BUY_SIGNAL
        if is_buy and not previous_buy:
            record = _fixed_record(
                frame,
                index,
                float(result.score),
                config,
                is_oos=frame.iloc[index]["date"] >= split_date,
            )
            if record is not None:
                records.append(record)
        previous_buy = is_buy
    message = "" if records else "在可评估的历史区间内，没有出现可统计的短线买入候选信号。"
    return _report(records, strategy_name="基础综合评分", message=message, oos_split_date=split_date)


def _strategy_trade(
    frame: pd.DataFrame,
    signal_index: int,
    evaluation,
    config: StrategyConfig,
    *,
    is_oos: bool,
) -> tuple[dict[str, object] | None, int | None]:
    entry_index = signal_index + 1
    entry_row = frame.iloc[entry_index]
    raw_entry = _finite_float(entry_row["open"])
    atr = _finite_float(evaluation.key_metrics.get("ATR14"))
    if raw_entry is None or raw_entry <= 0 or atr is None or atr <= 0:
        return None, None
    _, _, effective_entry = _round_trip(raw_entry, raw_entry, config)
    shares = _position_size(effective_entry, config, atr * config.stop_atr_multiple)
    if shares <= 0:
        return None, None
    stop = effective_entry - config.stop_atr_multiple * atr
    target = effective_entry + config.target_atr_multiple * atr
    last_index = min(len(frame) - 1, signal_index + config.max_holding_days)
    exit_index = None
    raw_exit = None
    exit_reason = ""
    for index in range(entry_index + 1, last_index + 1):
        row = frame.iloc[index]
        open_price = _finite_float(row["open"])
        high = _finite_float(row["high"])
        low = _finite_float(row["low"])
        close = _finite_float(row["close"])
        if any(value is None for value in (open_price, high, low, close)):
            return None, None
        if open_price <= stop:
            exit_index, raw_exit, exit_reason = index, open_price, "止损（跳空）"
        elif open_price >= target:
            exit_index, raw_exit, exit_reason = index, open_price, "止盈（跳空）"
        elif low <= stop and high >= target:
            exit_index, raw_exit, exit_reason = index, stop, "止损优先（同日触及止损止盈）"
        elif low <= stop:
            exit_index, raw_exit, exit_reason = index, stop, "止损"
        elif high >= target:
            exit_index, raw_exit, exit_reason = index, target, "止盈"
        else:
            row_sma20 = _finite_float(row.get("sma20"))
            row_macd = _finite_float(row.get("macd_hist"))
            trend_exit = row_sma20 is not None and row_macd is not None and close < row_sma20 and row_macd < 0
            if trend_exit:
                if index + 1 > last_index or index + 1 >= len(frame):
                    exit_index, raw_exit, exit_reason = index, close, "趋势退出（持有期末收盘）"
                else:
                    next_open = _finite_float(frame.iloc[index + 1]["open"])
                    if next_open is None or next_open <= 0:
                        return None, None
                    exit_index, raw_exit, exit_reason = index + 1, next_open, "趋势退出（次日开盘）"
            elif index == last_index:
                exit_index, raw_exit, exit_reason = index, close, "最长持有期到期"
        if exit_index is not None:
            break
    if exit_index is None or raw_exit is None:
        return None, None

    net_return, costs, _ = _round_trip(raw_entry, raw_exit, config, shares)
    close_5 = _finite_float(frame.iloc[signal_index + 5]["close"])
    close_20 = _finite_float(frame.iloc[signal_index + 20]["close"])
    if close_5 is None or close_20 is None:
        return None, None
    net_5, _, _ = _round_trip(raw_entry, close_5, config, shares)
    net_20, _, _ = _round_trip(raw_entry, close_20, config, shares)
    future_lows = pd.to_numeric(
        frame.iloc[entry_index : signal_index + max(HOLDING_DAYS) + 1]["low"], errors="coerce"
    )
    if future_lows.isna().any():
        return None, None
    return {
        "signal_date": frame.iloc[signal_index]["date"].date().isoformat(),
        "entry_date": entry_row["date"].date().isoformat(),
        "score": float(evaluation.score),
        "entry_price": raw_entry,
        "effective_entry_price": _round_trip(raw_entry, raw_entry, config)[2],
        "shares": shares,
        "stop_price": stop,
        "target_price": target,
        "exit_date": frame.iloc[exit_index]["date"].date().isoformat(),
        "exit_price": raw_exit,
        "exit_reason": exit_reason,
        "holding_days": exit_index - entry_index + 1,
        "gross_return": raw_exit / raw_entry - 1,
        "net_return": net_return,
        "fees": costs,
        "return_5d": close_5 / raw_entry - 1,
        "net_return_5d": net_5,
        "return_20d": close_20 / raw_entry - 1,
        "net_return_20d": net_20,
        "max_drawdown_20d": min(0.0, float((future_lows / _round_trip(raw_entry, raw_entry, config)[2] - 1).min())),
        "is_oos": is_oos,
    }, exit_index


def backtest_strategy(
    snapshot: IndicatorSnapshot,
    config: StrategyConfig = DEFAULT_STRATEGY,
    validation_mode: str = "rolling",
) -> BacktestReport:
    """Backtest the optimized strategy using only information available per signal date."""
    if validation_mode not in {"rolling", "all"}:
        raise ValueError("validation_mode必须是rolling或all")
    if snapshot.status != "ok":
        return _report([], strategy_name=config.name, message=snapshot.message or "当前行情不可用于策略回测。")
    missing = sorted(STRATEGY_REQUIRED_COLUMNS - set(snapshot.frame.columns))
    if missing:
        return _report([], strategy_name=config.name, message=f"策略回测缺少必要字段：{', '.join(missing)}")
    frame = snapshot.frame.copy().sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if len(frame) <= config.max_holding_days or frame["date"].isna().any():
        return _report([], strategy_name=config.name, message=f"策略回测至少需要{config.max_holding_days + 1}个有效交易日。")

    records: list[dict[str, object]] = []
    previous_buy = False
    next_allowed_index = 0
    last_signal_index = len(frame) - config.max_holding_days - 1
    split_date = None if validation_mode == "all" else frame.iloc[int(len(frame) * 0.7)]["date"]
    for index in range(last_signal_index + 1):
        point = _point_snapshot(snapshot, frame, index)
        evaluation = evaluate_strategy(point, config, today=frame.iloc[index]["date"].date())
        is_buy = evaluation.data_status == "ok" and evaluation.signal == BUY_SIGNAL
        if is_buy and not previous_buy and index >= next_allowed_index:
            record, exit_index = _strategy_trade(
                frame,
                index,
                evaluation,
                config,
                is_oos=split_date is not None and frame.iloc[index]["date"] >= split_date,
            )
            if record is not None:
                records.append(record)
                if exit_index is not None:
                    next_allowed_index = exit_index + config.cooldown_days + 1
        previous_buy = is_buy
    message = "" if records else "在可评估的历史区间内，没有出现满足全部过滤条件的优化策略信号。"
    return _report(records, strategy_name=config.name, message=message, oos_split_date=split_date)
