from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import requests

from .db import SQLiteStore
from .models import MoneyFlowHistory, PriceHistory, Security


KNOWN_SECURITY_NAMES = {
    "000001": "平安银行",
    "300750": "宁德时代",
    "600519": "贵州茅台",
    "601318": "中国平安",
}


def normalize_code(query: str) -> str:
    value = str(query or "").strip().upper()
    match = re.search(r"(\d{6})", value)
    if not match:
        return ""
    return match.group(1)


def exchange_for_code(code: str) -> str:
    if code.startswith(("6", "68")):
        return "SSE"
    if code.startswith(("0", "2", "3")):
        return "SZSE"
    if code.startswith(("4", "8")):
        return "BSE"
    return "UNKNOWN"


def security_name_for_code(code: str) -> str:
    return KNOWN_SECURITY_NAMES.get(code, f"代码 {code}")


def _standardize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "date",
        "交易日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    frame = frame.rename(columns={key: value for key, value in aliases.items()})
    required = ["date", "open", "high", "low", "close"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=required)
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return frame[["date", "open", "high", "low", "close", "volume", "amount"]]


def _standardize_money_flow(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize provider-estimated daily fund-flow fields."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("公开主力资金接口返回空数据")

    aliases = {
        "date": ["date", "日期", "交易日期"],
        "main_net_inflow": [
            "main_net_inflow",
            "主力净流入-净额",
            "主力净流入净额",
            "主力净流入",
        ],
        "main_net_ratio": [
            "main_net_ratio",
            "主力净流入-净占比",
            "主力净流入净占比",
        ],
        "close": ["close", "收盘价", "收盘"],
        "change": ["change", "涨跌幅", "涨跌"],
        "super_large_net_inflow": ["超大单净流入-净额", "超大单净流入"],
        "large_net_inflow": ["大单净流入-净额", "大单净流入"],
        "medium_net_inflow": ["中单净流入-净额", "中单净流入"],
        "small_net_inflow": ["小单净流入-净额", "小单净流入"],
    }
    normalized_columns = {
        str(column).strip().lower().replace(" ", ""): column
        for column in frame.columns
    }
    selected: dict[str, Any] = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            source_column = normalized_columns.get(candidate.lower().replace(" ", ""))
            if source_column is not None:
                selected[target] = source_column
                break

    missing = [key for key in ("date", "main_net_inflow") if key not in selected]
    if missing:
        raise ValueError(f"主力资金数据缺少字段: {', '.join(missing)}")

    result = pd.DataFrame()
    for target, source_column in selected.items():
        result[target] = frame[source_column]
    for column in aliases:
        if column not in result:
            result[column] = np.nan

    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    for column in result.columns:
        if column != "date":
            result[column] = pd.to_numeric(
                result[column].astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
                errors="coerce",
            )
    result = (
        result.dropna(subset=["date", "main_net_inflow"])
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )
    return result[
        [
            "date",
            "main_net_inflow",
            "main_net_ratio",
            "close",
            "change",
            "super_large_net_inflow",
            "large_net_inflow",
            "medium_net_inflow",
            "small_net_inflow",
        ]
    ]


class StockDataProvider(ABC):
    source_name = "unknown"

    @abstractmethod
    def resolve_candidates(self, query: str) -> list[Security]:
        raise NotImplementedError

    def resolve_security(self, query: str) -> Security:
        if not str(query or "").strip():
            raise ValueError("请输入股票名称或6位代码")
        if len(str(query).strip()) > 64:
            raise ValueError("股票查询内容过长")
        candidates = self.resolve_candidates(query)
        if not candidates:
            raise LookupError(f"没有找到股票: {query}")
        if len(candidates) > 1:
            names = "、".join(f"{item.code} {item.name}" for item in candidates[:8])
            raise LookupError(f"匹配到多只股票，请输入代码: {names}")
        return candidates[0]

    @abstractmethod
    def load_market_data(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        raise NotImplementedError

    def load_money_flow(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        raise NotImplementedError

    def load_financials(self, security: Security) -> dict[str, Any]:
        return {}


class CsvDataProvider(StockDataProvider):
    """Provider for a user-supplied OHLC CSV file.

    The file is treated as an external source of truth. It is never filled
    with synthetic rows and it is kept separate from public-source caches.
    """

    source_name = "csv-import"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = _standardize_bars(frame)

    def resolve_candidates(self, query: str) -> list[Security]:
        code = normalize_code(query)
        if not code or exchange_for_code(code) == "UNKNOWN":
            return []
        return [
            Security(
                code=code,
                name=security_name_for_code(code),
                exchange=exchange_for_code(code),
            )
        ]

    def load_market_data(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        frame = self.frame[
            (self.frame["date"].dt.date >= start_date)
            & (self.frame["date"].dt.date <= end_date)
        ].copy()
        if frame.empty:
            raise ValueError("CSV文件在所选日期范围内没有行情数据")
        return frame


class PublicDataProvider(StockDataProvider):
    """Real public-source provider: Tencent historical data first, AKShare second."""

    source_name = "public"

    def __init__(self) -> None:
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            ak = None
        self.ak = ak
        self.last_resolution_note = ""
        self.last_market_data_note = ""
        self.last_market_data_source = self.source_name
        self.last_money_flow_source = "akshare"
        self.latest_valuation: dict[str, float | None] = {}

    def resolve_candidates(self, query: str) -> list[Security]:
        value = str(query or "").strip().lower()
        code = normalize_code(value)

        if code and exchange_for_code(code) != "UNKNOWN":
            name = self._resolve_tencent_quote(code)
            return [
                Security(
                    code=code,
                    name=name or security_name_for_code(code),
                    exchange=exchange_for_code(code),
                )
            ]

        if self.ak is None:
            raise RuntimeError("名称查询需要 AKShare 股票列表接口，请改用6位股票代码查询。")
        try:
            frame = self.ak.stock_info_a_code_name()
        except Exception as exc:
            raise RuntimeError(
                "股票列表接口暂时不可用，请改用6位股票代码查询。"
            ) from exc
        frame = frame.rename(columns={"code": "code", "name": "name"})
        if "code" not in frame or "name" not in frame:
            raise ValueError("股票列表接口字段发生变化")
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame = frame[
            frame["name"].astype(str).str.lower().str.contains(
                value, na=False, regex=False
            )
        ]
        results = []
        for row in frame.head(20).itertuples(index=False):
            name = str(getattr(row, "name"))
            results.append(
                Security(
                    code=str(getattr(row, "code")).zfill(6),
                    name=name,
                    exchange=exchange_for_code(str(getattr(row, "code")).zfill(6)),
                    market_status="风险标记" if "ST" in name.upper() or "退" in name else "正常",
                )
            )
        return results

    def load_market_data(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        self.last_market_data_note = ""
        self.last_market_data_source = self.source_name

        try:
            frame = self._load_tencent_market_data(security, start_date, end_date)
            self.last_market_data_source = "tencent"
            return frame
        except Exception as tencent_error:
            tencent_message = str(tencent_error)

        def fetch_range(begin: date, finish: date) -> pd.DataFrame:
            if self.ak is None:
                raise RuntimeError("AKShare未安装")
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    raw = self.ak.stock_zh_a_hist(
                        symbol=security.code,
                        period="daily",
                        start_date=pd.Timestamp(begin).strftime("%Y%m%d"),
                        end_date=pd.Timestamp(finish).strftime("%Y%m%d"),
                        adjust="qfq",
                    )
                    if raw is None or raw.empty:
                        raise ValueError("公开行情接口返回空数据")
                    for column in ("市盈率-动态", "市盈率", "PE"):
                        if column in raw.columns and len(raw):
                            self.latest_valuation["pe"] = self._number(raw.iloc[-1][column])
                            break
                    for column in ("市净率", "PB"):
                        if column in raw.columns and len(raw):
                            self.latest_valuation["pb"] = self._number(raw.iloc[-1][column])
                            break
                    return _standardize_bars(raw)
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(0.25)
            raise RuntimeError(f"公开行情接口暂时不可用: {last_error}") from last_error

        requested_days = (end_date - start_date).days
        try:
            if requested_days <= 370:
                frame = fetch_range(start_date, end_date)
            else:
                # Long single requests are frequently dropped by public
                # upstream endpoints, so split them before using the backup.
                chunks = []
                cursor = start_date
                while cursor <= end_date:
                    chunk_end = min(cursor + timedelta(days=364), end_date)
                    chunks.append(fetch_range(cursor, chunk_end))
                    cursor = chunk_end + timedelta(days=1)
                frame = _standardize_bars(pd.concat(chunks, ignore_index=True))
            self.last_market_data_source = "akshare"
            self.last_market_data_note = "腾讯历史行情接口不可用，已切换 AKShare 公开行情备用源。"
            return frame
        except Exception as akshare_error:
            raise RuntimeError(
                "公开行情暂时无法访问：腾讯历史行情和 AKShare 备用接口均不可用。"
                f"腾讯原因：{tencent_message}；AKShare原因：{akshare_error}。"
                "请检查本机网络或稍后重试。"
            ) from akshare_error

    def _load_tencent_market_data(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(security.exchange)
        if prefix is None:
            raise ValueError(f"不支持的交易所: {security.exchange}")
        symbol = f"{prefix}{security.code}"
        endpoint = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        frames = []
        for year in range(start_date.year, end_date.year + 1):
            begin = max(start_date, date(year, 1, 1))
            finish = min(end_date, date(year, 12, 31))
            response = requests.get(
                endpoint,
                params={
                    "_var": f"kline_day{finish:%Y%m%d}",
                    "param": (
                        f"{symbol},day,{begin.isoformat()},{finish.isoformat()},"
                        "640,qfq"
                    ),
                    "r": f"{time.time():.6f}",
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://stockapp.finance.qq.com/",
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = self._parse_tencent_payload(response.text)
            symbol_data = payload.get("data", {}).get(symbol, {})
            rows = (
                symbol_data.get("qfqday")
                or symbol_data.get("day")
                or symbol_data.get("hfqday")
                or []
            )
            parsed = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    continue
                parsed.append(
                    {
                        "date": row[0],
                        "open": self._number(row[1]),
                        "close": self._number(row[2]),
                        "high": self._number(row[3]),
                        "low": self._number(row[4]),
                        # Tencent reports A-share daily volume in lots.
                        "volume": (self._number(row[5]) or 0.0) * 100,
                    }
                )
            if parsed:
                frame = pd.DataFrame(parsed)
                frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                frame = frame[
                    (frame["date"].dt.date >= start_date)
                    & (frame["date"].dt.date <= end_date)
                ]
                if frame.empty:
                    continue
                frame["amount"] = frame["close"] * frame["volume"]
                frames.append(frame)
        if not frames:
            raise ValueError("腾讯公开行情接口返回空数据")
        return _standardize_bars(pd.concat(frames, ignore_index=True))

    def load_money_flow(
        self, security: Security, start_date: date, end_date: date
    ) -> pd.DataFrame:
        if self.ak is None:
            raise RuntimeError("主力资金数据需要安装 AKShare 公开数据接口。")
        market = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(
            security.exchange
        )
        if market is None:
            raise ValueError("暂不支持该股票交易所的主力资金数据")
        try:
            frame = self.ak.stock_individual_fund_flow(
                stock=security.code,
                market=market,
            )
        except Exception as exc:
            raise RuntimeError("公开主力资金接口暂时不可用，请检查网络或稍后重试。") from exc
        self.last_money_flow_source = "akshare"
        return frame

    def _resolve_tencent_quote(self, code: str) -> str | None:
        prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange_for_code(code))
        if prefix is None:
            return None
        try:
            response = requests.get(
                "https://qt.gtimg.cn/q=" + prefix + code,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
            match = re.search(r'="(.*?)"', text, flags=re.S)
            if not match:
                return None
            fields = match.group(1).split("~")
            name = fields[1].strip() if len(fields) > 1 else ""
            # Tencent's public quote layout places dynamic PE at field 39 and
            # PB at field 46. Leave missing/invalid values absent rather than
            # treating them as zero or inventing a valuation.
            pe = self._number(fields[39]) if len(fields) > 39 else None
            pb = self._number(fields[46]) if len(fields) > 46 else None
            if pe is not None and pe >= 0:
                self.latest_valuation["pe"] = pe
            if pb is not None and pb >= 0:
                self.latest_valuation["pb"] = pb
            return name or None
        except Exception:
            return None

    def refresh_valuation(self, security: Security) -> dict[str, float | None]:
        """Refresh quote valuation fields when a financial cache is incomplete."""
        self._resolve_tencent_quote(security.code)
        return dict(self.latest_valuation)

    @staticmethod
    def _parse_tencent_payload(text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        if "=" in value:
            value = value.split("=", 1)[1].strip()
        value = value.rstrip(";").strip()
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("腾讯行情接口返回格式异常")
        return payload

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        cleaned = str(value).replace(",", "").replace("%", "").strip()
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None

    def load_financials(self, security: Security) -> dict[str, Any]:
        if self.ak is None:
            return {}
        try:
            frame = self.ak.stock_financial_analysis_indicator(symbol=security.code)
        except Exception:
            return {}

        if frame is None or frame.empty:
            return {}
        row = frame.iloc[-1].to_dict()
        aliases = {
            "roe": ["净资产收益率(%)", "加权净资产收益率(%)", "净资产收益率", "ROE", "roe"],
            "revenue_growth": ["主营业务收入增长率(%)", "营业收入增长率", "营业收入同比增长率", "revenue_growth"],
            "profit_growth": ["净利润增长率(%)", "净利润增长率", "净利润同比增长率", "profit_growth"],
            "debt_ratio": ["资产负债率(%)", "资产负债率", "负债率", "debt_ratio"],
        }
        result: dict[str, Any] = {"risk_flags": [], **self.latest_valuation}
        for key, possible_names in aliases.items():
            for name in possible_names:
                if name in row:
                    result[key] = self._number(row[name])
                    break
        return result


class StockDataService:
    def __init__(self, provider: StockDataProvider, store: SQLiteStore | None = None) -> None:
        self.provider = provider
        self.store = store

    def resolve_security(self, query: str) -> Security:
        value = str(query or "").strip()
        if not value:
            raise ValueError("请输入股票名称或6位代码")
        if len(value) > 64:
            raise ValueError("股票查询内容过长")
        code = normalize_code(value)
        # A code query is cheap to resolve against the live quote endpoint and
        # must not be satisfied by an old cache entry with a stale name.
        if self.store is not None and not code:
            cached = self.store.find_securities(
                code=code, name_fragment="" if code else value
            )
            if len(cached) == 1:
                return cached[0]
            if len(cached) > 1:
                names = "、".join(f"{item.code} {item.name}" for item in cached[:8])
                raise LookupError(f"匹配到多只股票，请输入代码: {names}")
        candidates = self.provider.resolve_candidates(value)
        if self.store is not None:
            self.store.upsert_securities(candidates)
        if not candidates:
            raise LookupError(f"没有找到股票: {value}")
        if len(candidates) > 1:
            names = "、".join(f"{item.code} {item.name}" for item in candidates[:8])
            raise LookupError(f"匹配到多只股票，请输入代码: {names}")
        return candidates[0]

    def load_market_data(
        self, security: Security, start_date: date, end_date: date
    ) -> PriceHistory:
        symbol = security.symbol
        try:
            frame = _standardize_bars(self.provider.load_market_data(security, start_date, end_date))
            if self.store is not None:
                self.store.upsert_price_bars(
                    symbol,
                    frame,
                    source=getattr(self.provider, "last_market_data_source", self.provider.source_name),
                )
            return PriceHistory(
                security,
                frame,
                getattr(self.provider, "last_market_data_source", self.provider.source_name),
                message=getattr(self.provider, "last_market_data_note", ""),
            )
        except Exception as exc:
            if self.store is not None:
                cached = self.store.load_price_bars(
                    symbol,
                    start_date,
                    end_date,
                    sources=("akshare", "tencent"),
                )
                if not cached.empty:
                    return PriceHistory(
                        security,
                        cached,
                        "sqlite-cache",
                        status="stale-cache",
                        message="公开数据获取失败，已使用本地缓存；缓存结果不可直接用于买卖判断。",
                    )
            if isinstance(exc, (ValueError, LookupError, RuntimeError)):
                message = str(exc)
            else:
                message = "公开数据接口暂时不可用，请检查网络或稍后重试。"
            return PriceHistory(security, pd.DataFrame(), self.provider.source_name, "error", message)

    def load_financials(self, security: Security) -> dict[str, Any]:
        live_valuation = dict(getattr(self.provider, "latest_valuation", {}) or {})
        if self.store is not None:
            cached = self.store.load_financials(
                security.symbol,
                sources=(self.provider.source_name, "akshare"),
            )
            if cached is not None:
                cached_has_valuation = all(
                    cached.get(key) is not None for key in ("pe", "pb")
                )
                live_valuation_complete = all(
                    live_valuation.get(key) is not None for key in ("pe", "pb")
                )
                if not cached_has_valuation and not live_valuation_complete:
                    refresh = getattr(self.provider, "refresh_valuation", None)
                    if callable(refresh):
                        try:
                            refreshed = refresh(security)
                        except Exception:
                            refreshed = {}
                        if isinstance(refreshed, dict):
                            live_valuation.update(refreshed)
                if live_valuation:
                    cached = {**cached, **live_valuation}
                return cached
        try:
            financials = self.provider.load_financials(security)
            if live_valuation:
                financials = {**financials, **live_valuation}
            if self.store is not None and financials:
                self.store.save_financials(
                    security.symbol,
                    financials,
                    source=self.provider.source_name,
                )
            return financials
        except Exception:
            return {}

    def load_money_flow(
        self, security: Security, start_date: date, end_date: date
    ) -> MoneyFlowHistory:
        source = getattr(self.provider, "last_money_flow_source", "akshare")
        try:
            frame = _standardize_money_flow(
                self.provider.load_money_flow(security, start_date, end_date)
            )
            frame = frame[
                (frame["date"].dt.date >= start_date)
                & (frame["date"].dt.date <= end_date)
            ].copy()
            if frame.empty:
                raise ValueError("所选日期范围内没有主力资金数据")
            source = getattr(self.provider, "last_money_flow_source", source)
            if self.store is not None:
                self.store.upsert_money_flow(security.symbol, frame, source=source)
            return MoneyFlowHistory(security, frame, source, status="ok")
        except Exception as exc:
            if self.store is not None:
                cached = self.store.load_money_flow(
                    security.symbol,
                    start_date,
                    end_date,
                    sources=(source, self.provider.source_name, "akshare"),
                )
                if not cached.empty:
                    return MoneyFlowHistory(
                        security,
                        cached,
                        "sqlite-cache",
                        status="stale-cache",
                        message="公开主力资金接口获取失败，已使用本地缓存；请查看数据日期后再作研究。",
                    )
            if isinstance(exc, (ValueError, LookupError, RuntimeError)):
                message = str(exc)
            else:
                message = "公开主力资金数据暂时不可用，请检查网络或稍后重试。"
            return MoneyFlowHistory(security, pd.DataFrame(), source, status="error", message=message)


def create_provider(mode: str = "public") -> StockDataProvider:
    if mode != "public":
        raise ValueError(f"不支持的数据模式: {mode}")
    return PublicDataProvider()


# Backward-compatible import name for integrations that used the old class name.
# It is an alias for the real public provider, never a synthetic data source.
AkShareProvider = PublicDataProvider


def resolve_security(query: str, service: StockDataService) -> Security:
    return service.resolve_security(query)


def load_market_data(
    security: Security,
    start_date: date,
    end_date: date,
    service: StockDataService,
) -> PriceHistory:
    return service.load_market_data(security, start_date, end_date)


def load_financials(security: Security, service: StockDataService) -> dict[str, Any]:
    return service.load_financials(security)
