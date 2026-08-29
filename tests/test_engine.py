from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stock_analysis.data import (
    CsvDataProvider,
    PublicDataProvider,
    StockDataProvider,
    StockDataService,
    exchange_for_code,
    normalize_code,
)
from stock_analysis.db import SQLiteStore
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import PriceHistory, Security
from stock_analysis.paper import PaperTradingService
from stock_analysis.scoring import evaluate_all_horizons, evaluate_stock, signal_for_score


class FixtureProvider(StockDataProvider):
    """Deterministic test fixture; production has no synthetic data provider."""

    source_name = "test-fixture"
    SECURITY = Security("600519", "测试股票", "SSE")

    def resolve_candidates(self, query: str) -> list[Security]:
        code = normalize_code(query)
        if code == self.SECURITY.code or str(query).strip() == self.SECURITY.name:
            return [self.SECURITY]
        return []

    def load_market_data(self, security: Security, start_date: date, end_date: date) -> pd.DataFrame:
        dates = pd.bdate_range(start=start_date, end=end_date)
        close = np.linspace(10.0, 30.0, len(dates))
        return pd.DataFrame({
            "date": dates, "open": close * 0.99, "high": close * 1.01,
            "low": close * 0.98, "close": close,
            "volume": np.full(len(dates), 100_000.0), "amount": close * 100_000,
        })

    def load_financials(self, security: Security) -> dict:
        return {"pe": 18.0, "pb": 2.0, "roe": 12.0, "revenue_growth": 10.0,
                "profit_growth": 12.0, "debt_ratio": 45.0, "risk_flags": []}


def build_fixture(tmp_path):
    store = SQLiteStore(tmp_path / "test.db")
    service = StockDataService(FixtureProvider(), store)
    security = service.resolve_security("600519")
    history = service.load_market_data(security, date.today() - timedelta(days=365 * 3), date.today())
    indicators = calculate_indicators(history)
    financials = service.load_financials(security)
    return store, service, security, history, indicators, financials


def test_code_normalization_and_exchange():
    assert normalize_code("600519.SH") == "600519"
    assert normalize_code("SZ000001") == "000001"
    assert exchange_for_code("600519") == "SSE"
    assert exchange_for_code("300750") == "SZSE"
    assert exchange_for_code("830799") == "BSE"


def test_fixture_resolution_and_invalid_query(tmp_path):
    service = StockDataService(FixtureProvider(), SQLiteStore(tmp_path / "query.db"))
    assert service.resolve_security("测试股票").code == "600519"
    with pytest.raises(LookupError):
        service.resolve_security("不存在")
    with pytest.raises(ValueError, match="请输入"):
        service.resolve_security("   ")
    with pytest.raises(ValueError, match="过长"):
        service.resolve_security("x" * 65)


def _quote_response(name: str) -> object:
    class Response:
        content = f'v_sh603993="1~{name}~"'.encode("gb18030")
        def raise_for_status(self):
            return None
    return Response()


def test_code_resolution_uses_live_tencent_name_and_correct_exchange(monkeypatch):
    calls = []
    def fake_get(url, **kwargs):
        calls.append(url)
        return _quote_response("洛阳钼业")
    monkeypatch.setattr("stock_analysis.data.requests.get", fake_get)
    provider = object.__new__(PublicDataProvider)
    provider.ak = None
    provider.latest_valuation = {}
    security = provider.resolve_security("603993")
    assert security == Security("603993", "洛阳钼业", "SSE")
    assert calls == ["https://qt.gtimg.cn/q=sh603993"]


def test_code_resolution_still_works_when_live_name_lookup_fails(monkeypatch):
    monkeypatch.setattr("stock_analysis.data.requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("blocked")))
    provider = object.__new__(PublicDataProvider)
    provider.ak = None
    provider.latest_valuation = {}
    security = provider.resolve_security("603993")
    assert security == Security("603993", "代码 603993", "SSE")


def test_name_resolution_reports_list_endpoint_failure():
    class BrokenListRequest:
        def stock_info_a_code_name(self):
            raise ConnectionError("blocked")
    provider = object.__new__(PublicDataProvider)
    provider.ak = BrokenListRequest()
    with pytest.raises(RuntimeError, match="改用6位股票代码查询"):
        provider.resolve_candidates("贵州茅台")


def _tencent_response(rows: list[list[str]]) -> object:
    import json
    payload = {"code": 0, "data": {"sz000001": {"qfqday": rows}}}
    class Response:
        text = "kline_day20260106=" + json.dumps(payload) + ";"
        def raise_for_status(self):
            return None
    return Response()


def test_tencent_history_parses_prefixed_payload_filters_dates_and_uses_lots(monkeypatch):
    class BrokenAkShare:
        def stock_zh_a_hist(self, **kwargs):
            raise AssertionError("Tencent should be tried before AKShare")
    rows = [["2025-12-31", "9.00", "9.50", "9.60", "8.90", "90"],
            ["2026-01-02", "10.00", "10.50", "10.60", "9.90", "100"],
            ["2026-01-05", "10.50", "10.20", "10.70", "10.10", "120"],
            ["2026-01-07", "10.20", "10.30", "10.40", "10.00", "130"]]
    provider = object.__new__(PublicDataProvider)
    provider.ak = BrokenAkShare()
    provider.last_market_data_note = ""
    provider.last_market_data_source = "public"
    provider.latest_valuation = {}
    monkeypatch.setattr("stock_analysis.data.requests.get", lambda *args, **kwargs: _tencent_response(rows))
    result = provider.load_market_data(Security("000001", "平安银行", "SZSE"), date(2026, 1, 1), date(2026, 1, 6))
    assert provider.last_market_data_source == "tencent"
    assert result["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-02", "2026-01-05"]
    assert result["close"].tolist() == [10.5, 10.2]
    assert result["volume"].tolist() == [10000.0, 12000.0]


def test_tencent_is_primary_and_does_not_call_akshare(monkeypatch):
    provider = object.__new__(PublicDataProvider)
    provider.ak = type("BrokenAkShare", (), {"stock_zh_a_hist": lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())})()
    provider.last_market_data_note = ""
    provider.last_market_data_source = "public"
    provider.latest_valuation = {}
    expected = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [10000.0], "amount": [105000.0]})
    monkeypatch.setattr(provider, "_load_tencent_market_data", lambda *args: expected)
    result = provider.load_market_data(Security("600519", "贵州茅台", "SSE"), date(2026, 1, 1), date(2026, 1, 3))
    assert result["close"].tolist() == [10.5]
    assert provider.last_market_data_source == "tencent"


def test_public_service_returns_data_insufficient_when_both_sources_fail(tmp_path):
    class BrokenAkShare:
        def stock_zh_a_hist(self, **kwargs):
            raise ConnectionError("akshare blocked")
    provider = object.__new__(PublicDataProvider)
    provider.ak = BrokenAkShare()
    provider.last_market_data_note = ""
    provider.last_market_data_source = "public"
    provider.latest_valuation = {}
    provider._load_tencent_market_data = lambda *args: (_ for _ in ()).throw(ConnectionError("tencent blocked"))
    service = StockDataService(provider, SQLiteStore(tmp_path / "failed.db"))
    history = service.load_market_data(Security("600519", "贵州茅台", "SSE"), date(2026, 1, 1), date(2026, 1, 3))
    assert history.data.empty
    assert history.status == "error"
    assert "腾讯历史行情和 AKShare 备用接口均不可用" in history.message


def test_live_valuation_is_merged_into_cached_financials(tmp_path):
    store = SQLiteStore(tmp_path / "financials.db")
    security = Security("600519", "贵州茅台", "SSE")
    store.save_financials(security.symbol, {"roe": 18.0, "risk_flags": []}, source="akshare")
    provider = object.__new__(PublicDataProvider)
    provider.ak = None
    provider.latest_valuation = {"pe": 22.0, "pb": 3.0}

    financials = StockDataService(provider, store).load_financials(security)

    assert financials["pe"] == 22.0
    assert financials["pb"] == 3.0
    assert financials["roe"] == 18.0


def test_csv_provider_uses_uploaded_prices_only():
    frame = pd.DataFrame({"日期": pd.date_range("2026-01-01", periods=3), "开盘": [10, 11, 12], "最高": [11, 12, 13], "最低": [9, 10, 11], "收盘": [10.5, 11.5, 12.5], "成交量": [100, 110, 120]})
    provider = CsvDataProvider(frame)
    result = provider.load_market_data(provider.resolve_security("000001"), date(2026, 1, 1), date(2026, 1, 3))
    assert result["close"].tolist() == [10.5, 11.5, 12.5]


def test_source_aware_price_cache_keeps_real_sources_separate(tmp_path):
    store = SQLiteStore(tmp_path / "sources.db")
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0], "amount": [1050.0]})
    store.upsert_price_bars("SZSE.000001", frame, source="tencent")
    store.upsert_price_bars("SZSE.000001", frame.assign(close=[11.5]), source="akshare")
    tencent = store.load_price_bars("SZSE.000001", date(2026, 1, 2), date(2026, 1, 2), sources=("tencent",))
    akshare = store.load_price_bars("SZSE.000001", date(2026, 1, 2), date(2026, 1, 2), sources=("akshare",))
    assert tencent["close"].tolist() == [10.5]
    assert akshare["close"].tolist() == [11.5]


def test_security_and_financial_cache(tmp_path):
    store, service, security, _, _, financials = build_fixture(tmp_path)
    assert store.find_securities(code="600519")[0] == security
    assert store.load_financials(security.symbol, sources=("test-fixture",))["roe"] == financials["roe"]


def test_indicators_and_all_horizons(tmp_path):
    _, _, security, history, indicators, financials = build_fixture(tmp_path)
    assert len(history.data) > 500
    assert indicators.latest["close"] > 0
    results = evaluate_all_horizons(indicators, financials, today=indicators.as_of)
    assert set(results) == {"short", "swing", "long"}
    assert all(result.score is not None for result in results.values())
    assert all(result.security == security for result in results.values())


def test_indicators_reject_short_history():
    provider = FixtureProvider()
    short_history = provider.load_market_data(provider.SECURITY, date.today() - timedelta(days=90), date.today())
    with pytest.raises(ValueError, match="历史数据不足"):
        calculate_indicators(PriceHistory(provider.SECURITY, short_history, "test-fixture"))


def test_signal_thresholds_are_stable():
    assert signal_for_score(70) == "买入候选"
    assert signal_for_score(69.9) == "观望/持有"
    assert signal_for_score(45) == "观望/持有"
    assert signal_for_score(44.9) == "减仓/卖出倾向"


def test_risk_flag_overrides_score(tmp_path):
    _, _, _, _, indicators, financials = build_fixture(tmp_path)
    financials["risk_flags"] = ["测试风险标记"]
    result = evaluate_stock(indicators, financials, "short", today=indicators.as_of)
    assert result.data_status == "insufficient"
    assert result.score is None
    assert result.signal == "数据不足/不可判断"


def test_stale_data_is_not_actionable(tmp_path):
    _, _, _, _, indicators, financials = build_fixture(tmp_path)
    result = evaluate_stock(indicators, financials, "short", today=indicators.as_of + timedelta(days=8))
    assert result.data_status == "insufficient"
    assert "超过7天" in " ".join(result.warnings)


def test_paper_buy_sell_and_portfolio(tmp_path):
    store = SQLiteStore(tmp_path / "paper.db")
    service = PaperTradingService(store, commission_rate=0, slippage_rate=0, minimum_commission=0, stamp_tax_rate=0)
    service.ensure_account("test", 100_000)
    buy = service.create_paper_order("test", "SSE.600519", "买入", 100, 10.0, date(2026, 1, 2))
    assert buy.status == "filled"
    portfolio = service.get_portfolio("test", {"SSE.600519": 12.0})
    assert portfolio.positions[0].quantity == 100
    assert portfolio.positions[0].unrealized_pnl == pytest.approx(200)
    service.create_paper_order("test", "SSE.600519", "卖出", 100, 12.0, date(2026, 1, 3))
    after = service.get_portfolio("test", {"SSE.600519": 12.0})
    assert after.positions == []
    assert after.realized_pnl == pytest.approx(200)


def test_paper_orders_reject_invalid_lots_and_oversell(tmp_path):
    store = SQLiteStore(tmp_path / "paper.db")
    service = PaperTradingService(store, slippage_rate=0, minimum_commission=0, stamp_tax_rate=0)
    service.ensure_account("test", 100_000)
    with pytest.raises(ValueError):
        service.create_paper_order("test", "SSE.600519", "买入", 50, 10.0)
    with pytest.raises(ValueError):
        service.create_paper_order("test", "SSE.600519", "卖出", 100, 10.0)


def test_paper_rejects_untrusted_symbol_format(tmp_path):
    store = SQLiteStore(tmp_path / "paper.db")
    service = PaperTradingService(store)
    with pytest.raises(ValueError, match="股票标识格式无效"):
        service.create_paper_order("test", "DROP TABLE paper_orders", "买入", 100, 10.0)


def test_app_has_no_virtual_data_mode():
    with open("app.py", encoding="utf-8") as handle:
        app_source = handle.read()
    assert "虚构演示数据" not in app_source
    assert "offline-demo" not in app_source
