from __future__ import annotations

from datetime import date, timedelta
from html import escape
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any

import pandas as pd
import streamlit as st
import altair as alt

from stock_analysis.backtest import BacktestReport, backtest_buy_signals, backtest_strategy
from stock_analysis.auth import (
    active_supabase_client,
    current_user_email,
    ensure_authenticated,
    get_supabase_config,
    logout,
)
from stock_analysis.cloud_store import HybridStore, SupabaseStore
from stock_analysis.data import StockDataService, create_provider
from stock_analysis.db import SQLiteStore
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import (
    IndicatorSnapshot,
    MoneyFlowHistory,
    PriceHistory,
    Security,
    TradePlan,
    TradeReview,
)
from stock_analysis.paper import PaperTradingService
from stock_analysis.position import recommend_position_action
from stock_analysis.quality import assess_data_quality
from stock_analysis.scoring import evaluate_all_horizons
from stock_analysis.strategy import DEFAULT_STRATEGY, evaluate_strategy
from stock_analysis.trading_plan import calculate_position_size


APP_VERSION = "v0.04 多用户云端持久化版"
LOCAL_ACCOUNT_ID = "local-default"

st.set_page_config(page_title=f"A股智能分析 · {APP_VERSION}", page_icon="📈", layout="wide")

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_LABELS = {
    "public": "公开数据",
    "akshare": "AKShare公开数据",
    "tencent": "腾讯公开行情",
    "sina": "新浪公开行情",
    "sqlite-cache": "本地公开数据缓存",
}
FINANCIAL_METRIC_GUIDE = (
    {
        "key": "pe",
        "label": "PE（市盈率）",
        "meaning": "股价相当于公司一年每股盈利的多少倍。PE为20，粗略理解为市场愿意用20元买公司1元的年度盈利。",
        "reading": "通常越低代表估值压力越小，但低PE也可能意味着市场担心公司未来盈利下降；应和同行业、公司历史水平比较。",
        "scoring": "≤15分档80；15–30分档60；30–50分档40；>50分档20。",
    },
    {
        "key": "pb",
        "label": "PB（市净率）",
        "meaning": "股价相对于每股净资产的倍数。PB为2，粗略理解为市场愿意用2元买公司账面上1元的净资产。",
        "reading": "通常越低代表相对净资产的估值越低，但净资产的质量、行业盈利能力和资产轻重都会影响意义。",
        "scoring": "≤1.5分档80；1.5–3分档60；3–6分档40；>6分档20。",
    },
    {
        "key": "roe",
        "label": "ROE（净资产收益率）",
        "meaning": "公司用股东投入的每100元资金，一年赚取多少元利润。例如ROE为15%，表示每100元股东权益约产生15元利润。",
        "reading": "较高且稳定通常说明资金使用效率较好，但高负债也可能抬高ROE；应观察连续多年和同行业对比。",
        "scoring": "≥15%分档80；10%–15%分档65；5%–10%分档45；<5%分档25。",
    },
    {
        "key": "revenue_growth",
        "label": "营收增速",
        "meaning": "公司销售收入相比上一报告期增长或下降的幅度，反映业务规模是在扩大还是收缩。",
        "reading": "增长是积极信号，但营收增加不等于利润增加，还要结合利润增速、现金流和毛利率判断质量。",
        "scoring": "≥20%分档85；10%–20%分档70；0%–10%分档55；-10%–0%分档35；<-10%分档15。",
    },
    {
        "key": "profit_growth",
        "label": "利润增速",
        "meaning": "公司净利润相比上一报告期增长或下降的幅度，直接反映赚到的钱是在增加还是减少。",
        "reading": "利润增长通常比单纯营收增长更重要，但一次性收益、低基数或会计变化可能造成短期失真，应查看报告说明。",
        "scoring": "≥20%分档85；10%–20%分档70；0%–10%分档55；-10%–0%分档35；<-10%分档15。",
    },
    {
        "key": "debt_ratio",
        "label": "资产负债率",
        "meaning": "公司资产中有多少比例来自负债。资产负债率为60%，可粗略理解为每100元资产中有60元来自借款或应付款。",
        "reading": "通常越低代表财务杠杆和偿债压力越小，但合理水平因行业不同而不同；房地产、金融等行业不能直接套用普通行业标准。",
        "scoring": "≤40%分档80；40%–60%分档60；60%–80%分档35；>80%分档15。",
    },
)


@st.cache_resource
def get_cache_store() -> SQLiteStore:
    return SQLiteStore(PROJECT_ROOT / "data" / "stock_analysis.db")


def get_store() -> Any:
    """Return local SQLite in development and user-scoped Supabase online."""
    if _is_local_runtime():
        return get_cache_store()
    config = get_supabase_config()
    if config is None:
        raise RuntimeError("线上模式尚未配置 Supabase。")
    return HybridStore(
        get_cache_store(),
        SupabaseStore(active_supabase_client(), get_paper_account_id()),
    )


def cloud_persistence_error_message(exc: Exception) -> str:
    """Translate common deployment mistakes into an actionable Chinese message."""
    message = str(exc).lower()
    if (
        "relation" in message and "does not exist" in message
    ) or "pgrst205" in message:
        return "云端数据库尚未初始化，请管理员在 Supabase SQL Editor 执行 supabase/schema.sql。"
    if "row-level security" in message or "permission denied" in message:
        return "云端数据库权限配置不完整，请检查各用户表的 RLS 策略和登录状态。"
    if "jwt" in message or "not authenticated" in message:
        return "登录状态已失效，请退出后重新登录。"
    return "云端个人数据服务暂时不可用，请稍后重试；若持续出现，请检查 Supabase 配置。"


def initialize_session_state() -> None:
    shared_symbol = str(st.query_params.get("symbol", "")).strip()
    previous_account_id = st.session_state.get("paper_account_id")
    previous_watchlist = st.session_state.get("watchlist")
    previous_recent_queries = st.session_state.get("recent_queries")
    previous_analysis_snapshots = st.session_state.get("analysis_snapshots")
    account_id = get_paper_account_id()
    store = get_store()
    if _is_local_runtime():
        store.migrate_latest_session_account(LOCAL_ACCOUNT_ID)
    st.session_state.setdefault("analysis_query", shared_symbol or "600519")
    st.session_state.setdefault("auto_analyze", bool(shared_symbol))
    if st.session_state.get("_persistence_loaded_account") != account_id:
        if previous_account_id and previous_account_id != account_id and _is_local_runtime():
            if isinstance(previous_watchlist, dict):
                for item in previous_watchlist.values():
                    try:
                        store.save_watchlist_item(
                            account_id,
                            Security(
                                item["code"],
                                item["name"],
                                item["exchange"],
                                item.get("market_status", "正常"),
                            ),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            if isinstance(previous_recent_queries, list):
                for item in previous_recent_queries:
                    try:
                        store.save_recent_query(
                            account_id,
                            Security(
                                item["code"],
                                item["name"],
                                item["exchange"],
                                item.get("market_status", "正常"),
                            ),
                            item.get("as_of"),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            if isinstance(previous_analysis_snapshots, dict):
                for symbol, item in previous_analysis_snapshots.items():
                    if not isinstance(item, dict):
                        continue
                    store.save_analysis_snapshot(
                        account_id,
                        symbol,
                        item.get("price"),
                        item.get("score"),
                        item.get("as_of"),
                    )
        elif previous_account_id and previous_account_id != account_id:
            for key in ("analysis", "analysis_error", "analysis_key", "analysis_alerts"):
                st.session_state.pop(key, None)

        loaded_watchlist = store.load_watchlist(account_id)
        st.session_state["watchlist"] = {
            item.symbol: {
                "symbol": item.symbol,
                "code": item.code,
                "name": item.name,
                "exchange": item.exchange,
                "market_status": item.market_status,
            }
            for item in loaded_watchlist
        }
        st.session_state["recent_queries"] = store.load_recent_queries(account_id, limit=10)
        st.session_state["analysis_snapshots"] = store.load_analysis_snapshots(
            account_id, limit=30
        )
        alert_settings = store.load_alert_settings(account_id)
        if alert_settings:
            st.session_state["alerts_enabled"] = bool(alert_settings.get("enabled", True))
            st.session_state["price_alert_threshold"] = float(
                alert_settings.get("price_threshold", 3.0)
            )
            st.session_state["score_alert_threshold"] = float(
                alert_settings.get("score_threshold", 5.0)
            )
        else:
            st.session_state["alerts_enabled"] = True
            st.session_state["price_alert_threshold"] = 3.0
            st.session_state["score_alert_threshold"] = 5.0
        st.session_state["_saved_alert_settings"] = None
        st.session_state["_persistence_loaded_account"] = account_id
        if not shared_symbol and not previous_account_id and st.session_state["recent_queries"]:
            st.session_state["analysis_query"] = st.session_state["recent_queries"][0]["code"]
    st.session_state.setdefault("analysis_alerts", [])
    st.session_state.setdefault("alerts_enabled", True)
    st.session_state.setdefault("price_alert_threshold", 3.0)
    st.session_state.setdefault("score_alert_threshold", 5.0)
    st.session_state.setdefault("current_holding_ratio_pct", 0.0)


def build_share_url(symbol: str) -> str:
    current_url = getattr(st.context, "url", "") or ""
    if not current_url:
        return f"?symbol={symbol}"
    parsed = urlsplit(current_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["symbol"] = symbol
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment))


def remember_query(security: Security, history) -> None:
    recent = [item for item in st.session_state["recent_queries"] if item["symbol"] != security.symbol]
    recent.insert(
        0,
        {
            "symbol": security.symbol,
            "code": security.code,
            "name": security.name,
            "as_of": str(history.as_of or "—"),
        },
    )
    st.session_state["recent_queries"] = recent[:10]
    get_store().save_recent_query(
        get_paper_account_id(),
        security,
        history.as_of,
    )


def record_change_alerts(security: Security, indicators, result) -> list[str]:
    symbol = security.symbol
    latest_price = indicators.latest.get("close")
    current_score = result.score
    current_date = str(indicators.as_of or "—")
    previous = st.session_state["analysis_snapshots"].get(symbol)
    alerts: list[str] = []
    if st.session_state["alerts_enabled"] and previous:
        try:
            price_change = float(latest_price) / float(previous["price"]) - 1
        except (TypeError, ValueError, ZeroDivisionError):
            price_change = None
        if price_change is not None and abs(price_change) * 100 >= st.session_state["price_alert_threshold"]:
            alerts.append(
                f"价格提醒：{security.name}自上次查询以来变动 {price_change:+.2%} "
                f"（{previous['as_of']} → {current_date}）。"
            )
        if current_score is not None and previous.get("score") is not None:
            score_change = float(current_score) - float(previous["score"])
            if abs(score_change) >= st.session_state["score_alert_threshold"]:
                alerts.append(
                    f"评分提醒：{security.name}短线评分变动 {score_change:+.1f} 分 "
                    f"（{previous['score']:.1f} → {current_score:.1f}）。"
                )
    snapshots = st.session_state["analysis_snapshots"]
    snapshots[symbol] = {
        "price": latest_price,
        "score": current_score,
        "as_of": current_date,
    }
    st.session_state["analysis_snapshots"] = dict(list(snapshots.items())[-30:])
    get_store().save_analysis_snapshot(
        get_paper_account_id(),
        symbol,
        latest_price,
        current_score,
        current_date,
    )
    return alerts


@st.cache_data(ttl="1h", max_entries=20)
def run_historical_backtest(
    frame: pd.DataFrame,
    code: str,
    name: str,
    exchange: str,
    market_status: str,
    status: str,
    message: str,
) -> BacktestReport:
    security = Security(code, name, exchange, market_status)
    snapshot = IndicatorSnapshot(security, frame, frame.iloc[-1].to_dict(), status, message)
    return backtest_buy_signals(snapshot)


@st.cache_data(ttl="1h", max_entries=20)
def run_optimized_backtest(
    frame: pd.DataFrame,
    code: str,
    name: str,
    exchange: str,
    market_status: str,
    status: str,
    message: str,
) -> BacktestReport:
    security = Security(code, name, exchange, market_status)
    snapshot = IndicatorSnapshot(security, frame, frame.iloc[-1].to_dict(), status, message)
    return backtest_strategy(snapshot, DEFAULT_STRATEGY, validation_mode="rolling")


@st.cache_data(ttl="30m", max_entries=20)
def load_money_flow_view(
    code: str, name: str, exchange: str, refresh_token: int = 0
) -> MoneyFlowHistory:
    """Load public estimated money flow without blocking the main analysis."""
    security = Security(code, name, exchange)
    service = StockDataService(create_provider("public"), get_store())
    start = date.today() - timedelta(days=365 * 3)
    return service.load_money_flow(security, start, date.today())


def format_number(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def format_percent(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    return f"{value * 100:.2f}%" if abs(value) < 2 else f"{value:.2f}%"


def format_financial_value(key: str, value) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(number):
        return "—"
    if key in {"roe", "revenue_growth", "profit_growth", "debt_ratio"}:
        return f"{number:.2f}%"
    return f"{number:.2f}"


def format_money_flow_amount(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:+.2f}亿元"
    if abs(value) >= 10_000:
        return f"{value / 10_000:+.2f}万元"
    return f"{value:+,.0f}元"


def format_money_flow_ratio(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):+.2f}%"


def build_money_flow_chart(frame: pd.DataFrame) -> alt.Chart:
    """Build a recent daily main-money net-flow bar chart."""
    chart_frame = frame.tail(180).copy()
    chart_frame["inflow_yi"] = chart_frame["main_net_inflow"] / 100_000_000
    chart_frame["direction"] = chart_frame["inflow_yi"] >= 0
    return (
        alt.Chart(chart_frame)
        .mark_bar()
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y-%m")),
            y=alt.Y("inflow_yi:Q", title="主力净流入（亿元）"),
            color=alt.condition("datum.direction", alt.value("#d94b4b"), alt.value("#16856a")),
            tooltip=[
                alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("inflow_yi:Q", title="主力净流入（亿元）", format="+.2f"),
                alt.Tooltip("main_net_ratio:Q", title="主力净流入占比（%）", format="+.2f"),
            ],
        )
        .properties(height=360)
        .interactive()
    )


def show_money_flow(security: Security) -> None:
    """Show provider-estimated daily main-money flow and transparent caveats."""
    st.subheader("每日主力资金")
    st.caption(
        "主力资金是公开数据服务商按大单/超大单成交估算的净流入，不是交易所公布的唯一真实主力账户数据；"
        "正数表示估算净流入，负数表示估算净流出。"
    )
    refresh_token = int(st.session_state.get("money_flow_refresh_token", 0))
    if st.button("刷新资金流数据", key=f"money_flow_refresh_{security.symbol}", icon=":material/refresh:"):
        st.session_state["money_flow_refresh_token"] = refresh_token + 1
        st.rerun()

    with st.spinner("正在获取每日主力资金数据…"):
        flow = load_money_flow_view(
            security.code, security.name, security.exchange, refresh_token
        )
    if flow.data.empty:
        st.warning(flow.message or "当前没有可用的公开主力资金数据。", icon=":material/data_alert:")
        st.caption("接口失败、限流或字段变化时不会填充虚构数据；请稍后点击“刷新资金流数据”重试。")
        return
    if flow.status == "stale-cache":
        st.warning(flow.message, icon=":material/cloud_off:")

    frame = flow.data.copy()
    latest = frame.iloc[-1]
    latest_flow = float(latest["main_net_inflow"])
    non_zero = frame[frame["main_net_inflow"] != 0]
    direction = 1 if latest_flow > 0 else -1 if latest_flow < 0 else 0
    streak = 0
    if direction:
        for value in reversed(non_zero["main_net_inflow"].tolist()):
            if (value > 0 and direction > 0) or (value < 0 and direction < 0):
                streak += 1
            else:
                break
    streak_text = "资金基本持平" if not direction else f"连续{streak}日净{"流入" if direction > 0 else "流出"}"
    last_5 = frame.tail(5)["main_net_inflow"].sum()
    last_20 = frame.tail(20)["main_net_inflow"].sum()

    metric_cols = st.columns(4)
    metric_cols[0].metric("最近交易日主力净流入", format_money_flow_amount(latest_flow))
    metric_cols[1].metric("主力净流入占比", format_money_flow_ratio(latest.get("main_net_ratio")))
    metric_cols[2].metric("近5日累计净流入", format_money_flow_amount(last_5))
    metric_cols[3].metric("连续资金方向", streak_text)

    if latest_flow > 0:
        st.success("最近交易日为公开估算的主力净流入，属于资金进场迹象；不能单独作为买入信号。", icon=":material/trending_up:")
    elif latest_flow < 0:
        st.warning("最近交易日为公开估算的主力净流出，属于资金离场迹象；不能单独作为卖出信号。", icon=":material/trending_down:")
    else:
        st.info("最近交易日主力资金估算接近持平，暂未形成明显进场或离场方向。", icon=":material/remove:")

    st.caption(
        f"数据源：{SOURCE_LABELS.get(flow.source, flow.source)}　资金流最新日期：{flow.as_of or '—'}　"
        f"近20日累计：{format_money_flow_amount(last_20)}"
    )
    st.altair_chart(build_money_flow_chart(frame), width="stretch")
    display_frame = frame.tail(30).sort_values("date", ascending=False).copy()
    display_frame["日期"] = display_frame["date"].dt.strftime("%Y-%m-%d")
    display_frame["主力净流入"] = display_frame["main_net_inflow"].map(format_money_flow_amount)
    display_frame["主力净流入占比"] = display_frame["main_net_ratio"].map(format_money_flow_ratio)
    display_frame["收盘价"] = display_frame["close"].map(format_number)
    display_frame["涨跌幅"] = display_frame["change"].map(format_money_flow_ratio)
    display_frame = display_frame[["日期", "主力净流入", "主力净流入占比", "收盘价", "涨跌幅"]]
    st.dataframe(display_frame, width="stretch", hide_index=True)
    st.info(
        "阅读提示：资金净流入只能说明当日估算的大单成交方向，可能受对倒、涨跌停、成交结构和数据商算法影响。"
        "应和价格趋势、成交量、财务指标及历史验证一起看，本页面不把资金流向直接转换成买卖结论。",
        icon=":material/menu_book:",
    )


def show_financial_metric_guide(financials: dict) -> None:
    st.subheader("指标说明（通俗版）")
    st.caption(
        "PE、PB主要看估值贵不贵；ROE看赚钱效率；营收和利润增速看成长；"
        "资产负债率看负债压力。以下解释用于帮助理解，不代表单项指标达到某个数值就一定该买或该卖。"
    )
    guide_cols = st.columns(2)
    for index, item in enumerate(FINANCIAL_METRIC_GUIDE):
        with guide_cols[index % 2]:
            with st.container(border=True):
                st.markdown(f"**{item['label']}**")
                st.write("当前值：", format_financial_value(item["key"], financials.get(item["key"])))
                st.write("它表示什么：", item["meaning"])
                st.write("一般怎么看：", item["reading"])
                st.caption(f"本应用评分参考：{item['scoring']}")
    st.info(
        "阅读建议：先看估值（PE/PB），再看盈利质量（ROE/资产负债率），"
        "最后看成长（营收增速/利润增速）。财务指标只参与波段和中长期评分，"
        "还会与技术趋势、动量和量价因素一起计算，不能单独替代完整财报分析。",
        icon=":material/menu_book:",
    )


def show_scoring_guide() -> None:
    with st.expander("评分说明", icon=":material/analytics:"):
        st.markdown(
            "评分范围为 **0–100分**，是技术面、量价、估值和财务指标按周期加权后的规则分数，"
            "不是收益率，也不是股票上涨的概率。"
        )
        st.markdown(
            "| 分数区间 | 页面信号 | 没有持仓时 | 已有持仓时 |\n"
            "|---|---|---|---|\n"
            "| 70–100 | 买入候选 | 可进一步研究 | 持有 |\n"
            "| 45–69 | 观望/持有 | 观望 | 持有 |\n"
            "| 0–44 | 减仓/卖出倾向 | 不宜买入 | 减仓或卖出倾向 |"
        )
        st.caption("以上是规则筛选结果，不代表确定的买卖结论；实际操作还需结合个人风险承受能力。")

        st.markdown("**先看懂两个规则倾向百分比**")
        st.markdown(
            "- **买入规则倾向**：等于综合评分，表示当前数据对‘没有持仓时考虑新建仓’的支持程度。"
            "例如综合评分 72 分，页面显示 72%，意思是规则支持度达到买入候选区间，不是有 72% 的上涨概率。\n"
            "- **卖出规则倾向**：等于 100 − 综合评分，表示当前数据对‘已有持仓时减仓或卖出’的相对倾向。"
            "例如综合评分 38 分，页面显示 62%，并且低于45分卖出阈值，因此规则偏向减仓/卖出。\n"
            "- **45–69.9 分的特殊情况**：买入倾向和卖出倾向都没有达到各自的明确触发条件，页面会显示观望/持有，"
            "不代表既要买入又要卖出。\n"
            "- 两个百分比只是把同一个 0–100 分规则结果换成容易阅读的方向展示，**不是上涨/下跌概率、历史胜率、准确率或建议仓位比例**。"
        )
        st.info(
            "阅读顺序：先看你属于‘没有持仓’还是‘已有持仓’，再看对应依据和阈值。"
            "买入候选只表示值得进一步研究，不表示立即买入；卖出倾向也不替代对成本价、仓位和风险承受能力的判断。",
            icon=":material/info:",
        )

        st.markdown("**综合评分是怎样算出来的**")
        st.markdown(
            "1. 先把每个评分维度换算成 **0–100 分**：50分代表中性，分数越高代表该维度越有利，分数越低代表越不利。\n"
            "2. 根据分析周期给每个维度分配权重。例如短线更重视趋势和动量，中长期更重视估值、盈利质量和成长。\n"
            "3. 将‘单项分数 × 对应权重’相加，得到综合评分。举例：某短线股票趋势80分、动量60分、量价50分、风险70分，"
            "综合评分 = 80×35% + 60×25% + 50×20% + 70×20% = 67分，因此属于观望，而不是买入候选。"
        )

        st.markdown("**单项评分的具体规则（当前版本）**")
        st.markdown(
            "- **趋势**：收盘价在20日均线上方加18分、下方扣18分；20日均线在60日均线上方加22分、下方扣22分；从50分起算。\n"
            "- **动量**：MACD柱为正加25分、为负扣25分；RSI在45–70加20分，超过80扣25分，低于30扣5分，其余加5分。\n"
            "- **量价**：上涨且成交量至少是20日均量的1.1倍加30分；下跌且放量扣25分；量比低于0.7扣5分；其他上涨加5分、下跌扣5分。\n"
            "- **风险**：从80分起算；年化波动率超过40%扣15分、超过60%再按更高风险扣35分；历史回撤低于−15%扣10分、低于−30%扣30分。\n"
            "- **估值**：PE分别按≤15、≤30、≤50、>50分为80、60、40、20分；PB分别按≤1.5、≤3、≤6、>6分为80、60、40、20分，取可用指标平均值。\n"
            "- **盈利质量**：ROE按≥15%、≥10%、≥5%、<5%分为80、65、45、25分；资产负债率按≤40%、≤60%、≤80%、>80%分为80、60、35、15分，取可用指标平均值。\n"
            "- **成长**：营收增速和利润增速分别按≥20%、≥10%、≥0%、≥−10%、<−10%分为85、70、55、35、15分，取可用指标平均值。\n"
            "- **长期趋势**：收盘价在60日均线上方加25分、下方扣25分；收盘价在120日均线上方加20分、下方扣20分；从50分起算。"
        )
        st.caption(
            "PE、PB等估值分档是本应用的简化筛选规则，应结合同行业和该股票自身历史水平比较；"
            "单一指标不能单独决定买卖。任一关键指标缺失、数据过期、停牌或有风险标记时，风险规则会覆盖评分，显示数据不足/不可判断。"
        )

        st.markdown("**不同分析周期的权重**")
        st.markdown(
            "| 分析周期 | 评分维度及权重 |\n"
            "|---|---|\n"
            "| 短线 | 趋势 35%、动量 25%、量价 20%、风险 20% |\n"
            "| 波段 | 趋势 25%、动量 20%、量价 15%、估值 20%、盈利质量 20% |\n"
            "| 中长期 | 估值 30%、盈利质量 30%、成长 20%、长期趋势 20% |"
        )

        st.markdown("**各评分维度看什么**")
        st.markdown(
            "- **趋势**：收盘价与20日、60日均线的位置和均线排列。\n"
            "- **动量**：MACD柱方向和RSI所处区间。\n"
            "- **量价**：涨跌方向与成交量相对20日均量的关系。\n"
            "- **风险**：年化波动率和历史回撤，风险越高得分越低。\n"
            "- **估值**：PE、PB分档，估值越低通常得分越高。\n"
            "- **盈利质量**：ROE和资产负债率。\n"
            "- **成长**：营收增速和利润增速。\n"
            "- **长期趋势**：收盘价与60日、120日均线的位置。"
        )

        st.markdown(
            "**信号明确度（原置信度）**：信号明确度表示规则信号的明确程度，不是赚钱概率。评分越偏离50分，"
            "信号明确度越高；当前规则最高为95%。关键指标缺失、数据过期、停牌或出现风险标记时，"
            "风险规则会覆盖评分，结果显示为**数据不足/不可判断**，信号明确度为0%。"
        )


def show_strategy_guide() -> None:
    with st.expander("优化策略说明：趋势、动量、量价与风险如何共同决定", expanded=False):
        st.markdown(
            "**趋势动量波段策略**借鉴公开量化研究中常见的规则组合，不复制任何商业软件的私有代码。"
            "它不是预测模型，而是把多个可观察条件组合成一个可回放的交易规则。"
        )
        st.markdown(
            "- **趋势过滤（35%）**：收盘价在20日均线上方、20日均线在60日均线上方，且60日均线近20日向上。\n"
            "- **动量确认（30%）**：20日和60日收益率为正，MACD柱为正且改善，RSI处于45–72适中区间。\n"
            "- **量价确认（15%）**：成交量至少达到20日均量；上涨放量时得分更高。\n"
            "- **波动风险（20%）**：年化波动率超过60%或当前回撤超过30%时，不允许新开仓。"
        )
        st.markdown(
            "**买入规则**：综合评分达到70分，同时趋势、动量、量价和风险四类过滤条件全部通过。\n"
            "**卖出规则**：入场后触及1.5×ATR止损、2.25×ATR止盈，或收盘跌破20日均线且MACD柱转负；"
            "最长持有20个交易日，到期退出。"
        )
        st.markdown(
            "**持仓比例联动规则**：买入候选时，评分70–84.9分的目标仓位约10%，评分85分及以上的目标仓位约15%，"
            "单只股票最高按25%控制；观望/持有时不主动加仓；减仓/卖出倾向时，评分35–44.9分按当前持仓减少25%，"
            "低于35分按当前持仓减少50%。"
        )
        st.caption(
            "回测默认按100万元资金、单笔仓位上限25%计算，手续费0.03%（最低5元）、卖出印花税0.10%、滑点0.10%，"
            "交易数量按100股整数倍。样本外统计使用历史后30%，不根据单只股票自动调参。"
        )


def _is_local_runtime() -> bool:
    """Use SQLite only for local runs without Supabase credentials.

    Supplying Streamlit secrets locally intentionally enables the same online
    auth and cloud-persistence path used by a deployed app.
    """
    current_url = getattr(st.context, "url", "") or ""
    host = (urlsplit(current_url).hostname or "").lower()
    is_local_host = not host or host in {"localhost", "127.0.0.1", "::1"}
    return is_local_host and get_supabase_config() is None


def get_paper_account_id() -> str:
    """Use the local account or the authenticated Supabase user UUID."""
    previous_account_id = st.session_state.get("paper_account_id")
    if _is_local_runtime():
        if previous_account_id and previous_account_id != LOCAL_ACCOUNT_ID:
            get_cache_store().migrate_account(previous_account_id, LOCAL_ACCOUNT_ID)
        st.session_state["paper_account_id"] = LOCAL_ACCOUNT_ID
        return LOCAL_ACCOUNT_ID
    user_id = str(st.session_state.get("auth_user_id", "")).strip()
    if not user_id:
        raise RuntimeError("Online user is not authenticated.")
    st.session_state["paper_account_id"] = user_id
    return user_id


def build_price_chart(frame: pd.DataFrame) -> alt.Chart:
    """Build a compact OHLC chart with moving-average overlays."""
    chart_frame = frame.tail(260).copy()
    chart_frame["direction"] = chart_frame["close"] >= chart_frame["open"]
    base = alt.Chart(chart_frame).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y-%m")),
        tooltip=[
            alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
            alt.Tooltip("open:Q", title="开盘", format=".2f"),
            alt.Tooltip("high:Q", title="最高", format=".2f"),
            alt.Tooltip("low:Q", title="最低", format=".2f"),
            alt.Tooltip("close:Q", title="收盘", format=".2f"),
        ],
    )
    wick = base.mark_rule().encode(
        y=alt.Y("low:Q", title="价格"),
        y2="high:Q",
        color=alt.condition("datum.direction", alt.value("#d94b4b"), alt.value("#16856a")),
    )
    body = base.mark_bar(size=6).encode(
        y="open:Q",
        y2="close:Q",
        color=alt.condition("datum.direction", alt.value("#d94b4b"), alt.value("#16856a")),
    )
    averages = (
        alt.Chart(chart_frame)
        .transform_fold(["sma5", "sma20", "sma60", "sma120"], as_=["均线", "价格"])
        .mark_line(strokeWidth=1.5)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("价格:Q", title="价格"),
            color=alt.Color(
                "均线:N",
                scale=alt.Scale(
                    domain=["sma5", "sma20", "sma60", "sma120"],
                    range=["#e08b2f", "#3867d6", "#8e44ad", "#273746"],
                ),
                legend=alt.Legend(title="均线"),
            ),
        )
    )
    return (wick + body + averages).properties(height=470).interactive()


def run_analysis(query: str, progress=None):
    def report(message: str) -> None:
        if callable(progress):
            progress(message)

    provider = create_provider("public")
    service = StockDataService(provider, get_store())
    report("\u6b63\u5728\u786e\u8ba4\u80a1\u7968\u4ee3\u7801\u548c\u540d\u79f0\u2026")
    security = service.resolve_security(query)
    start = date.today() - timedelta(days=365 * 3)
    report(f"\u6b63\u5728\u83b7\u53d6 {security.name}\uff08{security.code}\uff09\u8fd1\u4e09\u5e74\u771f\u5b9e\u65e5\u7ebf\u2026")
    history = service.load_market_data(security, start, date.today())
    if history.data.empty:
        raise RuntimeError(history.message or "没有获取到行情数据")
    indicators = calculate_indicators(history)
    report(f"\u5df2\u83b7\u53d6 {len(history.data)} \u6761\u884c\u60c5\uff0c\u6b63\u5728\u8ba1\u7b97\u6280\u672f\u6307\u6807\u2026")
    financials = service.load_financials(security)
    report("\u6b63\u5728\u751f\u6210\u7efc\u5408\u8bc4\u5206\u548c\u8d8b\u52bf\u52a8\u91cf\u7b56\u7565\u2026")
    results = evaluate_all_horizons(indicators, financials)
    return security, history, indicators, financials, results


BENCHMARK_SECURITY = Security("000300", "沪深300", "SSE")


@st.cache_data(ttl="30m", max_entries=20)
def load_benchmark_history(start_date: date, end_date: date) -> PriceHistory:
    """Load a broad market benchmark only when the market-comparison view opens."""
    service = StockDataService(create_provider("public"), get_store())
    return service.load_market_data(BENCHMARK_SECURITY, start_date, end_date)


def _period_return(values: pd.Series, periods: int) -> float | None:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if len(values) <= periods:
        return None
    return float(values.iloc[-1] / values.iloc[-periods - 1] - 1)


def build_relative_performance_frame(
    stock_history: PriceHistory, benchmark_history: PriceHistory
) -> pd.DataFrame:
    """Align a stock and CSI 300 on common dates and normalize both to 100."""
    if stock_history.data.empty or benchmark_history.data.empty:
        return pd.DataFrame()
    stock = stock_history.data[["date", "close"]].copy()
    benchmark = benchmark_history.data[["date", "close"]].copy()
    stock["date"] = pd.to_datetime(stock["date"], errors="coerce")
    benchmark["date"] = pd.to_datetime(benchmark["date"], errors="coerce")
    stock["个股收盘"] = pd.to_numeric(stock["close"], errors="coerce")
    benchmark["沪深300收盘"] = pd.to_numeric(benchmark["close"], errors="coerce")
    frame = pd.merge(
        stock[["date", "个股收盘"]],
        benchmark[["date", "沪深300收盘"]],
        on="date",
        how="inner",
    ).dropna()
    if frame.empty:
        return frame
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame["个股指数"] = frame["个股收盘"] / frame["个股收盘"].iloc[0] * 100
    frame["沪深300指数"] = frame["沪深300收盘"] / frame["沪深300收盘"].iloc[0] * 100
    return frame[["date", "个股指数", "沪深300指数"]]


def show_market_comparison(security: Security, history: PriceHistory) -> None:
    """Show market context without silently using it as a buy/sell signal."""
    st.subheader("市场环境与沪深300对照")
    st.caption(
        "个股涨跌需要放在大盘环境中理解。这里使用同一日期区间的真实公开沪深300日线，"
        "只做相对表现对照，不会自动改变当前策略评分。"
    )
    if history.data.empty or "date" not in history.data:
        st.warning("当前没有足够的个股行情，暂时无法进行市场对照。", icon=":material/data_alert:")
        return
    start_date = pd.Timestamp(history.data["date"].min()).date()
    end_date = pd.Timestamp(history.data["date"].max()).date()
    with st.spinner("正在获取沪深300公开行情…"):
        benchmark = load_benchmark_history(start_date, end_date)
    if benchmark.data.empty:
        st.warning(
            benchmark.message or "沪深300公开行情暂时不可用，无法完成市场对照。",
            icon=":material/data_alert:",
        )
        return
    frame = build_relative_performance_frame(history, benchmark)
    if len(frame) < 21:
        st.warning("个股与沪深300的共同交易日不足21天，暂不显示相对表现。", icon=":material/data_alert:")
        return

    stock_returns = pd.to_numeric(frame["个股指数"], errors="coerce")
    benchmark_returns = pd.to_numeric(frame["沪深300指数"], errors="coerce")
    stock_return_20 = _period_return(stock_returns, 20)
    benchmark_return_20 = _period_return(benchmark_returns, 20)
    stock_return_60 = _period_return(stock_returns, 60)
    benchmark_return_60 = _period_return(benchmark_returns, 60)
    relative_20 = None if stock_return_20 is None or benchmark_return_20 is None else stock_return_20 - benchmark_return_20
    relative_60 = None if stock_return_60 is None or benchmark_return_60 is None else stock_return_60 - benchmark_return_60

    metric_cols = st.columns(4)
    metric_cols[0].metric("个股近20日", format_percent(stock_return_20))
    metric_cols[1].metric("沪深300近20日", format_percent(benchmark_return_20))
    metric_cols[2].metric("个股相对强弱（20日）", format_percent(relative_20))
    metric_cols[3].metric("个股相对强弱（60日）", format_percent(relative_60))

    if relative_20 is not None and relative_20 > 0:
        st.success(f"{security.name}近20日跑赢沪深300约{relative_20:.2%}，相对表现偏强。", icon=":material/trending_up:")
    elif relative_20 is not None and relative_20 < 0:
        st.warning(f"{security.name}近20日跑输沪深300约{abs(relative_20):.2%}，个股上涨可能弱于市场。", icon=":material/trending_down:")
    else:
        st.info("近20日个股与沪深300表现接近，暂未形成明显相对强弱。", icon=":material/remove:")

    chart_frame = frame.tail(260).copy()
    long_frame = chart_frame.melt(
        id_vars=["date"],
        value_vars=["个股指数", "沪深300指数"],
        var_name="序列",
        value_name="基准化指数",
    )
    chart = (
        alt.Chart(long_frame)
        .mark_line()
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y-%m")),
            y=alt.Y("基准化指数:Q", title="基准化指数（共同起点=100）"),
            color=alt.Color("序列:N", title=None),
            tooltip=[
                alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("序列:N", title="序列"),
                alt.Tooltip("基准化指数:Q", title="指数", format=".2f"),
            ],
        )
        .properties(height=300)
        .interactive()
    )
    st.altair_chart(chart, width="stretch")
    st.caption(
        f"个股数据日期：{history.as_of or '—'}；沪深300数据日期：{benchmark.as_of or '—'}；"
        "相对强弱不是买卖信号，也不代表个股未来一定跑赢或跑输。"
    )


def summarize_result(result) -> str:
    if result.score is None:
        return "关键数据不足，当前周期无法形成可靠判断。"
    if result.score >= 70:
        return "规则信号偏强，适合继续研究；“买入候选”不等于立即买入。"
    if result.score >= 45:
        return "规则信号中性，建议等待趋势或财务数据进一步确认。"
    return "规则信号偏弱，不宜仅凭当前结果新建仓；已有持仓应关注回撤风险。"


def show_action_guidance(result) -> None:
    """Translate the score into separate new-position and existing-position actions."""
    with st.container(border=True):
        st.markdown("**明确操作结论（按当前规则）**")
        if result.score is None:
            st.markdown("**没有持仓：** 暂不建议买入。")
            st.markdown("**已有持仓：** 当前数据不足，不能仅凭本结果确认卖出；请先核查数据和个股风险。")
            st.caption(
                "买入/卖出百分比：暂无法计算。数据不足时不生成方向性百分比，避免把不完整数据误解成建议。"
            )
            return

        score = float(result.score)
        buy_support = score
        sell_tendency = 100.0 - score
        if score >= 70:
            new_position = "可以考虑买入候选，建议结合个人风险承受能力分批研究。"
            existing_position = "规则上更支持持有，暂不建议仅凭本评分卖出。"
            buy_basis = f"综合评分 {score:.1f} 分达到买入候选阈值（≥70分）。"
            sell_basis = f"综合评分未低于减仓/卖出阈值（<45分），当前规则未触发卖出。"
        elif score >= 45:
            new_position = "不建议现在买入，先观望，等待评分达到70分或指标进一步确认。"
            existing_position = "暂不建议仅凭本评分卖出，继续观察趋势、估值和风险变化。"
            buy_basis = f"综合评分 {score:.1f} 分未达到买入候选阈值（≥70分）。"
            sell_basis = f"综合评分 {score:.1f} 分仍在观望/持有区间（45–69.9分），未触发减仓/卖出阈值。"
        else:
            new_position = "不建议买入。"
            existing_position = "可考虑减仓/卖出，但仍应结合个人成本、仓位和风险承受能力。"
            buy_basis = f"综合评分 {score:.1f} 分低于买入候选区间，且低于45分的弱势阈值。"
            sell_basis = f"综合评分 {score:.1f} 分低于减仓/卖出阈值（<45分）。"

        st.markdown(f"**没有持仓：** {new_position}")
        st.markdown(f"**已有持仓：** {existing_position}")
        percent_cols = st.columns(2)
        percent_cols[0].metric("买入规则倾向", f"{buy_support:.1f}%")
        percent_cols[1].metric("卖出规则倾向", f"{sell_tendency:.1f}%")
        st.caption(
            "计算方式：买入规则倾向 = 综合评分；卖出规则倾向 = 100 − 综合评分。"
            "这两个数是同一套规则的方向性展示，不是上涨/下跌概率、准确率或建议仓位比例。"
        )
        basis_cols = st.columns(2)
        with basis_cols[0]:
            st.markdown(f"**买入依据：** {buy_basis}")
        with basis_cols[1]:
            st.markdown(f"**卖出依据：** {sell_basis}")


def show_position_guidance(result) -> None:
    """Show conservative, rule-based position sizing bands."""
    with st.container(border=True):
        st.markdown("**仓位比例建议（按当前规则）**")
        if result.score is None:
            st.markdown("**建议新增仓位：** 无法计算（数据不足）")
            st.markdown("**建议减仓比例：** 无法计算（数据不足）")
            st.caption(
                "分母说明：新增仓位以总资产为分母，减仓比例以当前持仓数量为分母。"
                "数据不完整时不提供仓位比例。"
            )
            return

        score = float(result.score)
        if score >= 80:
            buy_ratio, sell_ratio = 30, 0
            buy_basis = "评分达到80分以上，进入较强规则区间；新增仓位上限按总资产的30%控制。"
            sell_basis = "当前评分未触发减仓/卖出阈值，不建议仅凭本规则减仓。"
        elif score >= 70:
            buy_ratio, sell_ratio = 20, 0
            buy_basis = "评分达到70分买入候选阈值，但未达到80分；新增仓位上限按总资产的20%控制。"
            sell_basis = "当前评分未触发减仓/卖出阈值，不建议仅凭本规则减仓。"
        elif score >= 45:
            buy_ratio, sell_ratio = 0, 0
            buy_basis = f"评分为{score:.1f}分，低于70分买入阈值；建议新增仓位为0%，先观望。"
            sell_basis = f"评分仍在45–69.9分观望/持有区间，建议减仓比例为0%。"
        elif score >= 35:
            buy_ratio, sell_ratio = 0, 25
            buy_basis = f"评分为{score:.1f}分，低于45分弱势阈值，不建议新增仓位。"
            sell_basis = "评分处于35–44.9分，若已有持仓，可按当前持仓数量考虑减仓25%。"
        else:
            buy_ratio, sell_ratio = 0, 50
            buy_basis = f"评分为{score:.1f}分，明显低于45分弱势阈值，不建议新增仓位。"
            sell_basis = "评分低于35分，若已有持仓，可按当前持仓数量考虑减仓50%。"

        ratio_cols = st.columns(2)
        ratio_cols[0].metric("建议新增仓位上限", f"{buy_ratio}%")
        ratio_cols[1].metric("建议减仓比例", f"{sell_ratio}%")
        st.caption(
            "仓位分档是风险控制上限，不是必须执行的目标；新增仓位最高30%，单次减仓最高50%，"
            "不建议满仓或机械清仓。"
        )
        basis_cols = st.columns(2)
        with basis_cols[0]:
            st.markdown(f"**买入仓位依据：** {buy_basis}")
        with basis_cols[1]:
            st.markdown(f"**卖出仓位依据：** {sell_basis}")
        st.caption(
            "以上比例只由当前规则评分分档得到，未考虑你的成本价、总资产、集中度、流动性和个人风险承受能力；"
            "不是个性化投资建议。"
        )


def show_horizon_overview(results: dict) -> None:
    st.subheader("三周期信号总览")
    overview = pd.DataFrame(
        [
            {
                "周期": result.horizon,
                "信号": result.signal,
                "评分": "—" if result.score is None else f"{result.score:.1f}",
                "信号明确度": f"{result.confidence:.1f}%",
                "数据状态": "可用" if result.data_status == "ok" else "数据不足",
            }
            for result in results.values()
        ]
    )
    st.dataframe(overview, width="stretch", hide_index=True)
    valid_results = [result for result in results.values() if result.score is not None]
    if not valid_results:
        st.caption("三个周期都缺少可用于评分的完整数据。")
    elif len({result.signal for result in valid_results}) > 1:
        st.info(
            "不同周期使用的指标和权重不同，信号不一致时，应优先参考与你实际持有周期对应的结论。",
            icon=":material/tune:",
        )


def build_score_basis_frame(result) -> pd.DataFrame:
    """Return the auditable inputs behind one horizon's composite score."""
    rows = []
    for item in result.components:
        score = None if item.score is None else float(item.score)
        weight = float(item.weight)
        rows.append(
            {
                "评分维度": item.name,
                "原始分数": "—" if score is None else f"{score:.1f}",
                "权重": f"{weight:.0%}",
                "加权得分": "—" if score is None else f"{score * weight:.1f}",
                "相对中性影响": "—" if score is None else f"{(score - 50) * weight:+.1f}",
                "实际判断依据": item.detail or "缺少可展示的指标说明",
            }
        )
    return pd.DataFrame(rows)


def show_score_basis(result) -> None:
    """Explain how the displayed signal follows from measured indicators."""
    with st.container(border=True):
        st.markdown("**结论依据：综合评分如何得到**")
        st.caption(
            "综合评分 = Σ（各评分维度的原始分数 × 对应权重）。"
            "“相对中性影响” =（原始分数 − 50）× 权重；正数拉高总分，负数拉低总分。"
        )
        st.caption(
            "信号阈值：70分及以上为买入候选，45–69.9分为观望/持有，低于45分为减仓/卖出倾向。"
        )
        st.dataframe(build_score_basis_frame(result), width="stretch", hide_index=True)
        if result.score is None:
            reasons = "；".join(result.warnings) or "评分所需数据不完整"
            st.warning(
                f"本周期未形成综合评分，不能据此生成买卖倾向。原因：{reasons}。"
                "表格中的单项分数仅表示已取得数据的检查结果。",
                icon=":material/data_alert:",
            )
        else:
            st.caption(
                f"加权合计 = {result.score:.1f}分。信号明确度为规则信号偏离中性50分的程度，"
                "不是赚钱概率、准确率或未来收益保证。"
            )


def show_result(result) -> None:
    status = "正常" if result.data_status == "ok" else "需要检查数据"
    st.subheader(f"{result.horizon}：{result.signal}")
    metric_cols = st.columns(4)
    metric_cols[0].metric("综合评分", "—" if result.score is None else f"{result.score:.1f} / 100")
    metric_cols[1].metric("信号明确度", f"{result.confidence:.1f}%")
    metric_cols[2].metric("数据日期", str(result.as_of or "—"))
    metric_cols[3].metric("数据状态", status)

    st.info(summarize_result(result), icon=":material/lightbulb:")
    show_action_guidance(result)
    show_position_guidance(result)

    if result.warnings:
        for warning in result.warnings:
            st.warning(warning)

    show_score_basis(result)

    if result.score is not None:
        positive = [item for item in result.components if item.score is not None and item.score >= 60]
        negative = [item for item in result.components if item.score is not None and item.score <= 40]
        reason_cols = st.columns(2)
        with reason_cols[0]:
            st.markdown("**主要支持因素**")
            if positive:
                for item in positive[:3]:
                    st.write(f"- {item.name}（{item.score:.0f}分）：{item.detail}")
            else:
                st.caption("当前没有明显的加分维度。")
        with reason_cols[1]:
            st.markdown("**需要关注的因素**")
            if negative:
                for item in negative[:3]:
                    st.write(f"- {item.name}（{item.score:.0f}分）：{item.detail}")
            else:
                st.caption("当前没有明显的低分维度；仍需关注上方风险提示。")

    component_frame = pd.DataFrame(
        [
            {
                "维度": item.name,
                "权重": f"{item.weight:.0%}",
                "分数": "—" if item.score is None else item.score,
                "说明": item.detail,
            }
            for item in result.components
        ]
    )
    st.dataframe(component_frame, width="stretch", hide_index=True)


def format_ratio(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    value = float(value)
    return "∞" if math.isinf(value) else f"{value:.2f}"


def show_strategy_result(result) -> None:
    """Show the optimized strategy before the legacy composite-score results."""
    with st.container(border=True):
        st.subheader(f"{result.strategy_name}：{result.signal}")
        metrics = st.columns(4)
        metrics[0].metric("策略评分", "—" if result.score is None else f"{result.score:.1f} / 100")
        metrics[1].metric("信号明确度", f"{result.confidence:.1f}%")
        metrics[2].metric("趋势/条件通过", f"{result.key_metrics.get('趋势确认数', '—')} / 4")
        metrics[3].metric("ATR14", format_number(result.key_metrics.get("ATR14")))

        if result.score is None:
            st.warning("优化策略当前无法判断，缺少有效数据或数据状态不满足要求。", icon=":material/data_alert:")
        elif result.signal == "买入候选":
            st.success("优化策略的全部买入过滤条件已通过；这只是研究候选，不代表立即买入。", icon=":material/trending_up:")
        elif result.signal == "减仓/卖出倾向":
            st.warning("优化策略偏弱，已有持仓需要重点检查退出条件；没有持仓时不建议新开仓。", icon=":material/trending_down:")
        else:
            st.info("优化策略尚未形成完整买入确认，先等待趋势、动量或量价条件改善。", icon=":material/hourglass_top:")

        action_cols = st.columns(2)
        with action_cols[0]:
            st.markdown("**没有持仓时**")
            st.write("可以研究买入候选" if result.signal == "买入候选" else "暂不建议新开仓")
            st.caption("只有在趋势、动量、量价、风险和评分同时满足时才进入候选区间。")
        with action_cols[1]:
            st.markdown("**已有持仓时**")
            st.write("继续观察持仓" if result.signal != "减仓/卖出倾向" else "检查减仓或退出条件")
            st.caption("卖出依据以止损、止盈、趋势转弱和最长持有期为准。")

        position_cols = st.columns(2)
        position_cols[0].metric(
            "建议新增仓位上限",
            "25%" if result.signal == "买入候选" else "0%",
        )
        position_cols[1].metric(
            "ATR止损 / 止盈参考",
            f"{format_number(result.key_metrics.get('止损参考价'))} / {format_number(result.key_metrics.get('止盈参考价'))}",
        )
        st.caption("仓位上限和ATR价格只是固定规则的风险控制参考，不是个性化投资建议。")

        reason_cols = st.columns(2)
        with reason_cols[0]:
            st.markdown("**为什么买入或未买入**")
            for condition in result.entry_conditions:
                st.write(condition)
        with reason_cols[1]:
            st.markdown("**为什么卖出或继续持有**")
            for condition in result.exit_conditions:
                st.write(condition)
        st.markdown("**风险控制**")
        for control in result.risk_controls:
            st.write(control)
        for warning in result.warnings:
            st.warning(warning, icon=":material/warning:")

        component_frame = pd.DataFrame(
            [
                {
                    "策略维度": item.name,
                    "权重": f"{item.weight:.0%}",
                    "分数": "—" if item.score is None else item.score,
                    "实际判断依据": item.detail,
                }
                for item in result.components
            ]
        )
        st.dataframe(component_frame, width="stretch", hide_index=True)


def show_holding_ratio_guidance(result) -> None:
    """Show how the current holding ratio changes the fixed strategy action."""
    with st.container(border=True):
        st.markdown("**结合已有持仓比例的操作倾向**")
        st.caption(
            "请输入这只股票当前占你总资产的比例。应用会把它与当前策略的目标仓位和上限比较；"
            "建议卖出比例会同时按总资产和当前持仓两种口径显示。"
        )
        current_pct = st.number_input(
            "当前该股持仓比例（占总资产）",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="current_holding_ratio_pct",
        )
        guidance = recommend_position_action(result, float(current_pct) / 100)
        st.markdown(f"**结合持仓后的结论：** {guidance.action}")

        if guidance.suggested_buy_ratio is None:
            st.caption("买入和卖出比例：暂无法计算。数据不足时不生成仓位方向，避免误导。")
        else:
            metric_cols = st.columns(4)
            metric_cols[0].metric("当前持仓比例", f"{guidance.current_ratio:.1%}")
            metric_cols[1].metric("建议新增仓位", f"{guidance.suggested_buy_ratio:.1%}")
            metric_cols[2].metric("建议卖出占总资产", f"{guidance.suggested_sell_ratio_total:.1%}")
            metric_cols[3].metric("建议卖出当前持仓", f"{guidance.suggested_sell_ratio_current:.1%}")
            st.caption(
                f"规则目标仓位：{'—' if guidance.target_ratio is None else f'{guidance.target_ratio:.0%}'}；"
                f"规则最高仓位：{'—' if guidance.max_ratio is None else f'{guidance.max_ratio:.0%}'}。"
            )

        basis_cols = st.columns(2)
        with basis_cols[0]:
            st.markdown("**仓位计算依据**")
            for basis in guidance.basis:
                st.write(f"- {basis}")
        with basis_cols[1]:
            st.markdown("**使用限制**")
            for warning in guidance.warnings:
                st.write(f"- {warning}")


def show_trade_plan(security: Security, strategy_result, latest: dict) -> None:
    """Collect a pre-trade plan and calculate a conservative position size."""
    current_price = latest.get("close")
    try:
        current_price = float(current_price)
    except (TypeError, ValueError):
        current_price = 0.0
    if current_price <= 0:
        return

    stop_default = strategy_result.key_metrics.get("止损参考价") or current_price * 0.95
    target_default = strategy_result.key_metrics.get("止盈参考价") or current_price * 1.10
    if stop_default >= current_price:
        stop_default = current_price * 0.95
    if target_default <= current_price:
        target_default = current_price * 1.10
    default_direction = "买入计划" if strategy_result.signal == "买入候选" else "观察计划"
    default_thesis = (
        f"{strategy_result.strategy_name}：{strategy_result.signal}，评分"
        f"{strategy_result.score:.1f}分。"
        if strategy_result.score is not None
        else "当前数据不足，先记录观察计划。"
    )

    with st.container(border=True):
        st.markdown("**交易计划卡（先计划，再下单）**")
        st.caption(
            "这张卡不预测收益，也不会自动下单。它要求你先写清买入理由、止损位置和最大风险，"
            "再决定是否创建模拟订单。"
        )
        failed_conditions = [
            condition for condition in strategy_result.entry_conditions
            if condition.startswith("未通过")
        ]
        if failed_conditions:
            st.warning(
                "当前不满足新开仓条件，主要拦截项：" + "；".join(failed_conditions),
                icon=":material/block:",
            )
        elif strategy_result.signal == "买入候选":
            st.success("当前策略条件全部通过，但仍需按交易计划控制仓位和最大亏损。", icon=":material/check_circle:")

        with st.form(f"trade_plan_{security.symbol}", border=False):
            first = st.columns(3)
            direction = first[0].selectbox(
                "计划类型", ["买入计划", "观察计划"],
                index=["买入计划", "观察计划"].index(default_direction),
            )
            setup = first[1].selectbox("交易场景", ["趋势突破", "回踩确认", "估值观察", "其他"])
            horizon = first[2].selectbox("计划周期", ["5–20个交易日", "1–3个月", "长期观察"])

            prices = st.columns(3)
            entry_price = prices[0].number_input(
                "计划买入价", min_value=0.01, value=round(current_price, 2), step=0.01,
                key=f"plan_entry_{security.symbol}",
            )
            stop_loss = prices[1].number_input(
                "止损价", min_value=0.01, value=round(float(stop_default), 2), step=0.01,
                key=f"plan_stop_{security.symbol}",
            )
            take_profit = prices[2].number_input(
                "止盈价", min_value=0.01, value=round(float(target_default), 2), step=0.01,
                key=f"plan_target_{security.symbol}",
            )

            capital_cols = st.columns(3)
            total_capital = capital_cols[0].number_input(
                "计划总资金（元）", min_value=100.0, value=100_000.0, step=10_000.0,
                key=f"plan_capital_{security.symbol}",
            )
            risk_pct = capital_cols[1].number_input(
                "单笔最多亏损（%）", min_value=0.1, max_value=10.0, value=1.0, step=0.1,
                key=f"plan_risk_{security.symbol}",
            )
            max_position_pct = capital_cols[2].number_input(
                "单只股票仓位上限（%）", min_value=1.0, max_value=100.0, value=25.0, step=1.0,
                key=f"plan_max_position_{security.symbol}",
            )

            thesis = st.text_area(
                "我的买入/观察理由",
                value=default_thesis,
                key=f"plan_thesis_{security.symbol}",
            )
            invalidation = st.text_area(
                "什么情况说明判断错误？",
                value="收盘跌破止损价，或趋势跌破20日均线且MACD柱转负。",
                key=f"plan_invalidation_{security.symbol}",
            )
            save_plan = st.form_submit_button(
                "计算并保存交易计划", type="primary", icon=":material/save:"
            )

        sizing = calculate_position_size(
            float(entry_price), float(stop_loss), float(take_profit), float(total_capital),
            float(risk_pct) / 100, float(max_position_pct) / 100,
        )
        st.markdown("**仓位与风险估算**")
        metric_cols = st.columns(5)
        metric_cols[0].metric("风险预算", f"¥{sizing.risk_budget:,.0f}")
        metric_cols[1].metric("建议股数", f"{sizing.suggested_shares:,} 股")
        metric_cols[2].metric("计划投入", f"¥{sizing.planned_amount:,.0f}")
        metric_cols[3].metric("预计最大亏损", f"¥{sizing.estimated_max_loss:,.0f}")
        metric_cols[4].metric("预期盈亏比", "—" if sizing.risk_reward is None else f"{sizing.risk_reward:.2f}")
        st.caption(
            "建议股数取‘风险预算限制’和‘仓位上限限制’中较小者，并按100股整数倍向下取整；"
            "实际成交可能受价格跳空、滑点和流动性影响。"
        )
        for warning in sizing.warnings:
            st.warning(warning, icon=":material/warning:")

        if save_plan:
            if not sizing.valid:
                st.error("交易计划参数不完整，暂不能保存。")
            elif not thesis.strip() or not invalidation.strip():
                st.error("请填写买入/观察理由，以及判断错误的条件。")
            else:
                plan = TradePlan(
                    id=None,
                    account_id=get_paper_account_id(),
                    symbol=security.symbol,
                    direction=direction,
                    setup=setup,
                    horizon=horizon,
                    entry_price=float(entry_price),
                    stop_loss=float(stop_loss),
                    take_profit=float(take_profit),
                    total_capital=float(total_capital),
                    risk_pct=float(risk_pct) / 100,
                    max_position_pct=float(max_position_pct) / 100,
                    planned_shares=sizing.suggested_shares,
                    planned_amount=sizing.planned_amount,
                    risk_budget=sizing.risk_budget,
                    estimated_max_loss=sizing.estimated_max_loss,
                    risk_reward=sizing.risk_reward,
                    thesis=thesis.strip(),
                    invalidation=invalidation.strip(),
                )
                plan_id = get_store().save_trade_plan(plan)
                st.success(f"交易计划已保存（编号 {plan_id}）。确认计划后，再到‘模拟交易’页提交订单。", icon=":material/task_alt:")


def show_trade_reviews(security: Security) -> None:
    """Record execution quality and mistakes after a simulated trade."""
    store = get_store()
    account_id = get_paper_account_id()
    plans = store.load_trade_plans(account_id, security.symbol, limit=20)
    plan_labels = ["不关联交易计划"]
    plan_by_label: dict[str, int | None] = {plan_labels[0]: None}
    for plan in plans:
        label = f"#{plan.id} · {plan.created_at or '未知时间'} · {plan.direction} · {plan.planned_shares}股"
        plan_labels.append(label)
        plan_by_label[label] = plan.id

    with st.container(border=True):
        st.markdown("**交易后复盘**")
        st.caption("复盘不是给自己打分，而是区分‘策略判断错误’和‘执行没有按计划进行’。")
        with st.form(f"trade_review_{security.symbol}", border=False):
            review_cols = st.columns(3)
            selected_plan = review_cols[0].selectbox("关联计划", plan_labels)
            review_date = review_cols[1].date_input("复盘日期", value=date.today())
            outcome = review_cols[2].selectbox("当前结果", ["执行中", "盈利", "亏损", "持平", "取消计划"])
            adherence = st.slider("计划执行度（%）", min_value=0, max_value=100, value=100, step=5)
            mistake_tags = st.multiselect(
                "本次是否出现以下情况？",
                ["追涨", "恐慌卖出", "仓位过重", "没有止损", "过早卖出", "信号不足仍然买入", "按计划执行"],
            )
            notes = st.text_area("复盘笔记", placeholder="记录当时的想法、实际执行和下一次改进办法。")
            save_review = st.form_submit_button("保存复盘", icon=":material/edit_note:")
        if save_review:
            review = TradeReview(
                id=None,
                account_id=account_id,
                symbol=security.symbol,
                plan_id=plan_by_label[selected_plan],
                review_date=review_date.isoformat(),
                outcome=outcome,
                execution_adherence=int(adherence),
                mistake_tags=tuple(mistake_tags),
                notes=notes.strip(),
            )
            review_id = store.save_trade_review(review)
            st.success(f"复盘已保存（编号 {review_id}）。", icon=":material/task_alt:")

        reviews = store.load_trade_reviews(account_id, security.symbol, limit=50)
        if not reviews:
            st.info("这只股票还没有复盘记录。")
            return
        avg_adherence = sum(item.execution_adherence for item in reviews) / len(reviews)
        summary_cols = st.columns(3)
        summary_cols[0].metric("复盘次数", len(reviews))
        summary_cols[1].metric("平均计划执行度", f"{avg_adherence:.0f}%")
        summary_cols[2].metric("出现错误标签的次数", sum(bool(item.mistake_tags) for item in reviews))
        review_frame = pd.DataFrame(
            [
                {
                    "复盘日期": item.review_date,
                    "结果": item.outcome,
                    "计划执行度": f"{item.execution_adherence}%",
                    "问题标签": "、".join(item.mistake_tags) or "无",
                    "笔记": item.notes,
                }
                for item in reviews
            ]
        )
        st.dataframe(review_frame, width="stretch", hide_index=True)


def _report_comparison_frame(reports: list[BacktestReport]) -> pd.DataFrame:
    rows = [
        ("历史信号数", lambda report: str(report.signal_count)),
        ("5日毛收益胜率（信号后）", lambda report: format_percent(report.win_rate_5d)),
        ("20日毛收益胜率（信号后）", lambda report: format_percent(report.win_rate_20d)),
        ("5日平均毛收益", lambda report: format_percent(report.avg_return_5d)),
        ("20日平均毛收益", lambda report: format_percent(report.avg_return_20d)),
        ("实际净收益胜率", lambda report: format_percent(report.win_rate_actual)),
        ("实际平均净收益", lambda report: format_percent(report.avg_net_return)),
        ("平均盈利", lambda report: format_percent(report.avg_win_actual)),
        ("平均亏损", lambda report: format_percent(report.avg_loss_actual)),
        ("盈亏比", lambda report: format_ratio(report.profit_factor)),
        ("累计净收益（顺序交易）", lambda report: format_percent(report.total_net_return)),
        ("单笔20日内最大回撤", lambda report: format_percent(report.max_drawdown_20d)),
        ("权益曲线最大回撤", lambda report: format_percent(report.max_drawdown_equity)),
        ("交易成本合计（按100万元资金、25%仓位上限）", lambda report: format_number(report.total_costs)),
        ("样本外信号数", lambda report: str(report.oos_signal_count)),
        ("样本外净收益胜率", lambda report: format_percent(report.oos_win_rate)),
        ("样本外平均净收益", lambda report: format_percent(report.oos_avg_net_return)),
        ("样本外权益最大回撤", lambda report: format_percent(report.oos_max_drawdown)),
    ]
    return pd.DataFrame(
        [
            {"指标": label, **{report.strategy_name: formatter(report) for report in reports}}
            for label, formatter in rows
        ]
    )


def show_backtest_records(report: BacktestReport, title: str) -> None:
    if report.signal_count == 0:
        st.info(f"{title}暂无可统计信号。", icon=":material/info:")
        return
    display_frame = report.signals.rename(
        columns={
            "signal_date": "信号日期",
            "entry_date": "次日入场日期",
            "score": "策略评分",
            "entry_price": "入场价格",
            "shares": "数量",
            "exit_date": "退出日期",
            "exit_price": "退出价格",
            "exit_reason": "退出原因",
            "holding_days": "持有交易日",
            "gross_return": "实际毛收益",
            "net_return": "实际净收益",
            "return_5d": "信号后5日收益",
            "return_20d": "信号后20日收益",
            "max_drawdown_20d": "20日内最大回撤",
        }
    ).copy()
    columns = [
        "信号日期",
        "次日入场日期",
        "策略评分",
        "入场价格",
        "数量",
        "退出日期",
        "退出价格",
        "退出原因",
        "持有交易日",
        "实际毛收益",
        "实际净收益",
        "信号后5日收益",
        "信号后20日收益",
        "20日内最大回撤",
    ]
    display_frame = display_frame[[column for column in columns if column in display_frame]]
    if "策略评分" in display_frame:
        display_frame["策略评分"] = display_frame["策略评分"].map(lambda value: f"{value:.1f}")
    for column in ("入场价格", "退出价格"):
        if column in display_frame:
            display_frame[column] = display_frame[column].map(format_number)
    for column in ("实际毛收益", "实际净收益", "信号后5日收益", "信号后20日收益", "20日内最大回撤"):
        if column in display_frame:
            display_frame[column] = display_frame[column].map(format_percent)
    if len(display_frame) > 100:
        st.caption("下表显示最近100个信号，顶部统计使用全部历史信号。")
        display_frame = display_frame.tail(100)
    st.dataframe(display_frame, width="stretch", hide_index=True)


def show_historical_validation(indicators) -> None:
    st.subheader("历史验证：原规则 vs 优化策略")
    st.caption(
        "两套规则使用同一段真实行情。信号日收盘后，下一交易日开盘入场；5日和20日指标是信号后的固定观察窗口。"
        "优化策略的实际净收益按止损、止盈、趋势退出或最长持有期回放。"
    )
    arguments = (
        indicators.frame,
        indicators.security.code,
        indicators.security.name,
        indicators.security.exchange,
        indicators.security.market_status,
        indicators.status,
        indicators.message,
    )
    base_report = run_historical_backtest(*arguments)
    optimized_report = run_optimized_backtest(*arguments)
    for report in (base_report, optimized_report):
        if report.message:
            st.warning(f"{report.strategy_name}：{report.message}", icon=":material/warning:")
    st.dataframe(
        _report_comparison_frame([base_report, optimized_report]),
        width="stretch",
        hide_index=True,
    )
    curve_frames = []
    for report in (base_report, optimized_report):
        if report.signal_count == 0 or "exit_date" not in report.signals or "net_return" not in report.signals:
            continue
        curve = report.signals[["exit_date", "net_return"]].copy()
        curve["日期"] = pd.to_datetime(curve["exit_date"], errors="coerce")
        curve["净收益"] = pd.to_numeric(curve["net_return"], errors="coerce")
        curve = curve.dropna(subset=["日期", "净收益"]).sort_values("日期")
        if curve.empty:
            continue
        curve["权益指数"] = (1 + curve["净收益"]).cumprod() * 100
        curve["策略"] = report.strategy_name
        curve_frames.append(curve[["日期", "权益指数", "策略"]])
    if curve_frames:
        st.markdown("**顺序交易权益曲线**")
        curve_frame = pd.concat(curve_frames, ignore_index=True)
        curve_chart = (
            alt.Chart(curve_frame)
            .mark_line()
            .encode(
                x=alt.X("日期:T", title=None),
                y=alt.Y("权益指数:Q", title="权益指数（初始=100）"),
                color=alt.Color("策略:N", title=None),
                tooltip=[
                    alt.Tooltip("日期:T", title="退出日期", format="%Y-%m-%d"),
                    alt.Tooltip("策略:N", title="策略"),
                    alt.Tooltip("权益指数:Q", title="权益指数", format=".2f"),
                ],
            )
            .properties(height=300)
            .interactive()
        )
        st.altair_chart(curve_chart, width="stretch")
        st.caption("权益曲线按每笔交易退出后的净收益顺序复利计算，不代表可以无摩擦地连续持有。")
    st.warning(
        "5日/20日胜率按固定观察窗口的毛收益计算；实际净收益胜率才扣除手续费、卖出印花税和滑点。"
        "胜率不是上涨概率，也不代表未来收益。"
        "样本外统计使用历史后30%信号。历史表现不代表未来收益，也不构成投资建议。",
        icon=":material/history:",
    )
    with st.expander("查看优化策略交易明细"):
        show_backtest_records(optimized_report, "优化策略")
    with st.expander("查看原综合评分交易明细"):
        show_backtest_records(base_report, "基础规则")


def build_analysis_report_html(security, history, indicators, financials, results, strategy_result=None) -> str:
    latest = indicators.latest
    summary_rows = "".join(
        "<tr>"
        f"<td>{escape(result.horizon)}</td>"
        f"<td>{escape(result.signal)}</td>"
        f"<td>{'—' if result.score is None else f'{result.score:.1f}'}</td>"
        f"<td>{result.confidence:.1f}%</td>"
        "</tr>"
        for result in results.values()
    )
    reasons = "".join(
        f"<li>{escape(reason)}</li>"
        for result in results.values()
        for reason in result.reasons[:3]
    )
    warnings = "".join(
        f"<li>{escape(warning)}</li>"
        for result in results.values()
        for warning in result.warnings
    ) or "<li>当前没有风险警告</li>"
    strategy_summary = ""
    if strategy_result is not None:
        strategy_summary = (
            f"<h2>{escape(strategy_result.strategy_name)}</h2>"
            f"<p>信号：{escape(strategy_result.signal)}；评分："
            f"{'—' if strategy_result.score is None else f'{strategy_result.score:.1f}'}；"
            f"信号明确度：{strategy_result.confidence:.1f}%</p>"
            f"<h3>买入条件</h3><ul>{''.join(f'<li>{escape(item)}</li>' for item in strategy_result.entry_conditions) or '<li>暂无</li>'}</ul>"
            f"<h3>卖出与风控条件</h3><ul>{''.join(f'<li>{escape(item)}</li>' for item in strategy_result.exit_conditions + strategy_result.risk_controls) or '<li>暂无</li>'}</ul>"
        )
    financial_rows = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(financials.get(key, '—')))}</td></tr>"
        for key, label in {
            "pe": "PE",
            "pb": "PB",
            "roe": "ROE（%）",
            "revenue_growth": "营收增速（%）",
            "profit_growth": "利润增速（%）",
            "debt_ratio": "资产负债率（%）",
        }.items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{escape(security.name)}分析报告</title>
<style>
body {{ font-family: Arial, 'Microsoft YaHei', sans-serif; color: #202124; max-width: 900px; margin: 36px auto; line-height: 1.6; }}
h1 {{ margin-bottom: 4px; }} h2 {{ margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
.meta {{ color: #5f6368; }} table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #dfe1e5; padding: 8px 10px; text-align: left; }}
th {{ background: #f5f7fa; }} .notice {{ background: #fff8e1; padding: 12px; border-left: 4px solid #f0ad00; }}
@media print {{ body {{ margin: 0; }} }}
</style></head>
<body>
<h1>{escape(security.name)}（{escape(security.code)}）</h1>
<div class="meta">数据源：{escape(str(history.source))}　数据日期：{escape(str(history.as_of or '—'))}　最新收盘价：{escape(format_number(latest.get('close')))}</div>
<h2>三周期信号</h2>
<table><thead><tr><th>周期</th><th>信号</th><th>评分</th><th>信号明确度</th></tr></thead><tbody>{summary_rows}</tbody></table>
{strategy_summary}
<h2>主要分析依据</h2><ul>{reasons or '<li>当前没有可用分析依据</li>'}</ul>
<h2>风险与数据警告</h2><ul>{warnings}</ul>
<h2>财务与估值</h2><table><thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>{financial_rows}</tbody></table>
<p class="notice">本报告仅供研究和学习参考，不构成投资建议。历史表现不代表未来收益。</p>
</body></html>"""


def build_analysis_svg(security, history, indicators, results, strategy_result=None) -> str:
    latest = indicators.latest
    lines = [
        f"{security.name}（{security.code}）",
        f"数据日期：{history.as_of or '—'}    数据源：{history.source}",
        f"最新收盘价：{format_number(latest.get('close'))}",
        "三周期信号",
    ]
    for result in results.values():
        score = "—" if result.score is None else f"{result.score:.1f}分"
        lines.append(f"{result.horizon}：{result.signal}    {score}    明确度 {result.confidence:.1f}%")
    if strategy_result is not None:
        score = "—" if strategy_result.score is None else f"{strategy_result.score:.1f}分"
        lines.append(f"优化策略：{strategy_result.signal}    {score}    明确度 {strategy_result.confidence:.1f}%")
    lines.extend(["", "本图仅供研究参考，不构成投资建议。"])
    text_rows = ""
    for index, line in enumerate(lines):
        class_name = "title" if index == 0 else "body"
        text_rows += f'<text x="48" y="{54 + index * 32}" class="{class_name}">{escape(line)}</text>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{80 + len(lines) * 32}" viewBox="0 0 1100 {80 + len(lines) * 32}">
<rect width="100%" height="100%" fill="#ffffff"/>
<rect x="24" y="24" width="1052" height="{32 + len(lines) * 32}" rx="8" fill="#f5f7fa" stroke="#dfe1e5"/>
<style>.title {{ font: 700 24px Arial, 'Microsoft YaHei', sans-serif; fill: #202124; }} .body {{ font: 16px Arial, 'Microsoft YaHei', sans-serif; fill: #3c4043; }}</style>
{text_rows}
</svg>"""


def show_share_and_export(security, history, indicators, financials, results, strategy_result=None) -> None:
    with st.popover("分享与导出", icon=":material/share:"):
        st.caption("分享链接会打开该股票的分析页面。")
        st.code(build_share_url(security.code), language=None)
        report_html = build_analysis_report_html(security, history, indicators, financials, results, strategy_result)
        report_svg = build_analysis_svg(security, history, indicators, results, strategy_result)
        st.download_button(
            "下载分析图（SVG）",
            data=report_svg,
            file_name=f"{security.code}_分析图.svg",
            mime="image/svg+xml",
            icon=":material/image:",
        )
        st.download_button(
            "下载打印版报告（HTML）",
            data=report_html,
            file_name=f"{security.code}_分析报告.html",
            mime="text/html",
            icon=":material/print:",
        )
        st.caption("HTML报告可在浏览器中打开后选择“打印”，保存为PDF。")


def show_data_freshness(history) -> None:
    if history.as_of is None:
        st.warning("缺少最新行情日期，无法确认数据更新时间。", icon=":material/schedule:")
        return
    age_days = (date.today() - history.as_of).days
    if age_days < 0:
        st.warning("行情日期晚于当前日期，请检查数据源。", icon=":material/error:")
    elif history.status != "ok" or age_days > 7:
        st.warning(
            f"数据更新时间提醒：最新行情为 {history.as_of}，距今天 {age_days} 个日历日，当前结果不适合直接作为买卖依据。",
            icon=":material/schedule:",
        )
    elif age_days > 1:
        st.info(
            f"数据更新时间：{history.as_of}，距今天 {age_days} 个日历日；非交易时段以最近收盘数据为准。",
            icon=":material/schedule:",
        )
    else:
        st.caption(f"数据更新时间：{history.as_of}（最近交易日收盘数据）")


def show_data_quality_center(history, indicators, financials) -> None:
    """Make the data pipeline auditable before showing a trading conclusion."""
    report = assess_data_quality(history, indicators, financials)
    with st.container(border=True):
        st.subheader("数据可信度中心")
        st.caption(
            "这是对数据来源、时效、字段和指标完整性的检查，不是对股票上涨概率的预测。"
        )
        metric_cols = st.columns(4)
        metric_cols[0].metric("数据可信度", report.level)
        metric_cols[1].metric("质量评分", f"{report.score:.0f} / 100")
        metric_cols[2].metric("行情日期", str(report.as_of or "—"))
        metric_cols[3].metric("有效日线", f"{report.row_count} 条")

        check_frame = pd.DataFrame(
            [
                {"检查项目": item.name, "状态": item.status, "检查结果": item.detail}
                for item in report.checks
            ]
        )
        st.dataframe(check_frame, width="stretch", hide_index=True)
        if report.actionable:
            st.success(
                "行情来源、时效、价格字段和核心技术指标通过基本检查；"
                "财务数据仍需结合报告期和同行业比较。",
                icon=":material/verified:",
            )
        else:
            st.error(
                "当前数据不满足直接解读买卖方向的最低条件，页面中的方向性结论应视为不可判断。",
                icon=":material/block:",
            )
        if report.financial_coverage < 1:
            st.info(
                f"财务与估值字段覆盖率为 {report.financial_coverage:.0%}。"
                "短线技术分析可以独立计算，但波段和中长期结论会因财务字段缺失而受限。",
                icon=":material/account_balance:",
            )
        for warning in report.warnings:
            st.warning(warning, icon=":material/warning:")


def persist_alert_settings() -> None:
    settings = (
        bool(st.session_state.get("alerts_enabled", True)),
        float(st.session_state.get("price_alert_threshold", 3.0)),
        float(st.session_state.get("score_alert_threshold", 5.0)),
    )
    if settings == st.session_state.get("_saved_alert_settings"):
        return
    try:
        get_store().save_alert_settings(
            get_paper_account_id(), settings[0], settings[1], settings[2]
        )
    except Exception:
        st.warning("提醒设置保存失败，本次会话仍会生效。", icon=":material/cloud_off:")
        return
    st.session_state["_saved_alert_settings"] = settings


def show_personal_tools_sidebar() -> None:
    watchlist = st.session_state["watchlist"]
    if watchlist:
        st.subheader("自选股")
        watch_symbols = list(watchlist)
        selected_watch = st.selectbox(
            "选择自选股",
            watch_symbols,
            format_func=lambda symbol: f"{watchlist[symbol]['name']}（{watchlist[symbol]['code']}）",
            key="watchlist_selection",
        )
        if st.button("分析选中股票", key="analyze_watchlist", width="stretch", icon=":material/search:"):
            st.session_state["analysis_query"] = watchlist[selected_watch]["code"]
            st.session_state["auto_analyze"] = True
            st.rerun()
    else:
        st.caption("查询股票后，可将它加入自选股。")

    recent = st.session_state["recent_queries"]
    if recent:
        st.subheader("最近查询")
        recent_symbols = [item["symbol"] for item in recent]
        selected_recent = st.selectbox(
            "选择最近查询",
            recent_symbols,
            format_func=lambda symbol: next(
                f"{item['name']}（{item['code']}）" for item in recent if item["symbol"] == symbol
            ),
            key="recent_query_selection",
        )
        if st.button("重新分析", key="reanalyze_recent", width="stretch", icon=":material/refresh:"):
            selected_item = next(item for item in recent if item["symbol"] == selected_recent)
            st.session_state["analysis_query"] = selected_item["code"]
            st.session_state["auto_analyze"] = True
            st.rerun()

    with st.expander("提醒设置", icon=":material/notifications:"):
        st.toggle("价格和评分变化提醒", key="alerts_enabled")
        if st.session_state["alerts_enabled"]:
            st.number_input(
                "价格变化阈值（%）",
                min_value=0.5,
                max_value=50.0,
                step=0.5,
                key="price_alert_threshold",
            )
            st.number_input(
                "短线评分变化阈值（分）",
                min_value=1.0,
                max_value=50.0,
                step=1.0,
                key="score_alert_threshold",
            )
            st.caption("重新查询同一股票时，与本次会话上一次记录比较。")
    persist_alert_settings()

    if watchlist or recent:
        if _is_local_runtime():
            st.caption("本地模式：自选股、最近查询、提醒快照和模拟交易保存在 data/stock_analysis.db。")
        else:
            st.caption("公开部署：已登录用户的数据按账号隔离，并保存到 Supabase 云端。")


def show_portfolio(service: PaperTradingService, symbol: str | None = None, price: float | None = None) -> None:
    st.subheader("模拟交易")
    account_id = get_paper_account_id()
    service.ensure_account(account_id)
    quotes = {symbol: price} if symbol and price else {}
    portfolio = service.get_portfolio(account_id, quotes)
    cols = st.columns(4)
    cols[0].metric("可用资金", f"¥{portfolio.cash:,.2f}")
    cols[1].metric("持仓市值", f"¥{portfolio.total_market_value:,.2f}")
    cols[2].metric("账户总资产", f"¥{portfolio.total_equity:,.2f}")
    cols[3].metric("已实现盈亏", f"¥{portfolio.realized_pnl:,.2f}")

    if portfolio.positions:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "股票": item.symbol,
                        "数量": item.quantity,
                        "平均成本": item.average_cost,
                        "最新价": item.current_price,
                        "市值": item.market_value,
                        "浮动盈亏": item.unrealized_pnl,
                    }
                    for item in portfolio.positions
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("暂无模拟持仓。")

    orders = service.list_orders(account_id)
    if orders:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "时间": order.traded_at,
                        "股票": order.symbol,
                        "方向": order.side,
                        "数量": order.shares,
                        "成交价": order.price,
                        "费用": order.fee,
                        "状态": order.status,
                    }
                    for order in orders
                ]
            ),
            width="stretch",
            hide_index=True,
        )


def show_account_status() -> None:
    if _is_local_runtime():
        st.caption("本地运行：个人数据保存在本机 SQLite 数据库。")
        return
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"已登录：{current_user_email()}　·　数据保存：云端同步")
        if st.button("退出登录", icon=":material/logout:"):
            logout()


def require_authentication() -> None:
    """Stop the page before any user-scoped data or protected UI is rendered."""
    if not _is_local_runtime() and not ensure_authenticated():
        st.stop()


def main() -> None:
    require_authentication()
    try:
        initialize_session_state()
    except Exception as exc:
        st.error(cloud_persistence_error_message(exc))
        st.caption("行情接口异常只会影响行情分析；此错误表示个人数据存储尚未准备好。")
        return
    show_account_status()
    st.title("📈 A股股票分析与模拟交易")
    st.caption(f"{APP_VERSION} · 真实收盘日线 · 透明规则评分 · 虚拟资金，不连接真实券商")
    st.info("本工具仅供研究和学习参考，不构成投资建议。数据可能存在延迟、缺失或接口异常。")

    with st.sidebar:
        st.header("查询设置")
        st.divider()
        st.caption("当前仅使用腾讯真实历史行情，AKShare作为备用；接口失败时不会填充虚构价格。")
        show_personal_tools_sidebar()

    auto_analyze = bool(st.session_state.get("auto_analyze", False))
    st.session_state["auto_analyze"] = False
    # Keep the search input and action as ordinary widgets.  This avoids a
    # hosted Streamlit form-submit event being swallowed while the browser
    # session component is restoring authentication.  The text input already
    # persists its value through its key, and no network call happens while
    # the user is typing; the expensive work still starts only on button click.
    with st.container(horizontal=True, vertical_alignment="bottom"):
        query = st.text_input(
            "股票名称或代码",
            placeholder="例如：600519、sh.600519 或 贵州茅台",
            key="analysis_query",
        )
        submitted = st.button(
            "开始分析",
            type="primary",
            icon=":material/search:",
            key="start_analysis_button",
        )
    analyze = submitted or auto_analyze
    analysis_feedback_slot = st.container()
    st.caption("支持6位股票代码、带交易所前缀的代码或股票名称；公开数据异常时不会生成买卖信号。")
    show_scoring_guide()
    show_strategy_guide()

    request_key = query.strip()
    # Keep the last successful result while the user edits the next query.
    # Streamlit reruns on widget changes, and clearing analysis here made
    # a temporary lookup/network failure look like the whole app had lost its
    # result. A result is replaced only after a new analysis succeeds.
    error_query = st.session_state.get("analysis_error_query")
    if error_query and error_query != request_key:
        st.session_state.pop("analysis_error", None)
        st.session_state.pop("analysis_error_query", None)

    if analyze:
        st.session_state["analysis_error"] = ""
        st.session_state.pop("analysis_error_query", None)
        st.session_state["analysis_alerts"] = []
        progress_status = analysis_feedback_slot.status(
            "\u6b63\u5728\u5f00\u59cb\u5206\u6790\uff0c\u8bf7\u7a0d\u5019\u2026",
            expanded=True,
        )
        try:
            with st.spinner("正在获取数据并计算指标…"):
                analysis = run_analysis(
                    query,
                    progress=lambda message: progress_status.update(
                        label=message,
                        state="running",
                        expanded=True,
                    ),
                )
            progress_status.update(
                label="\u5206\u6790\u5b8c\u6210\uff0c\u6b63\u5728\u663e\u793a\u7ed3\u679c\u3002",
                state="complete",
                expanded=False,
            )
            # Persist the successful analysis before optional cloud side effects.
            # If a user-data write fails, a rerun must not erase the visible result.
            st.session_state["analysis"] = analysis
            st.session_state["analysis_key"] = request_key
            analyzed_security, analyzed_history, analyzed_indicators, _, analyzed_results = analysis
            try:
                remember_query(analyzed_security, analyzed_history)
            except Exception as exc:
                st.warning(
                    "分析已完成，但最近查询记录暂时无法同步到云端："
                    f"{cloud_persistence_error_message(exc)}",
                    icon=":material/cloud_off:",
                )
            try:
                st.session_state["analysis_alerts"] = record_change_alerts(
                    analyzed_security,
                    analyzed_indicators,
                    analyzed_results["short"],
                )
            except Exception as exc:
                st.warning(
                    "分析已完成，但价格/评分提醒快照暂时无法保存："
                    f"{cloud_persistence_error_message(exc)}",
                    icon=":material/notifications_off:",
                )
            try:
                st.query_params["symbol"] = analyzed_security.code
            except Exception:
                # The URL is only a sharing convenience and must not block results.
                pass
        except Exception as exc:
            progress_status.update(
                label="\u5206\u6790\u672a\u5b8c\u6210\uff0c\u8bf7\u67e5\u770b\u4e0b\u65b9\u9519\u8bef\u8bf4\u660e\u3002",
                state="error",
                expanded=True,
            )
            st.session_state["analysis_error"] = str(exc)
            st.session_state["analysis_error_query"] = request_key

    if st.session_state.get("analysis_error"):
        st.error(st.session_state.analysis_error)
        if "analysis" in st.session_state:
            previous_key = st.session_state.get("analysis_key", "")
            if previous_key and previous_key != request_key:
                st.caption(
                    "\u4ecd\u663e\u793a\u4e0a\u4e00\u6b21\u6210\u529f\u5206\u6790\u7ed3\u679c\uff08\u67e5\u8be2\uff1a"
                    f"{previous_key}\uff09\uff1b\u65b0\u67e5\u8be2\u6210\u529f\u540e\u4f1a\u81ea\u52a8\u66ff\u6362\u3002"
                )

    if "analysis" in st.session_state and st.session_state.get("analysis_key") != request_key:
        if not st.session_state.get("analysis_error"):
            previous_key = st.session_state.get("analysis_key", "")
            if previous_key:
                st.info(
                    "\u5f53\u524d\u663e\u793a\u7684\u662f\u4e0a\u4e00\u6b21\u6210\u529f\u5206\u6790\u7ed3\u679c\uff08\u67e5\u8be2\uff1a"
                    f"{previous_key}\uff09\uff1b\u70b9\u51fb\u201c\u5f00\u59cb\u5206\u6790\u201d\u540e\u624d\u4f1a\u66ff\u6362\u4e3a\u65b0\u67e5\u8be2\u3002"
                )

    if "analysis" not in st.session_state:
        st.markdown("### 从这里开始")
        st.write("输入股票代码后点击“开始分析”。公开数据接口不可用时，页面会显示数据不足。")
        return

    security, history, indicators, financials, results = st.session_state.analysis
    strategy_result = evaluate_strategy(indicators, DEFAULT_STRATEGY)
    st.header(f"{security.name}（{security.code}）")
    freshness = "正常" if history.status == "ok" else "缓存/需检查"
    source_label = SOURCE_LABELS.get(history.source, history.source)
    st.caption(
        f"交易所：{security.exchange}　状态：{security.market_status}　"
        f"数据源：{source_label}　数据状态：{freshness}"
    )
    show_data_freshness(history)
    show_data_quality_center(history, indicators, financials)
    if history.message:
        st.info(history.message)
    for alert in st.session_state.get("analysis_alerts", []):
        st.warning(alert, icon=":material/notifications_active:")

    watchlist = st.session_state["watchlist"]
    is_watched = security.symbol in watchlist
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(
            "移出自选" if is_watched else "加入自选",
            key=f"watch_toggle_{security.symbol}",
            icon=":material/star:" if is_watched else ":material/star_border:",
        ):
            if is_watched:
                watchlist.pop(security.symbol, None)
                get_store().delete_watchlist_item(get_paper_account_id(), security.symbol)
                st.toast("已移出自选股")
            else:
                watchlist[security.symbol] = {
                    "symbol": security.symbol,
                    "code": security.code,
                    "name": security.name,
                    "exchange": security.exchange,
                    "market_status": security.market_status,
                }
                get_store().save_watchlist_item(get_paper_account_id(), security)
                st.toast("已加入自选股")
            st.rerun()
        show_share_and_export(security, history, indicators, financials, results, strategy_result)
    latest = indicators.latest
    cols = st.columns(4)
    cols[0].metric("最新收盘价", format_number(latest.get("close")))
    cols[1].metric("日涨跌幅", format_percent(latest.get("return1")))
    cols[2].metric("RSI14", format_number(latest.get("rsi14")))
    cols[3].metric("MACD柱", format_number(latest.get("macd_hist"), 4))

    tabs = st.tabs(
        ["综合结论", "行情与指标", "资金流向", "财务与估值", "模拟交易", "历史验证"],
        on_change="rerun",
    )
    if tabs[0].open:
        with tabs[0]:
            show_strategy_result(strategy_result)
            show_holding_ratio_guidance(strategy_result)
            show_trade_plan(security, strategy_result, latest)
            with st.expander("原综合评分对照", expanded=False):
                show_horizon_overview(results)
                for key in ("short", "swing", "long"):
                    show_result(results[key])
                    if key != "long":
                        st.divider()

    if tabs[1].open:
        with tabs[1]:
            st.altair_chart(build_price_chart(indicators.frame), width="stretch")
            show_market_comparison(security, history)
            st.dataframe(indicators.frame.tail(30), width="stretch", hide_index=True)

    if tabs[2].open:
        with tabs[2]:
            show_money_flow(security)

    if tabs[3].open:
        with tabs[3]:
            labels = {"pe": "PE", "pb": "PB", "roe": "ROE（%）", "revenue_growth": "营收增速（%）", "profit_growth": "利润增速（%）", "debt_ratio": "资产负债率（%）"}
            financial_frame = pd.DataFrame(
                [{"指标": label, "数值": format_financial_value(key, financials.get(key))} for key, label in labels.items()]
            )
            st.dataframe(financial_frame, width="stretch", hide_index=True)
            show_financial_metric_guide(financials)

    if tabs[4].open:
        with tabs[4]:
            store = get_store()
            paper = PaperTradingService(store)
            account_id = get_paper_account_id()
            st.warning(
                "当前最新行情尚未包含下一交易日开盘价，交互式模拟使用最新收盘价加滑点估算；"
                "历史信号验证必须使用下一交易日开盘价，避免未来函数。"
            )
            with st.form("paper_order"):
                side = st.selectbox("方向", ["买入", "卖出"])
                shares = st.number_input("数量（100股的整数倍）", min_value=100, value=100, step=100)
                submitted = st.form_submit_button("提交模拟订单")
            if submitted:
                try:
                    order = paper.create_paper_order(
                        account_id,
                        security.symbol,
                        side,
                        int(shares),
                        float(latest["close"]),
                        date.today(),
                    )
                    st.success(f"模拟订单已成交：{order.side} {order.shares} 股，记账价 ¥{order.price:.2f}")
                except Exception as exc:
                    st.error(str(exc))
            show_portfolio(paper, security.symbol, float(latest["close"]))
            show_trade_reviews(security)

    if tabs[5].open:
        with tabs[5]:
            show_historical_validation(indicators)


if __name__ == "__main__":
    main()
