from __future__ import annotations

from dataclasses import dataclass, field

from .models import StrategyEvaluation


BUY_SIGNAL = "买入候选"
HOLD_SIGNAL = "观望/持有"
SELL_SIGNAL = "减仓/卖出倾向"


def _ratio(value: float) -> float:
    """Keep displayed and persisted percentages stable across float arithmetic."""
    return round(max(0.0, min(1.0, float(value))), 6)


@dataclass(frozen=True)
class PositionGuidance:
    """Rule-based position adjustment using the user's current holding ratio."""

    current_ratio: float
    action: str
    target_ratio: float | None
    max_ratio: float | None
    suggested_buy_ratio: float | None
    suggested_sell_ratio_total: float | None
    suggested_sell_ratio_current: float | None
    basis: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def recommend_position_action(
    result: StrategyEvaluation,
    current_ratio: float,
) -> PositionGuidance:
    """Translate a strategy result and current holding ratio into a visible rule.

    Ratios are decimals of total assets. The current-position sell ratio is a
    fraction of the existing position and is shown separately in the UI.
    """

    if not 0 <= current_ratio <= 1:
        raise ValueError("当前持仓比例必须在0%到100%之间")

    if result.data_status != "ok" or result.score is None:
        warnings = tuple(result.warnings) or ("当前数据不足，不能根据持仓比例生成调整建议。",)
        return PositionGuidance(
            current_ratio=current_ratio,
            action="数据不足/不可判断",
            target_ratio=None,
            max_ratio=None,
            suggested_buy_ratio=None,
            suggested_sell_ratio_total=None,
            suggested_sell_ratio_current=None,
            warnings=warnings,
        )

    score = float(result.score)
    basis = [f"当前优化策略信号：{result.signal}，评分 {score:.1f} 分。"]
    warnings = [
        "仓位比例是固定规则的风险控制参考，不是根据个人总资产、成本价或风险承受能力计算的个性化建议。",
        "建议分批调整，不要因为单次信号一次性满仓或清仓。",
    ]

    target_ratio: float | None = None
    max_ratio: float | None = None
    buy_ratio = 0.0
    sell_total = 0.0
    sell_current = 0.0

    if result.signal == BUY_SIGNAL:
        target_ratio = 0.15 if score >= 85 else 0.10
        max_ratio = 0.25
        basis.append(
            f"买入候选仓位规则：目标仓位约{target_ratio:.0%}，最高不超过{max_ratio:.0%}。"
        )
        if current_ratio < target_ratio:
            buy_ratio = _ratio(target_ratio - current_ratio)
            action = "当前仓位低于规则目标，可考虑分批买入/加仓"
            basis.append(
                f"当前仓位{current_ratio:.0%}低于目标{target_ratio:.0%}，按差额计算新增仓位。"
            )
        elif current_ratio > max_ratio:
            sell_total = _ratio(current_ratio - max_ratio)
            sell_current = _ratio(sell_total / current_ratio) if current_ratio else 0.0
            action = "当前仓位超过规则上限，优先考虑减仓"
            basis.append(
                f"当前仓位{current_ratio:.0%}高于上限{max_ratio:.0%}，只建议处理超出部分。"
            )
        else:
            action = "已有仓位在规则控制范围内，暂不主动加仓"
            basis.append(
                f"当前仓位{current_ratio:.0%}已达到目标附近且未超过上限，避免追涨加仓。"
            )
    elif result.signal == HOLD_SIGNAL:
        max_ratio = 0.25
        basis.append("观望/持有规则：不主动新增仓位，单只股票仓位上限按25%控制。")
        if current_ratio > max_ratio:
            sell_total = _ratio(current_ratio - max_ratio)
            sell_current = _ratio(sell_total / current_ratio) if current_ratio else 0.0
            action = "不建议买入，当前仓位超过上限，可考虑减仓至25%以内"
            basis.append(
                f"当前仓位{current_ratio:.0%}超过25%上限，建议优先降低超出部分。"
            )
        elif current_ratio > 0:
            action = "不建议新增仓位，当前持仓可继续观察"
            basis.append("当前仓位未超过上限，基础动作是持有和等待条件确认。")
        else:
            action = "没有持仓，不建议现在买入"
            basis.append("评分或过滤条件未进入买入候选区间，新增仓位为0%。")
    else:
        reduction = 0.50 if score < 35 else 0.25
        max_ratio = 0.0
        target_ratio = _ratio(current_ratio * (1 - reduction))
        sell_current = _ratio(reduction) if current_ratio > 0 else 0.0
        sell_total = _ratio(current_ratio * reduction)
        basis.append(
            f"减仓规则：评分{'低于35分' if score < 35 else '处于35–44.9分'}，"
            f"按当前持仓建议减少{reduction:.0%}。"
        )
        if current_ratio > 0:
            action = f"不建议买入，已有持仓可考虑减仓{reduction:.0%}"
            basis.append(
                f"当前持仓占总资产{current_ratio:.0%}，对应建议卖出约{sell_total:.1%}总资产。"
            )
        else:
            action = "没有持仓，不建议买入"
            basis.append("当前已是减仓/卖出倾向，新增仓位为0%。")

    return PositionGuidance(
        current_ratio=current_ratio,
        action=action,
        target_ratio=target_ratio,
        max_ratio=max_ratio,
        suggested_buy_ratio=buy_ratio,
        suggested_sell_ratio_total=sell_total,
        suggested_sell_ratio_current=sell_current,
        basis=tuple(basis),
        warnings=tuple(warnings),
    )
