from __future__ import annotations

import json
from typing import Any

from .models import PaperOrder, Security, TradePlan, TradeReview


def _response_data(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("data", response)
    return getattr(response, "data", response)


def _rows(response: Any) -> list[dict[str, Any]]:
    data = _response_data(response)
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return [dict(item) for item in data if isinstance(item, dict)]


def _rpc_id(response: Any) -> int:
    data = _response_data(response)
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        data = data.get("id") or data.get("order_id")
    if data is None:
        raise RuntimeError("云端订单已提交，但没有返回订单编号。")
    return int(data)


class SupabaseStore:
    """User-data persistence backed by Supabase PostgREST and RLS."""

    def __init__(self, client: Any, user_id: str) -> None:
        if not user_id:
            raise ValueError("Supabase 用户身份不能为空")
        self.client = client
        self.user_id = user_id

    def _table(self, name: str):
        return self.client.table(name)

    def save_watchlist_item(self, account_id: str, security: Security) -> None:
        self._table("user_watchlist").upsert(
            {
                "user_id": self.user_id,
                "symbol": security.symbol,
                "code": security.code,
                "name": security.name,
                "exchange": security.exchange,
                "market_status": security.market_status,
            },
            on_conflict="user_id,symbol",
        ).execute()

    def delete_watchlist_item(self, account_id: str, symbol: str) -> None:
        self._table("user_watchlist").delete().eq("user_id", self.user_id).eq(
            "symbol", symbol
        ).execute()

    def load_watchlist(self, account_id: str) -> list[Security]:
        response = (
            self._table("user_watchlist")
            .select("code,name,exchange,market_status")
            .eq("user_id", self.user_id)
            .execute()
        )
        return [Security(**row) for row in _rows(response)]

    def save_recent_query(
        self, account_id: str, security: Security, as_of: Any = None
    ) -> None:
        date_value = None if as_of in (None, "", "—") else str(as_of)
        self._table("recent_queries").upsert(
            {
                "user_id": self.user_id,
                "symbol": security.symbol,
                "code": security.code,
                "name": security.name,
                "exchange": security.exchange,
                "market_status": security.market_status,
                "as_of": date_value,
            },
            on_conflict="user_id,symbol",
        ).execute()

    def load_recent_queries(self, account_id: str, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        response = (
            self._table("recent_queries")
            .select("symbol,code,name,exchange,market_status,as_of")
            .eq("user_id", self.user_id)
            .order("last_queried_at", desc=True)
            .limit(safe_limit)
            .execute()
        )
        return _rows(response)

    def save_analysis_snapshot(
        self, account_id: str, symbol: str, price: Any, score: Any, as_of: Any = None
    ) -> None:
        def optional_float(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if number == number else None

        date_value = None if as_of in (None, "", "—") else str(as_of)
        self._table("analysis_snapshots").upsert(
            {
                "user_id": self.user_id,
                "symbol": symbol,
                "price": optional_float(price),
                "score": optional_float(score),
                "as_of": date_value,
            },
            on_conflict="user_id,symbol",
        ).execute()

    def load_analysis_snapshots(
        self, account_id: str, limit: int = 30
    ) -> dict[str, dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        response = (
            self._table("analysis_snapshots")
            .select("symbol,price,score,as_of")
            .eq("user_id", self.user_id)
            .order("updated_at", desc=True)
            .limit(safe_limit)
            .execute()
        )
        rows = _rows(response)
        return {row["symbol"]: row for row in reversed(rows) if row.get("symbol")}

    def save_alert_settings(
        self,
        account_id: str,
        enabled: bool,
        price_threshold: float,
        score_threshold: float,
    ) -> None:
        self._table("user_alert_settings").upsert(
            {
                "user_id": self.user_id,
                "enabled": bool(enabled),
                "price_threshold": float(price_threshold),
                "score_threshold": float(score_threshold),
            },
            on_conflict="user_id",
        ).execute()

    def load_alert_settings(self, account_id: str) -> dict[str, Any] | None:
        response = (
            self._table("user_alert_settings")
            .select("enabled,price_threshold,score_threshold")
            .eq("user_id", self.user_id)
            .limit(1)
            .execute()
        )
        rows = _rows(response)
        return rows[0] if rows else None

    def ensure_account(self, account_id: str, initial_cash: float = 1_000_000.0) -> None:
        if initial_cash < 0:
            raise ValueError("初始资金不能为负数")
        self._table("paper_accounts").upsert(
            {"user_id": self.user_id, "cash": float(initial_cash)},
            on_conflict="user_id",
            ignore_duplicates=True,
        ).execute()

    def get_cash(self, account_id: str) -> float:
        response = (
            self._table("paper_accounts")
            .select("cash")
            .eq("user_id", self.user_id)
            .limit(1)
            .execute()
        )
        rows = _rows(response)
        if not rows:
            raise KeyError(f"云端模拟账户不存在: {account_id}")
        return float(rows[0]["cash"])

    def update_cash(self, account_id: str, cash: float) -> None:
        response = (
            self._table("paper_accounts")
            .update({"cash": float(cash)})
            .eq("user_id", self.user_id)
            .select("user_id")
            .execute()
        )
        if not _rows(response):
            raise KeyError(f"云端模拟账户不存在: {account_id}")

    def record_order_and_update_cash(self, order: PaperOrder, new_cash: float) -> int:
        """Use a server-side transaction; never split cash and order writes."""
        del new_cash
        gross = float(order.price) * int(order.shares)
        cash_delta = -(gross + float(order.fee)) if order.side == "买入" else gross - float(order.fee)
        response = self.client.rpc(
            "record_paper_order",
            {
                "p_symbol": order.symbol,
                "p_side": order.side,
                "p_shares": int(order.shares),
                "p_price": float(order.price),
                "p_fee": float(order.fee),
                "p_traded_at": order.traded_at,
                "p_status": order.status,
                "p_message": order.message,
                "p_cash_delta": cash_delta,
            },
        ).execute()
        return _rpc_id(response)

    def insert_order(self, order: PaperOrder) -> int:
        response = self._table("paper_orders").insert(
            {
                "user_id": self.user_id,
                "symbol": order.symbol,
                "side": order.side,
                "shares": int(order.shares),
                "price": float(order.price),
                "fee": float(order.fee),
                "traded_at": order.traded_at,
                "status": order.status,
                "message": order.message,
            }
        ).select("id").execute()
        rows = _rows(response)
        if not rows or rows[0].get("id") is None:
            raise RuntimeError("云端订单已写入，但没有返回订单编号。")
        return int(rows[0]["id"])

    @staticmethod
    def _paper_order(row: dict[str, Any], user_id: str) -> PaperOrder:
        payload = dict(row)
        payload.pop("user_id", None)
        payload["account_id"] = user_id
        return PaperOrder(**payload)

    def get_orders(self, account_id: str, status: str = "filled") -> list[PaperOrder]:
        response = (
            self._table("paper_orders")
            .select("id,symbol,side,shares,price,fee,traded_at,status,message")
            .eq("user_id", self.user_id)
            .eq("status", status)
            .order("traded_at")
            .order("id")
            .execute()
        )
        return [self._paper_order(row, self.user_id) for row in _rows(response)]

    def get_all_orders(self, account_id: str) -> list[PaperOrder]:
        response = (
            self._table("paper_orders")
            .select("id,symbol,side,shares,price,fee,traded_at,status,message")
            .eq("user_id", self.user_id)
            .order("traded_at", desc=True)
            .order("id", desc=True)
            .execute()
        )
        return [self._paper_order(row, self.user_id) for row in _rows(response)]

    def save_trade_plan(self, plan: TradePlan) -> int:
        response = self._table("trade_plans").insert(
            {
                "user_id": self.user_id,
                "symbol": plan.symbol,
                "direction": plan.direction,
                "setup": plan.setup,
                "horizon": plan.horizon,
                "entry_price": plan.entry_price,
                "stop_loss": plan.stop_loss,
                "take_profit": plan.take_profit,
                "total_capital": plan.total_capital,
                "risk_pct": plan.risk_pct,
                "max_position_pct": plan.max_position_pct,
                "planned_shares": plan.planned_shares,
                "planned_amount": plan.planned_amount,
                "risk_budget": plan.risk_budget,
                "estimated_max_loss": plan.estimated_max_loss,
                "risk_reward": plan.risk_reward,
                "thesis": plan.thesis,
                "invalidation": plan.invalidation,
                "status": plan.status,
            }
        ).select("id").execute()
        rows = _rows(response)
        if not rows or rows[0].get("id") is None:
            raise RuntimeError("交易计划已写入，但没有返回计划编号。")
        return int(rows[0]["id"])

    def load_trade_plans(
        self, account_id: str, symbol: str | None = None, limit: int = 20
    ) -> list[TradePlan]:
        query = (
            self._table("trade_plans")
            .select("id,symbol,direction,setup,horizon,entry_price,stop_loss,take_profit,"
                    "total_capital,risk_pct,max_position_pct,planned_shares,planned_amount,"
                    "risk_budget,estimated_max_loss,risk_reward,thesis,invalidation,status,created_at")
            .eq("user_id", self.user_id)
            .order("created_at", desc=True)
            .limit(max(1, min(int(limit), 100)))
        )
        if symbol:
            query = query.eq("symbol", symbol)
        rows = _rows(query.execute())
        return [TradePlan(account_id=self.user_id, **row) for row in rows]

    def save_trade_review(self, review: TradeReview) -> int:
        response = self._table("trade_reviews").insert(
            {
                "user_id": self.user_id,
                "symbol": review.symbol,
                "plan_id": review.plan_id,
                "review_date": review.review_date,
                "outcome": review.outcome,
                "execution_adherence": int(review.execution_adherence),
                "mistake_tags": list(review.mistake_tags),
                "notes": review.notes,
            }
        ).select("id").execute()
        rows = _rows(response)
        if not rows or rows[0].get("id") is None:
            raise RuntimeError("交易复盘已写入，但没有返回复盘编号。")
        return int(rows[0]["id"])

    def load_trade_reviews(
        self, account_id: str, symbol: str | None = None, limit: int = 50
    ) -> list[TradeReview]:
        query = (
            self._table("trade_reviews")
            .select("id,symbol,plan_id,review_date,outcome,execution_adherence,mistake_tags,notes")
            .eq("user_id", self.user_id)
            .order("review_date", desc=True)
            .limit(max(1, min(int(limit), 100)))
        )
        if symbol:
            query = query.eq("symbol", symbol)
        reviews: list[TradeReview] = []
        for row in _rows(query.execute()):
            tags = row.pop("mistake_tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (TypeError, ValueError, json.JSONDecodeError):
                    tags = []
            reviews.append(
                TradeReview(account_id=self.user_id, mistake_tags=tuple(tags or []), **row)
            )
        return reviews


class HybridStore:
    """Route public-data cache calls locally and user-data calls remotely."""

    def __init__(self, cache_store: Any, user_store: Any) -> None:
        self.cache_store = cache_store
        self.user_store = user_store

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.user_store, name):
            return getattr(self.user_store, name)
        return getattr(self.cache_store, name)
