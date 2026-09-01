from datetime import date

from stock_analysis.db import SQLiteStore
from stock_analysis.models import TradePlan, TradeReview


def test_trade_plan_and_review_survive_store_reinitialization(tmp_path):
    path = tmp_path / "trade_plan.db"
    plan = TradePlan(
        id=None,
        account_id="local-default",
        symbol="SSE.600519",
        direction="买入",
        setup="趋势突破",
        horizon="5–20个交易日",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=112.0,
        total_capital=100_000.0,
        risk_pct=0.01,
        max_position_pct=0.25,
        planned_shares=100,
        planned_amount=10_010.0,
        risk_budget=1_000.0,
        estimated_max_loss=520.0,
        risk_reward=2.1,
        thesis="趋势和量价同时确认",
        invalidation="跌破止损价",
    )
    store = SQLiteStore(path)
    plan_id = store.save_trade_plan(plan)
    store.save_trade_review(
        TradeReview(
            id=None,
            account_id="local-default",
            symbol="SSE.600519",
            plan_id=plan_id,
            review_date=date(2026, 8, 31).isoformat(),
            outcome="执行中",
            execution_adherence=80,
            mistake_tags=("追涨",),
            notes="等待下一交易日确认",
        )
    )

    second = SQLiteStore(path)
    plans = second.load_trade_plans("local-default", "SSE.600519")
    reviews = second.load_trade_reviews("local-default", "SSE.600519")

    assert plans[0].id == plan_id
    assert plans[0].planned_shares == 100
    assert reviews[0].mistake_tags == ("追涨",)
    assert reviews[0].execution_adherence == 80
