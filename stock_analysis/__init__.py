"""Core package for the A-share analysis application."""

from .data import StockDataService, create_provider
from .indicators import calculate_indicators
from .scoring import evaluate_all_horizons, evaluate_stock
from .strategy import DEFAULT_STRATEGY, evaluate_strategy
from .backtest import backtest_strategy
from .models import StrategyConfig, StrategyEvaluation

__all__ = [
    "StockDataService",
    "create_provider",
    "calculate_indicators",
    "evaluate_stock",
    "evaluate_all_horizons",
    "DEFAULT_STRATEGY",
    "evaluate_strategy",
    "backtest_strategy",
    "StrategyConfig",
    "StrategyEvaluation",
]
