from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

import numpy as np

from .models import AnalysisResult, IndicatorSnapshot, ScoreComponent


HORIZONS = {"short": "短线", "swing": "波段", "long": "中长期"}
WEIGHTS = {
    "short": {"趋势": 0.35, "动量": 0.25, "量价": 0.20, "风险": 0.20},
    "swing": {"趋势": 0.25, "动量": 0.20, "量价": 0.15, "估值": 0.20, "盈利质量": 0.20},
    "long": {"估值": 0.30, "盈利质量": 0.30, "成长": 0.20, "长期趋势": 0.20},
}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return None if np.isnan(number) else number
    except (TypeError, ValueError):
        return None


def _clip(score: float) -> float:
    return round(max(0.0, min(100.0, score)), 1)


def _component(name: str, score: float | None, weight: float, detail: str) -> ScoreComponent:
    return ScoreComponent(name, None if score is None else _clip(score), weight, detail, score is None)


def _trend(snapshot: IndicatorSnapshot) -> ScoreComponent:
    latest = snapshot.latest
    close, sma20, sma60 = (_num(latest.get(key)) for key in ("close", "sma20", "sma60"))
    if close is None or sma20 is None or sma60 is None:
        return _component("趋势", None, 0, "均线数据不足")
    score = 50.0
    notes = []
    if close > sma20:
        score += 18
        notes.append("收盘价在20日均线上方")
    else:
        score -= 18
        notes.append("收盘价在20日均线下方")
    if sma20 > sma60:
        score += 22
        notes.append("20日均线在60日均线上方")
    else:
        score -= 22
        notes.append("20日均线在60日均线下方")
    return _component("趋势", score, 0, "；".join(notes))


def _momentum(snapshot: IndicatorSnapshot) -> ScoreComponent:
    latest = snapshot.latest
    macd = _num(latest.get("macd_hist"))
    rsi = _num(latest.get("rsi14"))
    if macd is None or rsi is None:
        return _component("动量", None, 0, "MACD或RSI数据不足")
    score = 50.0
    notes = []
    if macd > 0:
        score += 25
        notes.append("MACD柱为正")
    else:
        score -= 25
        notes.append("MACD柱为负")
    if 45 <= rsi <= 70:
        score += 20
        notes.append(f"RSI {rsi:.1f}，动量适中")
    elif rsi > 80:
        score -= 25
        notes.append(f"RSI {rsi:.1f}，短线过热")
    elif rsi < 30:
        score -= 5
        notes.append(f"RSI {rsi:.1f}，处于超卖区")
    else:
        score += 5
        notes.append(f"RSI {rsi:.1f}")
    return _component("动量", score, 0, "；".join(notes))


def _volume(snapshot: IndicatorSnapshot) -> ScoreComponent:
    latest = snapshot.latest
    volume, average, change = (_num(latest.get(key)) for key in ("volume", "volume_avg20", "return1"))
    if volume is None or average is None or change is None or average <= 0:
        return _component("量价", None, 0, "成交量数据不足")
    ratio = volume / average
    score = 50.0
    if change > 0 and ratio >= 1.1:
        score += 30
        detail = f"上涨且放量，量比 {ratio:.2f}"
    elif change < 0 and ratio >= 1.1:
        score -= 25
        detail = f"下跌且放量，量比 {ratio:.2f}"
    elif ratio < 0.7:
        score -= 5
        detail = f"成交量偏低，量比 {ratio:.2f}"
    else:
        score += 5 if change > 0 else -5
        detail = f"量价信号一般，量比 {ratio:.2f}"
    return _component("量价", score, 0, detail)


def _risk(snapshot: IndicatorSnapshot) -> ScoreComponent:
    volatility = _num(snapshot.latest.get("volatility20"))
    drawdown = _num(snapshot.latest.get("drawdown"))
    if volatility is None or drawdown is None:
        return _component("风险", None, 0, "波动或回撤数据不足")
    score = 80.0
    notes = []
    if volatility > 0.60:
        score -= 35
        notes.append(f"年化波动率较高 {volatility:.1%}")
    elif volatility > 0.40:
        score -= 15
        notes.append(f"年化波动率偏高 {volatility:.1%}")
    else:
        notes.append(f"年化波动率 {volatility:.1%}")
    if drawdown < -0.30:
        score -= 30
        notes.append(f"历史回撤较大 {drawdown:.1%}")
    elif drawdown < -0.15:
        score -= 10
        notes.append(f"当前回撤 {drawdown:.1%}")
    return _component("风险", score, 0, "；".join(notes))


def _valuation(financials: dict[str, Any]) -> ScoreComponent:
    pe, pb = _num(financials.get("pe")), _num(financials.get("pb"))
    if pe is None and pb is None:
        return _component("估值", None, 0, "缺少PE和PB")
    scores, notes = [], []
    if pe is not None:
        if pe <= 15:
            scores.append(80)
        elif pe <= 30:
            scores.append(60)
        elif pe <= 50:
            scores.append(40)
        else:
            scores.append(20)
        notes.append(f"PE {pe:.1f}")
    if pb is not None:
        if pb <= 1.5:
            scores.append(80)
        elif pb <= 3:
            scores.append(60)
        elif pb <= 6:
            scores.append(40)
        else:
            scores.append(20)
        notes.append(f"PB {pb:.1f}")
    return _component("估值", sum(scores) / len(scores), 0, "；".join(notes))


def _quality(financials: dict[str, Any]) -> ScoreComponent:
    roe, debt = _num(financials.get("roe")), _num(financials.get("debt_ratio"))
    if roe is None and debt is None:
        return _component("盈利质量", None, 0, "缺少ROE和负债率")
    scores, notes = [], []
    if roe is not None:
        scores.append(80 if roe >= 15 else 65 if roe >= 10 else 45 if roe >= 5 else 25)
        notes.append(f"ROE {roe:.1f}%")
    if debt is not None:
        scores.append(80 if debt <= 40 else 60 if debt <= 60 else 35 if debt <= 80 else 15)
        notes.append(f"资产负债率 {debt:.1f}%")
    return _component("盈利质量", sum(scores) / len(scores), 0, "；".join(notes))


def _growth(financials: dict[str, Any]) -> ScoreComponent:
    revenue = _num(financials.get("revenue_growth"))
    profit = _num(financials.get("profit_growth"))
    if revenue is None and profit is None:
        return _component("成长", None, 0, "缺少营收或利润增速")
    scores, notes = [], []
    for value, label in ((revenue, "营收增速"), (profit, "利润增速")):
        if value is None:
            continue
        scores.append(85 if value >= 20 else 70 if value >= 10 else 55 if value >= 0 else 35 if value >= -10 else 15)
        notes.append(f"{label} {value:.1f}%")
    return _component("成长", sum(scores) / len(scores), 0, "；".join(notes))


def _long_trend(snapshot: IndicatorSnapshot) -> ScoreComponent:
    latest = snapshot.latest
    close, sma60, sma120 = (_num(latest.get(key)) for key in ("close", "sma60", "sma120"))
    if close is None or sma60 is None:
        return _component("长期趋势", None, 0, "长期均线数据不足")
    score = 50.0
    notes = []
    if close > sma60:
        score += 25
        notes.append("价格在60日均线上方")
    else:
        score -= 25
        notes.append("价格在60日均线下方")
    if sma120 is not None:
        if close > sma120:
            score += 20
            notes.append("价格在120日均线上方")
        else:
            score -= 20
            notes.append("价格在120日均线下方")
    return _component("长期趋势", score, 0, "；".join(notes))


def _signal(score: float) -> str:
    if score >= 70:
        return "买入候选"
    if score >= 45:
        return "观望/持有"
    return "减仓/卖出倾向"


def signal_for_score(score: float) -> str:
    """Public helper used by tests and future UI configuration."""
    return _signal(_clip(score))


def evaluate_stock(
    snapshot: IndicatorSnapshot,
    financials: dict[str, Any],
    horizon: str,
    *,
    today: date | None = None,
) -> AnalysisResult:
    if horizon not in HORIZONS:
        raise ValueError(f"不支持的分析周期: {horizon}")
    today = today or datetime.now().date()
    as_of = snapshot.as_of
    warnings: list[str] = []
    if snapshot.status == "stale-cache":
        warnings.append(snapshot.message or "正在使用本地缓存，数据不可直接用于买卖判断")
    elif snapshot.status != "ok":
        warnings.append(snapshot.message or "行情数据状态异常")
    if as_of is None:
        warnings.append("缺少最新行情日期")
    elif as_of > today:
        warnings.append(f"行情日期异常：最新日期 {as_of} 晚于分析日期 {today}")
    elif (today - as_of).days > 7:
        warnings.append(f"行情数据已超过7天未更新（最新日期：{as_of}）")
    if snapshot.security.market_status != "正常":
        warnings.append(f"股票状态：{snapshot.security.market_status}")
    warnings.extend(str(item) for item in financials.get("risk_flags", []))

    factories: dict[str, Callable[[], ScoreComponent]] = {
        "趋势": lambda: _trend(snapshot),
        "动量": lambda: _momentum(snapshot),
        "量价": lambda: _volume(snapshot),
        "风险": lambda: _risk(snapshot),
        "估值": lambda: _valuation(financials),
        "盈利质量": lambda: _quality(financials),
        "成长": lambda: _growth(financials),
        "长期趋势": lambda: _long_trend(snapshot),
    }
    components = [
        (lambda component: ScoreComponent(component.name, component.score, WEIGHTS[horizon][component.name], component.detail, component.missing))(factories[name]())
        for name in WEIGHTS[horizon]
    ]
    missing = [item.name for item in components if item.missing]
    if missing:
        warnings.append(f"缺少关键指标：{', '.join(missing)}")

    hard_stop = bool(warnings)
    score: float | None = None
    reasons: list[str] = []
    if not hard_stop:
        score = _clip(sum(float(item.score) * item.weight for item in components if item.score is not None))
        reasons = [
            f"{item.name} {item.score:.1f}分：{item.detail}"
            for item in components
            if item.score is not None
        ]
        signal = _signal(score)
        confidence = round(min(95.0, 55.0 + abs(score - 50.0) * 0.9), 1)
        data_status = "ok"
    else:
        signal = "数据不足/不可判断"
        confidence = 0.0
        data_status = "insufficient"

    key_metrics = {
        "最新价": snapshot.latest.get("close"),
        "涨跌幅": snapshot.latest.get("return1"),
        "RSI14": snapshot.latest.get("rsi14"),
        "MACD柱": snapshot.latest.get("macd_hist"),
        "PE": financials.get("pe"),
        "PB": financials.get("pb"),
        "ROE": financials.get("roe"),
        "营收增速": financials.get("revenue_growth"),
        "利润增速": financials.get("profit_growth"),
        "资产负债率": financials.get("debt_ratio"),
    }
    entry_conditions = [
        f"综合评分达到70分及以上时，{HORIZONS[horizon]}规则进入买入候选区间",
        "同时检查本周期所列趋势、动量、量价、估值或财务质量维度",
    ]
    exit_conditions = [
        "综合评分低于45分时，基础规则进入减仓/卖出倾向",
        "评分处于45–69.9分时，基础规则保持观望/持有，不单独触发买卖",
    ]
    risk_controls = [
        "关键指标缺失、数据过期、停牌或风险标记出现时，不生成方向性信号",
        "基础规则评分不是上涨概率、准确率或未来收益保证",
    ]
    return AnalysisResult(
        security=snapshot.security,
        horizon=HORIZONS[horizon],
        as_of=as_of,
        data_status=data_status,
        score=score,
        signal=signal,
        confidence=confidence,
        components=components,
        reasons=reasons,
        warnings=warnings,
        key_metrics=key_metrics,
        strategy_name="基础综合评分",
        entry_conditions=entry_conditions,
        exit_conditions=exit_conditions,
        risk_controls=risk_controls,
    )


def evaluate_all_horizons(
    snapshot: IndicatorSnapshot, financials: dict[str, Any], *, today: date | None = None
) -> dict[str, AnalysisResult]:
    return {key: evaluate_stock(snapshot, financials, key, today=today) for key in HORIZONS}
