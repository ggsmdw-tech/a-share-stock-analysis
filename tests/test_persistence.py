from datetime import date

from stock_analysis.db import SQLiteStore
from stock_analysis.models import Security
from stock_analysis.paper import PaperTradingService


def test_user_state_survives_store_reinitialization(tmp_path):
    database_path = tmp_path / "persistent.db"
    security = Security("600519", "贵州茅台", "SSE")

    first_store = SQLiteStore(database_path)
    first_store.save_watchlist_item("local-default", security)
    first_store.save_recent_query("local-default", security, date(2026, 8, 28))
    first_store.save_analysis_snapshot(
        "local-default", security.symbol, 1234.56, 78.5, date(2026, 8, 28)
    )

    second_store = SQLiteStore(database_path)
    watchlist = second_store.load_watchlist("local-default")
    recent = second_store.load_recent_queries("local-default")
    snapshots = second_store.load_analysis_snapshots("local-default")

    assert watchlist == [security]
    assert recent[0]["symbol"] == security.symbol
    assert recent[0]["as_of"] == "2026-08-28"
    assert snapshots[security.symbol]["price"] == 1234.56
    assert snapshots[security.symbol]["score"] == 78.5


def test_account_migration_copies_orders_and_is_idempotent(tmp_path):
    database_path = tmp_path / "migration.db"
    store = SQLiteStore(database_path)
    service = PaperTradingService(
        store,
        commission_rate=0,
        minimum_commission=0,
        stamp_tax_rate=0,
        slippage_rate=0,
    )
    service.ensure_account("session-legacy", 100_000)
    service.create_paper_order(
        "session-legacy", "SSE.600519", "买入", 100, 10.0, date(2026, 8, 28)
    )

    assert store.migrate_account("session-legacy", "local-default") is True
    migrated = PaperTradingService(store).get_portfolio(
        "local-default", {"SSE.600519": 12.0}
    )
    assert migrated.cash == 99_000
    assert migrated.positions[0].quantity == 100
    assert store.migrate_account("session-legacy", "local-default") is False
    assert len(store.get_all_orders("local-default")) == 1


def test_latest_legacy_account_migrates_only_when_local_account_is_missing(tmp_path):
    database_path = tmp_path / "legacy.db"
    store = SQLiteStore(database_path)
    store.ensure_account("session-old", 100_000)

    assert store.migrate_latest_session_account("local-default") is True
    assert store.get_cash("local-default") == 100_000
    assert store.migrate_latest_session_account("local-default") is False
