from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Security:
    code: str
    name: str
    exchange: str
    market_status: str = "正常"

    @property
    def symbol(self) -> str:
        return f"{self.exchange}.{self.code}"


@dataclass
class PriceHistory:
    security: Security
    data: pd.DataFrame
    source: str
    status: str = "ok"
    message: str = ""

    @property
    def as_of(self) -> date | None:
        if self.data.empty or "date" not in self.data.columns:
            return None
        return pd.Timestamp(self.data["date"].max()).date()


@dataclass
class IndicatorSnapshot:
    security: Security
    frame: pd.DataFrame
    latest: dict[str, Any]
    status: str = "ok"
    message: str = ""

    @property
    def as_of(self) -> date | None:
        if not self.latest.get("date"):
            return None
        return pd.Timestamp(self.latest["date"]).date()


@dataclass
class ScoreComponent:
    name: str
    score: float | None
    weight: float
    detail: str
    missing: bool = False


@dataclass
class AnalysisResult:
    security: Security
    horizon: str
    as_of: date | None
    data_status: str
    score: float | None
    signal: str
    confidence: float
    components: list[ScoreComponent] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperOrder:
    id: int | None
    account_id: str
    symbol: str
    side: str
    shares: int
    price: float
    fee: float
    traded_at: str
    status: str = "filled"
    message: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int
    average_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float


@dataclass
class PortfolioSnapshot:
    account_id: str
    cash: float
    positions: list[Position]
    total_market_value: float
    total_equity: float
    realized_pnl: float

