from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from .models import IndicatorSnapshot, ScoreComponent, StrategyConfig, StrategyEvaluation


DEFAULT_STRATEGY = StrategyConfig()
STRATEGY_REQUIRED_COLUMNS = {
    "date",
    "open",
    "high",
    "low",
    "close",
    "sma20",
    "sma60",
    "sma60_slope20",
    "macd_hist",
    "macd_hist_change",
    "rsi14",
    "roc20",
    "roc60",
    "volume_ratio20",
    "volatility20",
    "drawdown",
    "atr14",
    "atr_ratio",
    "high20_prev",
}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _clip(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def _component(name: str, score: float | None, weight: float, detail: str) -> ScoreComponent:
    return ScoreComponent(name, None if score is None else _clip(score), weight, detail, score is None)


def _trend(latest: dict[str, Any]) -> ScoreComponent:
    close, sma20, sma60, slope = (
        _num(latest.get(key)) for key in ("close", "sma20", "sma60", "sma60_slope20")
    )
    if any(value is None for value in (close, sma20, sma60, slope)):
        return _component("趋势过滤", None, 0.35, "收盘价、均线或均线斜率数据不足")

    score = 50.0
    notes: list[str] = []
    if close > sma20:
        score += 15
        notes.append("收盘价在20日均线上方")
    else:
        score -= 15
        notes.append("收盘价在20日均线下方")
    if sma20 > sma60:
        score += 20
        notes.append("20日均线在60日均线上方")
    else:
        score -= 20
        notes.append("20日均线在60日均线下方")
    if slope > 0:
        score += 15
        notes.append(f"60日均线20日斜率 {slope:.2%}，向上")
    else:
        score -= 15
        notes.append(f"60日均线20日斜率 {slope:.2%}，未向上")
    return _component("趋势过滤", score, 0.35, "；".join(notes))


def _momentum(latest: dict[str, Any], config: StrategyConfig) -> ScoreComponent:
    roc20, roc60, macd, macd_change, rsi = (
        _num(latest.get(key))
        for key in ("roc20", "roc60", "macd_hist", "macd_hist_change", "rsi14")
    )
    if any(value is None for value in (roc20, roc60, macd, macd_change, rsi)):
        return _component("动量确认", None, 0.30, "20/60日动量、MACD或RSI数据不足")

    score = 50.0
    notes: list[str] = []
    if roc20 > 0:
        score += 15
        notes.append(f"20日收益率 {roc20:.2%} 为正")
    else:
        score -= 15
        notes.append(f"20日收益率 {roc20:.2%} 为负")
    if roc60 > 0:
        score += 15
        notes.append(f"60日收益率 {roc60:.2%} 为正")
    else:
        score -= 15
        notes.append(f"60日收益率 {roc60:.2%} 为负")
    if macd > 0:
        score += 10
        notes.append("MACD柱为正")
    else:
        score -= 10
        notes.append("MACD柱为负")
    if macd_change > 0:
        score += 5
        notes.append("MACD柱较前一日改善")
    else:
        score -= 5
        notes.append("MACD柱较前一日走弱")
    if config.rsi_min <= rsi <= config.rsi_max:
        score += 10
        notes.append(f"RSI {rsi:.1f} 位于{config.rsi_min:.0f}-{config.rsi_max:.0f}适中区间")
    elif rsi > config.rsi_max:
        score -= 15
        notes.append(f"RSI {rsi:.1f} 高于{config.rsi_max:.0f}，短线偏热")
    else:
        score -= 5
        notes.append(f"RSI {rsi:.1f} 低于{config.rsi_min:.0f}，动量尚未确认")
    return _component("动量确认", score, 0.30, "；".join(notes))


def _volume(latest: dict[str, Any], config: StrategyConfig) -> ScoreComponent:
    ratio, change = _num(latest.get("volume_ratio20")), _num(latest.get("return1"))
    if ratio is None or change is None:
        return _component("量价确认", None, 0.15, "成交量比率或日涨跌幅数据不足")

    if change > 0 and ratio >= 1.2:
        score, detail = 90.0, f"上涨且放量，量比 {ratio:.2f}"
    elif change > 0 and ratio >= config.min_volume_ratio:
        score, detail = 72.0, f"上涨且成交量达到20日均量，量比 {ratio:.2f}"
    elif change < 0 and ratio >= 1.2:
        score, detail = 25.0, f"下跌且放量，量比 {ratio:.2f}，卖压需警惕"
    elif ratio < 0.7:
        score, detail = 40.0, f"成交量偏低，量比 {ratio:.2f}，确认度有限"
    else:
        score, detail = 55.0, f"量价信号一般，量比 {ratio:.2f}"
    return _component("量价确认", score, 0.15, detail)


def _risk(latest: dict[str, Any], config: StrategyConfig) -> ScoreComponent:
    volatility, drawdown, atr_ratio, atr = (
        _num(latest.get(key)) for key in ("volatility20", "drawdown", "atr_ratio", "atr14")
    )
    if any(value is None for value in (volatility, drawdown, atr_ratio, atr)):
        return _component("波动风险", None, 0.20, "波动率、回撤或ATR数据不足")

    score = 80.0
    notes = [f"年化波动率 {volatility:.1%}", f"当前回撤 {drawdown:.1%}", f"ATR占收盘价 {atr_ratio:.2%}"]
    if volatility > config.max_volatility:
        score -= 35
    elif volatility > 0.40:
        score -= 15
    if drawdown < config.max_drawdown:
        score -= 30
    elif drawdown < -0.15:
        score -= 10
    return _component("波动风险", score, 0.20, "；".join(notes))


def _condition(ok: bool, text: str) -> str:
    return f"{'通过' if ok else '未通过'}：{text}"


def evaluate_strategy(
    snapshot: IndicatorSnapshot,
    config: StrategyConfig = DEFAULT_STRATEGY,
    *,
    today: date | None = None,
) -> StrategyEvaluation:
    """Evaluate the transparent swing strategy using data available as of one date."""
    today = today or date.today()
    latest = snapshot.latest
    warnings: list[str] = []
    if snapshot.status != "ok":
        warnings.append(snapshot.message or "行情数据状态异常，不能生成优化策略信号")
    if snapshot.as_of is None:
        warnings.append("缺少最新行情日期")
    elif snapshot.as_of > today:
        warnings.append(f"行情日期异常：最新日期 {snapshot.as_of} 晚于分析日期 {today}")
    elif (today - snapshot.as_of).days > 7:
        warnings.append(f"行情数据已超过7天未更新（最新日期：{snapshot.as_of}）")
    if snapshot.security.market_status != "正常":
        warnings.append(f"股票状态：{snapshot.security.market_status}")

    missing = sorted(STRATEGY_REQUIRED_COLUMNS - set(snapshot.frame.columns))
    if missing:
        warnings.append(f"优化策略缺少必要字段：{', '.join(missing)}")
    if warnings:
        return StrategyEvaluation(
            strategy_name=config.name,
            as_of=snapshot.as_of,
            data_status="insufficient",
            score=None,
            signal="数据不足/不可判断",
            confidence=0.0,
            warnings=warnings,
            exit_conditions=[
                f"跌破20日均线且MACD柱转负时，下一交易日开盘退出",
                f"参考止损：入场价下方{config.stop_atr_multiple:.2f}×ATR14",
                f"最长持有{config.max_holding_days}个交易日，期满退出",
            ],
            risk_controls=["数据不足时不生成买入或卖出方向性结论"],
        )

    components = [
        _trend(latest),
        _momentum(latest, config),
        _volume(latest, config),
        _risk(latest, config),
    ]
    component_missing = [item.name for item in components if item.missing]
    if component_missing:
        warnings.append(f"优化策略缺少指标：{', '.join(component_missing)}")
        return StrategyEvaluation(
            strategy_name=config.name,
            as_of=snapshot.as_of,
            data_status="insufficient",
            score=None,
            signal="数据不足/不可判断",
            confidence=0.0,
            components=components,
            warnings=warnings,
            exit_conditions=["数据恢复完整后再判断卖出规则"],
            risk_controls=["关键指标缺失时不生成方向性信号"],
        )

    score = _clip(sum(float(item.score) * item.weight for item in components))
    close = _num(latest.get("close"))
    sma20 = _num(latest.get("sma20"))
    sma60 = _num(latest.get("sma60"))
    slope = _num(latest.get("sma60_slope20"))
    roc20 = _num(latest.get("roc20"))
    roc60 = _num(latest.get("roc60"))
    macd = _num(latest.get("macd_hist"))
    macd_change = _num(latest.get("macd_hist_change"))
    rsi = _num(latest.get("rsi14"))
    volume_ratio = _num(latest.get("volume_ratio20"))
    volatility = _num(latest.get("volatility20"))
    drawdown = _num(latest.get("drawdown"))
    atr = _num(latest.get("atr14"))

    trend_ok = close > sma20 > sma60 and slope > 0
    momentum_ok = roc20 > 0 and roc60 > 0 and macd > 0 and macd_change > 0 and config.rsi_min <= rsi <= config.rsi_max
    volume_ok = volume_ratio >= config.min_volume_ratio
    risk_ok = volatility <= config.max_volatility and drawdown >= config.max_drawdown
    buy_ok = score >= config.score_threshold and trend_ok and momentum_ok and volume_ok and risk_ok
    trend_break = close < sma20 and macd < 0

    if buy_ok:
        signal = "买入候选"
    elif trend_break or score < 45:
        signal = "减仓/卖出倾向"
    else:
        signal = "观望/持有"

    confirmation_count = sum((trend_ok, momentum_ok, volume_ok, risk_ok))
    confidence = round(min(95.0, 55.0 + abs(score - 50.0) * 0.6 + confirmation_count * 2), 1)
    reasons = [f"{item.name} {item.score:.1f}分：{item.detail}" for item in components]
    entry_conditions = [
        _condition(trend_ok, f"趋势：收盘价高于20日均线，20日均线高于60日均线，60日均线斜率为正"),
        _condition(momentum_ok, f"动量：20/60日收益率为正、MACD柱为正且改善、RSI在{config.rsi_min:.0f}-{config.rsi_max:.0f}"),
        _condition(volume_ok, f"量价：成交量比20日均量不低于{config.min_volume_ratio:.1f}倍"),
        _condition(risk_ok, f"风险：年化波动率不高于{config.max_volatility:.0%}，回撤不低于{config.max_drawdown:.0%}"),
        _condition(buy_ok, f"综合分数 {score:.1f} 分达到{config.score_threshold:.0f}分且全部买入过滤条件通过"),
    ]
    exit_conditions = [
        f"止损：入场价下方{config.stop_atr_multiple:.2f}×ATR14，当前趋势退出条件{'已满足' if trend_break else '未满足'}",
        f"止盈：入场价上方{config.target_atr_multiple:.2f}×ATR14",
        f"趋势退出：收盘价跌破20日均线且MACD柱转负，下一交易日开盘退出",
        f"时间退出：最多持有{config.max_holding_days}个交易日，到期使用收盘价退出",
    ]
    risk_controls = [
        f"参考止损价：{close - config.stop_atr_multiple * atr:.2f}（按当前收盘价估算）",
        f"参考止盈价：{close + config.target_atr_multiple * atr:.2f}（按当前收盘价估算）",
        f"单笔风险预算不超过虚拟资金的{config.risk_per_trade:.1%}，单笔最大仓位{config.max_position_ratio:.0%}",
        f"成交数量按{config.lot_size}股整数倍计算；买入后遵守A股T+1卖出限制",
    ]
    if not risk_ok:
        warnings.append("风险过滤未通过，当前不建议新开仓")
    if not volume_ok:
        warnings.append("量价确认未通过，等待成交量配合")

    return StrategyEvaluation(
        strategy_name=config.name,
        as_of=snapshot.as_of,
        data_status="ok",
        score=score,
        signal=signal,
        confidence=confidence,
        components=components,
        reasons=reasons,
        warnings=warnings,
        key_metrics={
            "最新价": close,
            "ATR14": atr,
            "ATR占比": _num(latest.get("atr_ratio")),
            "20日收益率": roc20,
            "60日收益率": roc60,
            "量比": volume_ratio,
            "趋势确认数": confirmation_count,
            "止损参考价": close - config.stop_atr_multiple * atr,
            "止盈参考价": close + config.target_atr_multiple * atr,
        },
        entry_conditions=entry_conditions,
        exit_conditions=exit_conditions,
        risk_controls=risk_controls,
    )
