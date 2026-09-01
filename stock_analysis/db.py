from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import PaperOrder, Security, TradePlan, TradeReview


class SQLiteStore:
    """Small SQLite persistence layer for cached data and paper trading."""

    def __init__(self, path: str | Path = "data/stock_analysis.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS price_bars (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    PRIMARY KEY (symbol, date, source)
                );

                CREATE INDEX IF NOT EXISTS idx_price_bars_symbol_date
                    ON price_bars(symbol, date);

                CREATE TABLE IF NOT EXISTS money_flow_bars (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    main_net_inflow REAL NOT NULL,
                    main_net_ratio REAL,
                    close REAL,
                    price_change REAL,
                    super_large_net_inflow REAL,
                    large_net_inflow REAL,
                    medium_net_inflow REAL,
                    small_net_inflow REAL,
                    PRIMARY KEY (symbol, date, source)
                );

                CREATE INDEX IF NOT EXISTS idx_money_flow_symbol_date
                    ON money_flow_bars(symbol, date);

                CREATE TABLE IF NOT EXISTS security_cache (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    market_status TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS financial_cache (
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    as_of TEXT,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, source)
                );

                CREATE TABLE IF NOT EXISTS analysis_cache (
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    as_of TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, horizon, as_of)
                );

                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    cash REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL,
                    traded_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'filled',
                    message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_watchlist (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    market_status TEXT NOT NULL DEFAULT 'normal',
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS recent_queries (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    market_status TEXT NOT NULL DEFAULT 'normal',
                    as_of TEXT,
                    last_queried_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL,
                    score REAL,
                    as_of TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS user_alert_settings (
                    account_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    price_threshold REAL NOT NULL DEFAULT 3.0,
                    score_threshold REAL NOT NULL DEFAULT 5.0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trade_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    total_capital REAL NOT NULL,
                    risk_pct REAL NOT NULL,
                    max_position_pct REAL NOT NULL,
                    planned_shares INTEGER NOT NULL,
                    planned_amount REAL NOT NULL,
                    risk_budget REAL NOT NULL,
                    estimated_max_loss REAL NOT NULL,
                    risk_reward REAL,
                    thesis TEXT NOT NULL DEFAULT '',
                    invalidation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'planned',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_trade_plans_account_symbol
                    ON trade_plans(account_id, symbol, created_at);

                CREATE TABLE IF NOT EXISTS trade_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    plan_id INTEGER,
                    review_date TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    execution_adherence INTEGER NOT NULL,
                    mistake_tags TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_trade_reviews_account_symbol
                    ON trade_reviews(account_id, symbol, review_date);
                """
            )
            self._migrate_source_aware_cache(connection)

    @staticmethod
    def _migrate_source_aware_cache(connection: sqlite3.Connection) -> None:
        price_info = connection.execute("PRAGMA table_info(price_bars)").fetchall()
        price_columns = {row[1] for row in price_info}
        price_pk = [row[1] for row in price_info if row[5]]
        if "source" not in price_columns or price_pk != ["symbol", "date", "source"]:
            connection.execute("DROP TABLE IF EXISTS price_bars_new")
            connection.execute(
                """
                CREATE TABLE price_bars_new (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    PRIMARY KEY (symbol, date, source)
                )
                """
            )
            if "source" in price_columns:
                connection.execute(
                    """
                    INSERT INTO price_bars_new
                        (symbol, date, source, open, high, low, close, volume, amount)
                    SELECT symbol, date, source, open, high, low, close, volume, amount
                    FROM price_bars
                    """
                )
            else:
                connection.execute(
                    """
                    INSERT INTO price_bars_new
                        (symbol, date, source, open, high, low, close, volume, amount)
                    SELECT symbol, date, 'unknown', open, high, low, close, volume, amount
                    FROM price_bars
                    """
                )
            connection.execute("DROP TABLE price_bars")
            connection.execute("ALTER TABLE price_bars_new RENAME TO price_bars")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_bars_symbol_date "
                "ON price_bars(symbol, date)"
            )

        financial_info = connection.execute("PRAGMA table_info(financial_cache)").fetchall()
        financial_columns = {row[1] for row in financial_info}
        financial_pk = [row[1] for row in financial_info if row[5]]
        if "source" not in financial_columns or financial_pk != ["symbol", "source"]:
            connection.execute("DROP TABLE IF EXISTS financial_cache_new")
            connection.execute(
                """
                CREATE TABLE financial_cache_new (
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    as_of TEXT,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, source)
                )
                """
            )
            if "source" in financial_columns:
                connection.execute(
                    """
                    INSERT INTO financial_cache_new
                        (symbol, source, as_of, payload, fetched_at)
                    SELECT symbol, source, as_of, payload, fetched_at
                    FROM financial_cache
                    """
                )
            else:
                connection.execute(
                    """
                    INSERT INTO financial_cache_new
                        (symbol, source, as_of, payload, fetched_at)
                    SELECT symbol, 'unknown', as_of, payload, fetched_at
                    FROM financial_cache
                    """
                )
            connection.execute("DROP TABLE financial_cache")
            connection.execute("ALTER TABLE financial_cache_new RENAME TO financial_cache")

    def migrate_account(self, old_account_id: str, new_account_id: str) -> bool:
        """Copy an old session account into a stable account without deleting the source."""
        if not old_account_id or not new_account_id or old_account_id == new_account_id:
            return False
        with self._connect() as connection:
            old_account = connection.execute(
                "SELECT cash FROM paper_accounts WHERE account_id = ?",
                (old_account_id,),
            ).fetchone()
            new_account = connection.execute(
                """
                SELECT cash,
                       (SELECT COUNT(*)
                        FROM paper_orders
                        WHERE paper_orders.account_id = paper_accounts.account_id) AS order_count
                FROM paper_accounts
                WHERE account_id = ?
                """,
                (new_account_id,),
            ).fetchone()
            if old_account is None:
                return False
            if new_account is not None and (
                int(new_account["order_count"]) > 0
                or abs(float(new_account["cash"]) - 1_000_000.0) > 1e-8
            ):
                return False

            if new_account is None:
                connection.execute(
                    "INSERT INTO paper_accounts(account_id, cash) VALUES (?, ?)",
                    (new_account_id, float(old_account["cash"])),
                )
            else:
                connection.execute(
                    "UPDATE paper_accounts SET cash = ? WHERE account_id = ?",
                    (float(old_account["cash"]), new_account_id),
                )
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, symbol, side, shares, price, fee,
                    traded_at, status, message
                )
                SELECT ?, symbol, side, shares, price, fee,
                       traded_at, status, message
                FROM paper_orders
                WHERE account_id = ?
                """,
                (new_account_id, old_account_id),
            )
            return True

    def migrate_latest_session_account(self, new_account_id: str) -> bool:
        """Migrate the most recently active legacy random account for local use."""
        with self._connect() as connection:
            new_account = connection.execute(
                """
                SELECT cash,
                       (SELECT COUNT(*)
                        FROM paper_orders
                        WHERE paper_orders.account_id = paper_accounts.account_id) AS order_count
                FROM paper_accounts
                WHERE account_id = ?
                """,
                (new_account_id,),
            ).fetchone()
            if new_account is not None and (
                int(new_account["order_count"]) > 0
                or abs(float(new_account["cash"]) - 1_000_000.0) > 1e-8
            ):
                return False
            legacy = connection.execute(
                """
                SELECT account_id
                FROM paper_accounts
                WHERE account_id LIKE 'session-%'
                ORDER BY
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM paper_orders
                        WHERE paper_orders.account_id = paper_accounts.account_id
                    ) THEN 0 ELSE 1 END,
                    COALESCE(
                        (SELECT MAX(id)
                         FROM paper_orders
                         WHERE paper_orders.account_id = paper_accounts.account_id),
                        -1
                    ) DESC,
                    created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if legacy is None:
            return False
        return self.migrate_account(legacy["account_id"], new_account_id)

    @staticmethod
    def _date_string(value: date | str | None) -> str | None:
        if value is None or str(value).strip() in {"", "—", "None"}:
            return None
        try:
            return pd.Timestamp(value).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    def save_watchlist_item(self, account_id: str, security: Security) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_watchlist(
                    account_id, symbol, code, name, exchange, market_status, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    code=excluded.code, name=excluded.name,
                    exchange=excluded.exchange, market_status=excluded.market_status
                """,
                (
                    account_id,
                    security.symbol,
                    security.code,
                    security.name,
                    security.exchange,
                    security.market_status,
                ),
            )

    def delete_watchlist_item(self, account_id: str, symbol: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM user_watchlist WHERE account_id = ? AND symbol = ?",
                (account_id, symbol),
            )

    def load_watchlist(self, account_id: str) -> list[Security]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT code, name, exchange, market_status
                FROM user_watchlist
                WHERE account_id = ?
                ORDER BY added_at DESC, symbol
                """,
                (account_id,),
            ).fetchall()
        return [Security(**dict(row)) for row in rows]

    def save_recent_query(
        self, account_id: str, security: Security, as_of: date | str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    account_id, symbol, code, name, exchange, market_status,
                    as_of, last_queried_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    code=excluded.code, name=excluded.name,
                    exchange=excluded.exchange, market_status=excluded.market_status,
                    as_of=excluded.as_of, last_queried_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    security.symbol,
                    security.code,
                    security.name,
                    security.exchange,
                    security.market_status,
                    self._date_string(as_of),
                ),
            )

    def load_recent_queries(self, account_id: str, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, code, name, exchange, market_status, as_of
                FROM recent_queries
                WHERE account_id = ?
                ORDER BY last_queried_at DESC, symbol
                LIMIT ?
                """,
                (account_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(number) else number

    def save_analysis_snapshot(
        self,
        account_id: str,
        symbol: str,
        price: Any,
        score: Any,
        as_of: date | str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_snapshots(
                    account_id, symbol, price, score, as_of, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    price=excluded.price, score=excluded.score,
                    as_of=excluded.as_of, updated_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    symbol,
                    self._optional_float(price),
                    self._optional_float(score),
                    self._date_string(as_of),
                ),
            )

    def load_analysis_snapshots(
        self, account_id: str, limit: int = 30
    ) -> dict[str, dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, price, score, as_of
                FROM analysis_snapshots
                WHERE account_id = ?
                ORDER BY updated_at DESC, symbol
                LIMIT ?
                """,
                (account_id, safe_limit),
            ).fetchall()
        return {row["symbol"]: dict(row) for row in reversed(rows)}

    def save_alert_settings(
        self,
        account_id: str,
        enabled: bool,
        price_threshold: float,
        score_threshold: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_alert_settings(
                    account_id, enabled, price_threshold, score_threshold, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    price_threshold=excluded.price_threshold,
                    score_threshold=excluded.score_threshold,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    1 if enabled else 0,
                    float(price_threshold),
                    float(score_threshold),
                ),
            )

    def load_alert_settings(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT enabled, price_threshold, score_threshold
                FROM user_alert_settings
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "enabled": bool(row["enabled"]),
            "price_threshold": float(row["price_threshold"]),
            "score_threshold": float(row["score_threshold"]),
        }

    def upsert_securities(self, securities: Iterable[Security]) -> None:
        rows = [
            (item.code, item.name, item.exchange, item.market_status)
            for item in securities
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO security_cache(code, name, exchange, market_status, fetched_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name, exchange=excluded.exchange,
                    market_status=excluded.market_status, fetched_at=CURRENT_TIMESTAMP
                """,
                rows,
            )

    def find_securities(
        self, *, code: str = "", name_fragment: str = "", max_age_days: int = 30
    ) -> list[Security]:
        if not code and not name_fragment:
            return []
        if code:
            query = """
                SELECT code, name, exchange, market_status
                FROM security_cache
                WHERE code = ? AND fetched_at >= datetime('now', ?)
                ORDER BY code
            """
            params = (code, f"-{max_age_days} days")
        else:
            query = """
                SELECT code, name, exchange, market_status
                FROM security_cache
                WHERE instr(lower(name), lower(?)) > 0
                  AND fetched_at >= datetime('now', ?)
                ORDER BY code
            """
            params = (name_fragment, f"-{max_age_days} days")
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [Security(**dict(row)) for row in rows]

    def save_financials(
        self,
        symbol: str,
        financials: dict[str, Any],
        as_of: date | str | None = None,
        source: str = "unknown",
    ) -> None:
        payload = json.dumps(financials, ensure_ascii=False, default=str)
        as_of_value = None if as_of is None else pd.Timestamp(as_of).strftime("%Y-%m-%d")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO financial_cache(symbol, source, as_of, payload, fetched_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol, source) DO UPDATE SET
                    source=excluded.source, as_of=excluded.as_of,
                    payload=excluded.payload, fetched_at=CURRENT_TIMESTAMP
                """,
                (symbol, source, as_of_value, payload),
            )

    def load_financials(
        self,
        symbol: str,
        max_age_days: int = 30,
        sources: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        source_clause = ""
        params: list[Any] = [symbol, f"-{max_age_days} days"]
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            source_clause = f" AND source IN ({placeholders})"
            params.extend(sources)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT payload FROM financial_cache
                WHERE symbol = ? AND fetched_at >= datetime('now', ?)
                {source_clause}
                """,
                params,
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def save_analysis_results(
        self, symbol: str, as_of: date | str | None, payloads: dict[str, Any]
    ) -> None:
        as_of_value = None if as_of is None else pd.Timestamp(as_of).strftime("%Y-%m-%d")
        rows = [
            (
                symbol,
                horizon,
                as_of_value,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
            for horizon, payload in payloads.items()
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO analysis_cache(symbol, horizon, as_of, payload, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol, horizon, as_of) DO UPDATE SET
                    payload=excluded.payload, created_at=CURRENT_TIMESTAMP
                """,
                rows,
            )

    def upsert_price_bars(
        self, symbol: str, bars: pd.DataFrame, source: str = "unknown"
    ) -> None:
        if bars.empty:
            return
        required = {"date", "open", "high", "low", "close"}
        missing = required.difference(bars.columns)
        if missing:
            raise ValueError(f"行情数据缺少字段: {', '.join(sorted(missing))}")
        rows = []
        for row in bars.itertuples(index=False):
            record = row._asdict()
            timestamp = pd.Timestamp(record["date"]).strftime("%Y-%m-%d")
            rows.append(
                (
                    symbol,
                    timestamp,
                    source,
                    float(record["open"]),
                    float(record["high"]),
                    float(record["low"]),
                    float(record["close"]),
                    float(record.get("volume", 0) or 0),
                    float(record.get("amount", 0) or 0),
                )
            )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO price_bars(symbol, date, source, open, high, low, close, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date, source) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, amount=excluded.amount
                """,
                rows,
            )

    def load_price_bars(
        self,
        symbol: str,
        start_date: date | str,
        end_date: date | str,
        sources: tuple[str, ...] | None = None,
    ) -> pd.DataFrame:
        start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        source_clause = ""
        params: list[Any] = [symbol, start, end]
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            source_clause = f" AND source IN ({placeholders})"
            params.extend(sources)
        with self._connect() as connection:
            return pd.read_sql_query(
                f"""
                SELECT date, open, high, low, close, volume, amount
                FROM price_bars
                WHERE symbol = ? AND date BETWEEN ? AND ?
                {source_clause}
                ORDER BY date
                """,
                connection,
                params=params,
                parse_dates=["date"],
            )

    def upsert_money_flow(
        self, symbol: str, frame: pd.DataFrame, source: str = "unknown"
    ) -> None:
        if frame.empty:
            return
        required = {"date", "main_net_inflow"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"主力资金数据缺少字段: {', '.join(sorted(missing))}")

        def optional_float(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return None if pd.isna(number) else number

        rows = []
        for row in frame.itertuples(index=False):
            record = row._asdict()
            rows.append(
                (
                    symbol,
                    pd.Timestamp(record["date"]).strftime("%Y-%m-%d"),
                    source,
                    optional_float(record.get("main_net_inflow")),
                    optional_float(record.get("main_net_ratio")),
                    optional_float(record.get("close")),
                    optional_float(record.get("change")),
                    optional_float(record.get("super_large_net_inflow")),
                    optional_float(record.get("large_net_inflow")),
                    optional_float(record.get("medium_net_inflow")),
                    optional_float(record.get("small_net_inflow")),
                )
            )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO money_flow_bars(
                    symbol, date, source, main_net_inflow, main_net_ratio, close,
                    price_change, super_large_net_inflow, large_net_inflow,
                    medium_net_inflow, small_net_inflow
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date, source) DO UPDATE SET
                    main_net_inflow=excluded.main_net_inflow,
                    main_net_ratio=excluded.main_net_ratio, close=excluded.close,
                    price_change=excluded.price_change,
                    super_large_net_inflow=excluded.super_large_net_inflow,
                    large_net_inflow=excluded.large_net_inflow,
                    medium_net_inflow=excluded.medium_net_inflow,
                    small_net_inflow=excluded.small_net_inflow
                """,
                rows,
            )

    def load_money_flow(
        self,
        symbol: str,
        start_date: date | str,
        end_date: date | str,
        sources: tuple[str, ...] | None = None,
    ) -> pd.DataFrame:
        start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        source_clause = ""
        params: list[Any] = [symbol, start, end]
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            source_clause = f" AND source IN ({placeholders})"
            params.extend(sources)
        with self._connect() as connection:
            return pd.read_sql_query(
                f"""
                SELECT date, main_net_inflow, main_net_ratio, close,
                       price_change AS change, super_large_net_inflow,
                       large_net_inflow, medium_net_inflow, small_net_inflow
                FROM money_flow_bars
                WHERE symbol = ? AND date BETWEEN ? AND ?
                {source_clause}
                ORDER BY date
                """,
                connection,
                params=params,
                parse_dates=["date"],
            )

    def ensure_account(self, account_id: str, initial_cash: float) -> None:
        if initial_cash < 0:
            raise ValueError("初始资金不能为负数")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO paper_accounts(account_id, cash) VALUES (?, ?)",
                (account_id, float(initial_cash)),
            )

    def get_cash(self, account_id: str) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cash FROM paper_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"模拟账户不存在: {account_id}")
        return float(row["cash"])

    def update_cash(self, account_id: str, cash: float) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_accounts SET cash = ? WHERE account_id = ?",
                (float(cash), account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"模拟账户不存在: {account_id}")

    def record_order_and_update_cash(self, order: PaperOrder, new_cash: float) -> int:
        """Persist cash and its order together so a failed write cannot split state."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_accounts SET cash = ? WHERE account_id = ?",
                (float(new_cash), order.account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"模拟账户不存在: {order.account_id}")
            cursor = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, symbol, side, shares, price, fee, traded_at, status, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.account_id,
                    order.symbol,
                    order.side,
                    order.shares,
                    order.price,
                    order.fee,
                    order.traded_at,
                    order.status,
                    order.message,
                ),
            )
            return int(cursor.lastrowid)

    def insert_order(self, order: PaperOrder) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, symbol, side, shares, price, fee, traded_at, status, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.account_id,
                    order.symbol,
                    order.side,
                    order.shares,
                    order.price,
                    order.fee,
                    order.traded_at,
                    order.status,
                    order.message,
                ),
            )
            return int(cursor.lastrowid)

    def get_orders(self, account_id: str, status: str = "filled") -> list[PaperOrder]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, account_id, symbol, side, shares, price, fee,
                       traded_at, status, message
                FROM paper_orders
                WHERE account_id = ? AND status = ?
                ORDER BY traded_at, id
                """,
                (account_id, status),
            ).fetchall()
        return [PaperOrder(**dict(row)) for row in rows]

    def get_all_orders(self, account_id: str) -> list[PaperOrder]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, account_id, symbol, side, shares, price, fee,
                       traded_at, status, message
                FROM paper_orders
                WHERE account_id = ?
                ORDER BY traded_at DESC, id DESC
                """,
                (account_id,),
            ).fetchall()
        return [PaperOrder(**dict(row)) for row in rows]

    def save_trade_plan(self, plan: TradePlan) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trade_plans(
                    account_id, symbol, direction, setup, horizon,
                    entry_price, stop_loss, take_profit, total_capital,
                    risk_pct, max_position_pct, planned_shares, planned_amount,
                    risk_budget, estimated_max_loss, risk_reward, thesis,
                    invalidation, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.account_id,
                    plan.symbol,
                    plan.direction,
                    plan.setup,
                    plan.horizon,
                    plan.entry_price,
                    plan.stop_loss,
                    plan.take_profit,
                    plan.total_capital,
                    plan.risk_pct,
                    plan.max_position_pct,
                    plan.planned_shares,
                    plan.planned_amount,
                    plan.risk_budget,
                    plan.estimated_max_loss,
                    plan.risk_reward,
                    plan.thesis,
                    plan.invalidation,
                    plan.status,
                ),
            )
            return int(cursor.lastrowid)

    def load_trade_plans(
        self, account_id: str, symbol: str | None = None, limit: int = 20
    ) -> list[TradePlan]:
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, account_id, symbol, direction, setup, horizon,
                       entry_price, stop_loss, take_profit, total_capital,
                       risk_pct, max_position_pct, planned_shares, planned_amount,
                       risk_budget, estimated_max_loss, risk_reward, thesis,
                       invalidation, status, created_at
                FROM trade_plans
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [TradePlan(**dict(row)) for row in rows]

    def save_trade_review(self, review: TradeReview) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trade_reviews(
                    account_id, symbol, plan_id, review_date, outcome,
                    execution_adherence, mistake_tags, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.account_id,
                    review.symbol,
                    review.plan_id,
                    review.review_date,
                    review.outcome,
                    int(review.execution_adherence),
                    json.dumps(list(review.mistake_tags), ensure_ascii=False),
                    review.notes,
                ),
            )
            return int(cursor.lastrowid)

    def load_trade_reviews(
        self, account_id: str, symbol: str | None = None, limit: int = 50
    ) -> list[TradeReview]:
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, account_id, symbol, plan_id, review_date, outcome,
                       execution_adherence, mistake_tags, notes
                FROM trade_reviews
                WHERE {' AND '.join(clauses)}
                ORDER BY review_date DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        reviews: list[TradeReview] = []
        for row in rows:
            payload = dict(row)
            try:
                tags = tuple(json.loads(payload.pop("mistake_tags") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = tuple()
            reviews.append(TradeReview(**payload, mistake_tags=tags))
        return reviews
