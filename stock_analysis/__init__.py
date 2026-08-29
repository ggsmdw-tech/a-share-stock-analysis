"""Core package for the A-share analysis application."""

from .data import StockDataService, create_provider
from .indicators import calculate_indicators
from .scoring import evaluate_all_horizons, evaluate_stock

__all__ = [
    "StockDataService",
    "create_provider",
    "calculate_indicators",
    "evaluate_stock",
    "evaluate_all_horizons",
]
