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
class MoneyFlowHistory:
    """Daily provider-estimated main-money flow history."""

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
    strategy_name: str = "基础综合评分"
    entry_conditions: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    risk_controls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StrategyConfig:
    """Transparent defaults for the 5-20 trading-day swing strategy."""

    name: str = "趋势动量波段策略"
    score_threshold: float = 70.0
    rsi_min: float = 45.0
    rsi_max: float = 72.0
    min_volume_ratio: float = 1.0
    max_volatility: float = 0.60
    max_drawdown: float = -0.30
    stop_atr_multiple: float = 1.50
    target_atr_multiple: float = 2.25
    max_holding_days: int = 20
    cooldown_days: int = 5
    capital_per_trade: float = 1_000_000.0
    risk_per_trade: float = 0.01
    max_position_ratio: float = 0.25
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_rate: float = 0.001
    slippage_rate: float = 0.001
    lot_size: int = 100


@dataclass
class StrategyEvaluation:
    """Auditable output from the optimized strategy, separate from base scoring."""

    strategy_name: str
    as_of: date | None
    data_status: str
    score: float | None
    signal: str
    confidence: float
    components: list[ScoreComponent] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key_metrics: dict[str, Any] = field(default_factory=dict)
    entry_conditions: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    risk_controls: list[str] = field(default_factory=list)


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
