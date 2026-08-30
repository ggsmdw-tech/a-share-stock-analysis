from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .models import IndicatorSnapshot, PriceHistory


CORE_PRICE_COLUMNS = ("date", "open", "high", "low", "close", "volume")
CORE_INDICATOR_COLUMNS = (
    "sma20",
    "sma60",
    "macd_hist",
    "rsi14",
    "volatility20",
    "drawdown",
)
FINANCIAL_FIELDS = (
    "pe",
    "pb",
    "roe",
    "revenue_growth",
    "profit_growth",
    "debt_ratio",
)


@dataclass(frozen=True)
class QualityCheck:
    """One auditable data-quality check shown to the user."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DataQualityReport:
    """A data-quality assessment, deliberately separate from trading signals."""

    level: str
    score: float
    actionable: bool
    as_of: date | None
    age_days: int | None
    source: str
    row_count: int
    financial_coverage: float
    checks: tuple[QualityCheck, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _finite_series(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame.get(column, pd.Series(dtype=float)), errors="coerce")


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def assess_data_quality(
    history: PriceHistory,
    indicators: IndicatorSnapshot | None = None,
    financials: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> DataQualityReport:
    """Assess freshness, integrity and completeness without inventing data."""

    today = today or date.today()
    frame = history.data if isinstance(history.data, pd.DataFrame) else pd.DataFrame()
    as_of = history.as_of
    age_days = None if as_of is None else (today - as_of).days
    checks: list[QualityCheck] = []
    warnings: list[str] = []

    source_ok = history.status == "ok" and history.source not in {"", "unknown", "sqlite-cache"}
    if source_ok:
        checks.append(QualityCheck("数据来源", "通过", f"当前来源：{history.source}"))
    else:
        checks.append(
            QualityCheck(
                "数据来源",
                "需核查",
                history.message or "当前不是可确认的新鲜公开数据",
            )
        )
        warnings.append("行情来源或状态需要核查，不能把当前结果当作直接交易依据。")

    if age_days is None:
        freshness_ok = False
        freshness_detail = "缺少最新行情日期"
        warnings.append("缺少最新行情日期。")
    elif age_days < 0:
        freshness_ok = False
        freshness_detail = f"行情日期 {as_of} 晚于分析日期 {today}"
        warnings.append("行情日期晚于当前日期，请检查数据源。")
    elif age_days <= 7:
        freshness_ok = True
        freshness_detail = f"最新交易日 {as_of}，距分析日 {age_days} 个日历日"
    else:
        freshness_ok = False
        freshness_detail = f"最新交易日 {as_of}，已相隔 {age_days} 个日历日"
        warnings.append(f"行情数据已超过7天未更新（最新日期：{as_of}）。")
    checks.append(QualityCheck("数据新鲜度", "通过" if freshness_ok else "不足", freshness_detail))

    required_missing = [column for column in CORE_PRICE_COLUMNS if column not in frame.columns]
    row_count = len(frame)
    price_ok = not required_missing and row_count >= 120
    price_detail = f"{row_count} 条日线"
    if required_missing:
        price_detail += f"；缺少字段：{', '.join(required_missing)}"
    elif row_count < 120:
        price_detail += "；至少需要120条有效日线"
    else:
        invalid_counts = {
            column: int(_finite_series(frame, column).isna().sum())
            for column in ("open", "high", "low", "close")
        }
        invalid_total = sum(invalid_counts.values())
        high = _finite_series(frame, "high")
        low = _finite_series(frame, "low")
        close = _finite_series(frame, "close")
        open_price = _finite_series(frame, "open")
        volume = _finite_series(frame, "volume")
        ohlc_invalid = int(
            (
                (open_price <= 0)
                | (high <= 0)
                | (low <= 0)
                | (close <= 0)
                | (high < low)
                | (close > high)
                | (close < low)
            )
            .fillna(False)
            .sum()
        )
        invalid_volume = int(volume.isna().sum() + (volume <= 0).sum())
        volume_too_sparse = row_count > 0 and invalid_volume > max(5, int(row_count * 0.05))
        if invalid_total or ohlc_invalid or volume_too_sparse:
            price_ok = False
            price_detail += (
                f"；无效价格字段 {invalid_total} 项，OHLC异常 {ohlc_invalid} 行，"
                f"成交量无效 {invalid_volume} 行"
            )
        else:
            price_detail += "；OHLC字段完整且通过基本一致性检查"
    if not price_ok:
        warnings.append("历史行情长度或OHLC字段不完整。")
    checks.append(QualityCheck("行情完整性", "通过" if price_ok else "不足", price_detail))

    duplicate_count = int(frame["date"].duplicated().sum()) if "date" in frame else 0
    dates = pd.to_datetime(frame.get("date", pd.Series(dtype="datetime64[ns]")), errors="coerce")
    chronology_ok = not dates.isna().any() and dates.is_monotonic_increasing and duplicate_count == 0
    chronology_detail = "日期连续排序且无重复" if chronology_ok else f"日期异常或重复 {duplicate_count} 行"
    checks.append(QualityCheck("日期序列", "通过" if chronology_ok else "需核查", chronology_detail))
    if not chronology_ok:
        warnings.append("日期序列存在缺失、重复或未排序问题。")

    indicator_ok = False
    missing_indicators: list[str] = list(CORE_INDICATOR_COLUMNS)
    if indicators is not None:
        missing_indicators = [
            column
            for column in CORE_INDICATOR_COLUMNS
            if not _has_value(indicators.latest.get(column))
        ]
        indicator_ok = not missing_indicators and indicators.status == "ok"
    indicator_detail = "核心技术指标齐全" if indicator_ok else f"缺少：{', '.join(missing_indicators)}"
    checks.append(QualityCheck("技术指标", "通过" if indicator_ok else "不足", indicator_detail))
    if not indicator_ok:
        warnings.append("核心技术指标不完整，无法可靠解释当前规则评分。")

    financials = financials or {}
    available_financials = [key for key in FINANCIAL_FIELDS if _has_value(financials.get(key))]
    financial_coverage = len(available_financials) / len(FINANCIAL_FIELDS)
    if financial_coverage == 1:
        financial_status, financial_detail = "通过", "6/6项财务与估值指标可用"
    elif financial_coverage > 0:
        missing_financials = [key for key in FINANCIAL_FIELDS if key not in available_financials]
        financial_status = "部分"
        financial_detail = f"{len(available_financials)}/6项可用；缺少 {', '.join(missing_financials)}"
    else:
        financial_status, financial_detail = "不足", "没有可用的PE、PB、ROE、成长或负债指标"
    checks.append(QualityCheck("财务数据", financial_status, financial_detail))

    hard_fail = not (source_ok and freshness_ok and price_ok and chronology_ok and indicator_ok)
    score = 0.0
    score += 20.0 if source_ok else 0.0
    score += 20.0 if freshness_ok else 0.0
    score += 25.0 if price_ok else 0.0
    score += 15.0 if chronology_ok else 0.0
    score += 20.0 if indicator_ok else 0.0
    score = round(score, 1)
    if hard_fail:
        level = "不可直接判断"
    elif score >= 90 and financial_coverage >= 0.5:
        level = "较高"
    elif score >= 75:
        level = "可用，部分数据缺失"
    else:
        level = "需核查"

    return DataQualityReport(
        level=level,
        score=score,
        actionable=not hard_fail,
        as_of=as_of,
        age_days=age_days,
        source=history.source,
        row_count=row_count,
        financial_coverage=financial_coverage,
        checks=tuple(checks),
        warnings=tuple(dict.fromkeys(warnings)),
    )
