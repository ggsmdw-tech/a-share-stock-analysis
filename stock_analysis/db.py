from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import PaperOrder, Security


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
