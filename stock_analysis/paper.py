from __future__ import annotations

from datetime import date, datetime
import re

from .db import SQLiteStore
from .models import PaperOrder, PortfolioSnapshot, Position


class PaperTradingService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        commission_rate: float = 0.0003,
        minimum_commission: float = 5.0,
        stamp_tax_rate: float = 0.001,
        slippage_rate: float = 0.001,
        lot_size: int = 100,
    ) -> None:
        self.store = store
        self.commission_rate = commission_rate
        self.minimum_commission = minimum_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.slippage_rate = slippage_rate
        self.lot_size = lot_size

    def ensure_account(self, account_id: str, initial_cash: float = 1_000_000.0) -> None:
        self.store.ensure_account(account_id, initial_cash)

    def _fee(self, side: str, gross: float) -> float:
        commission = max(self.minimum_commission, gross * self.commission_rate)
        stamp_tax = gross * self.stamp_tax_rate if side == "卖出" else 0.0
        return commission + stamp_tax

    def create_paper_order(
        self,
        account_id: str,
        symbol: str,
        side: str,
        shares: int,
        execution_price: float,
        trade_date: date | None = None,
    ) -> PaperOrder:
        if side not in {"买入", "卖出"}:
            raise ValueError("交易方向必须是买入或卖出")
        if not re.fullmatch(r"[A-Z]+\.\d{6}", symbol or ""):
            raise ValueError("股票标识格式无效")
        if shares <= 0 or shares % self.lot_size != 0:
            raise ValueError(f"交易数量必须是 {self.lot_size} 股的整数倍")
        if execution_price <= 0:
            raise ValueError("成交价必须大于0")
        self.ensure_account(account_id)
        trade_date = trade_date or datetime.now().date()
        effective_price = execution_price * (1 + self.slippage_rate if side == "买入" else 1 - self.slippage_rate)
        gross = effective_price * shares
        fee = self._fee(side, gross)
        cash = self.store.get_cash(account_id)
        if side == "买入":
            cash_change = -(gross + fee)
            if cash + cash_change < -1e-8:
                raise ValueError(f"可用资金不足，需要 {gross + fee:,.2f} 元")
        else:
            cash_change = gross - fee
            held = self._positions(account_id).get(symbol, {}).get("quantity", 0)
            if shares > held:
                raise ValueError(f"持仓不足，当前仅有 {held} 股")
        order = PaperOrder(
            id=None,
            account_id=account_id,
            symbol=symbol,
            side=side,
            shares=shares,
            price=effective_price,
            fee=fee,
            traded_at=trade_date.isoformat(),
        )
        order.id = self.store.record_order_and_update_cash(order, cash + cash_change)
        return order

    def _positions(self, account_id: str) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for order in self.store.get_orders(account_id):
            item = result.setdefault(order.symbol, {"quantity": 0, "average_cost": 0.0, "realized_pnl": 0.0})
            if order.side == "买入":
                old_quantity = item["quantity"]
                old_cost = item["average_cost"]
                new_quantity = old_quantity + order.shares
                item["average_cost"] = (
                    (old_quantity * old_cost) + (order.shares * order.price) + order.fee
                ) / new_quantity
                item["quantity"] = new_quantity
            else:
                sell_quantity = min(order.shares, item["quantity"])
                item["realized_pnl"] += (order.price * sell_quantity - order.fee) - (
                    item["average_cost"] * sell_quantity
                )
                item["quantity"] -= sell_quantity
        return result

    def get_portfolio(self, account_id: str, quotes: dict[str, float] | None = None) -> PortfolioSnapshot:
        self.ensure_account(account_id)
        quotes = quotes or {}
        position_data = self._positions(account_id)
        positions = []
        realized = 0.0
        for symbol, item in position_data.items():
            quantity = int(item["quantity"])
            realized += item["realized_pnl"]
            if quantity <= 0:
                continue
            current_price = float(quotes.get(symbol, item["average_cost"]))
            market_value = current_price * quantity
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_cost=round(item["average_cost"], 4),
                    current_price=current_price,
                    market_value=market_value,
                    unrealized_pnl=(current_price - item["average_cost"]) * quantity,
                )
            )
        cash = self.store.get_cash(account_id)
        market_value = sum(item.market_value for item in positions)
        return PortfolioSnapshot(account_id, cash, positions, market_value, cash + market_value, realized)

    def list_orders(self, account_id: str) -> list[PaperOrder]:
        return self.store.get_all_orders(account_id)


def create_paper_order(
    account_id: str,
    symbol: str,
    side: str,
    shares: int,
    *,
    execution_price: float,
    store: SQLiteStore,
    trade_date: date | None = None,
) -> PaperOrder:
    return PaperTradingService(store).create_paper_order(
        account_id, symbol, side, shares, execution_price, trade_date
    )


def get_portfolio(
    account_id: str, *, store: SQLiteStore, quotes: dict[str, float] | None = None
) -> PortfolioSnapshot:
    return PaperTradingService(store).get_portfolio(account_id, quotes)
