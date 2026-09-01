from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class PositionSizing:
    valid: bool
    risk_budget: float
    price_risk_per_share: float
    shares_by_risk: int
    shares_by_position: int
    suggested_shares: int
    planned_amount: float
    estimated_max_loss: float
    risk_reward: float | None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _lots(shares: float, lot_size: int) -> int:
    return max(0, int(math.floor(shares / lot_size)) * lot_size)


def _fee(
    side: str,
    gross: float,
    *,
    commission_rate: float,
    minimum_commission: float,
    stamp_tax_rate: float,
) -> float:
    if gross <= 0:
        return 0.0
    commission = max(minimum_commission, gross * commission_rate)
    stamp_tax = gross * stamp_tax_rate if side == "卖出" else 0.0
    return commission + stamp_tax


def calculate_position_size(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    total_capital: float,
    risk_pct: float,
    max_position_pct: float,
    *,
    lot_size: int = 100,
    commission_rate: float = 0.0003,
    minimum_commission: float = 5.0,
    stamp_tax_rate: float = 0.001,
    slippage_rate: float = 0.001,
) -> PositionSizing:
    """Calculate a conservative A-share position from a loss budget."""
    warnings: list[str] = []
    values = (entry_price, stop_loss, take_profit, total_capital, risk_pct, max_position_pct)
    if any(not math.isfinite(float(value)) for value in values):
        return PositionSizing(False, 0, 0, 0, 0, 0, 0, 0, None, ("输入中存在无效数字",))
    if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
        warnings.append("买入价、止损价和止盈价必须大于0")
    if stop_loss >= entry_price:
        warnings.append("止损价应低于计划买入价，否则无法计算下行风险")
    if take_profit <= entry_price:
        warnings.append("止盈价应高于计划买入价，否则盈亏比没有正向空间")
    if total_capital <= 0:
        warnings.append("总资金必须大于0")
    if not 0 < risk_pct <= 1:
        warnings.append("单笔风险比例应在0%到100%之间")
    if not 0 < max_position_pct <= 1:
        warnings.append("仓位上限应在0%到100%之间")
    if lot_size <= 0:
        warnings.append("最小交易单位必须大于0")
    if warnings:
        return PositionSizing(False, 0, 0, 0, 0, 0, 0, 0, None, tuple(warnings))

    risk_budget = total_capital * risk_pct
    price_risk_per_share = entry_price - stop_loss
    shares_by_risk = _lots(risk_budget / price_risk_per_share, lot_size)
    shares_by_position = _lots(
        total_capital * max_position_pct / (entry_price * (1 + slippage_rate)), lot_size
    )
    suggested_shares = min(shares_by_risk, shares_by_position)
    if suggested_shares == 0:
        warnings.append("按照当前风险预算和仓位上限，不足以买入一个最小交易单位")

    entry_execution = entry_price * (1 + slippage_rate)
    stop_execution = stop_loss * (1 - slippage_rate)
    target_execution = take_profit * (1 - slippage_rate)
    planned_gross = entry_execution * suggested_shares
    stop_gross = stop_execution * suggested_shares
    target_gross = target_execution * suggested_shares
    buy_fee = _fee("买入", planned_gross, commission_rate=commission_rate,
                    minimum_commission=minimum_commission, stamp_tax_rate=stamp_tax_rate)
    stop_fee = _fee("卖出", stop_gross, commission_rate=commission_rate,
                    minimum_commission=minimum_commission, stamp_tax_rate=stamp_tax_rate)
    target_fee = _fee("卖出", target_gross, commission_rate=commission_rate,
                      minimum_commission=minimum_commission, stamp_tax_rate=stamp_tax_rate)
    estimated_max_loss = max(0.0, planned_gross + buy_fee - stop_gross + stop_fee)
    estimated_reward = target_gross - target_fee - planned_gross - buy_fee
    risk_reward = estimated_reward / estimated_max_loss if estimated_max_loss > 0 else None

    return PositionSizing(
        True,
        round(risk_budget, 2),
        round(price_risk_per_share, 4),
        shares_by_risk,
        shares_by_position,
        suggested_shares,
        round(planned_gross + buy_fee, 2),
        round(estimated_max_loss, 2),
        None if risk_reward is None else round(risk_reward, 2),
        tuple(warnings),
    )
