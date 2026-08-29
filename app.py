from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import uuid

import pandas as pd
import streamlit as st
import altair as alt

from stock_analysis.data import CsvDataProvider, StockDataService, create_provider
from stock_analysis.db import SQLiteStore
from stock_analysis.indicators import calculate_indicators
from stock_analysis.paper import PaperTradingService
from stock_analysis.scoring import evaluate_all_horizons


st.set_page_config(page_title="A股智能分析", page_icon="📈", layout="wide")

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_LABELS = {
    "public": "公开数据",
    "akshare": "AKShare公开数据",
    "tencent": "腾讯公开行情",
    "sqlite-cache": "本地公开数据缓存",
    "csv-import": "用户导入CSV",
}


@st.cache_resource
def get_store() -> SQLiteStore:
    return SQLiteStore(PROJECT_ROOT / "data" / "stock_analysis.db")


def format_number(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def format_percent(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    return f"{value * 100:.2f}%" if abs(value) < 2 else f"{value:.2f}%"


def get_paper_account_id() -> str:
    """Keep paper-trading balances isolated when several people share one app."""
    return st.session_state.setdefault("paper_account_id", f"session-{uuid.uuid4().hex}")


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


def run_analysis(query: str, mode: str, uploaded_file=None):
    if mode.startswith("CSV"):
        if uploaded_file is None:
            raise ValueError("请先上传包含 date/open/high/low/close 列的CSV文件")
        try:
            provider = CsvDataProvider(pd.read_csv(uploaded_file))
        except Exception as exc:
            raise ValueError(
                "CSV文件无法读取。请确认至少包含：日期、开盘、最高、最低、收盘；"
                f"原始错误：{exc}"
            ) from exc
    else:
        provider = create_provider("public")
    service = StockDataService(provider, get_store())
    security = service.resolve_security(query)
    start = date.today() - timedelta(days=365 * 3)
    history = service.load_market_data(security, start, date.today())
    if history.data.empty:
        raise RuntimeError(history.message or "没有获取到行情数据")
    indicators = calculate_indicators(history)
    financials = service.load_financials(security)
    results = evaluate_all_horizons(indicators, financials)
    return security, history, indicators, financials, results


def show_result(result) -> None:
    status = "正常" if result.data_status == "ok" else "需要检查数据"
    st.subheader(f"{result.horizon}：{result.signal}")
    metric_cols = st.columns(4)
    metric_cols[0].metric("综合评分", "—" if result.score is None else f"{result.score:.1f} / 100")
    metric_cols[1].metric("置信度", f"{result.confidence:.1f}%")
    metric_cols[2].metric("数据日期", str(result.as_of or "—"))
    metric_cols[3].metric("数据状态", status)

    if result.score is not None:
        new_position = "买入候选" if result.score >= 70 else "观望" if result.score >= 45 else "不宜买入"
        existing_position = "持有" if result.score >= 45 else "减仓/卖出倾向"
    else:
        new_position = existing_position = "不可判断"
    st.write(f"**没有持仓时：** {new_position}　　**已有持仓时：** {existing_position}")

    if result.warnings:
        for warning in result.warnings:
            st.warning(warning)

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


def main() -> None:
    st.title("📈 A股股票分析与模拟交易")
    st.caption("真实收盘日线 · 透明规则评分 · 虚拟资金，不连接真实券商")
    st.info("本工具仅供研究和学习参考，不构成投资建议。数据可能存在延迟、缺失或接口异常。")

    with st.sidebar:
        st.header("查询设置")
        mode = st.radio(
            "数据模式",
            ["公开数据（腾讯+AKShare备用）", "CSV真实历史数据"],
            index=0,
        )
        query = st.text_input("股票名称或代码", value="600519", placeholder="例如：600519 或 贵州茅台")
        uploaded_file = None
        if mode.startswith("CSV"):
            uploaded_file = st.file_uploader(
                "上传真实历史日线CSV",
                type=["csv"],
                help="至少包含：日期、开盘、最高、最低、收盘；可选成交量、成交额。",
            )
        analyze = st.button("开始分析", type="primary", width="stretch")
        st.divider()
        if mode.startswith("CSV"):
            st.caption("CSV模式只使用你上传文件中的实际历史价格，不访问网络。")
        else:
            st.caption("公开模式优先使用腾讯真实历史行情，AKShare仅作备用；接口失败时不会填充虚构价格。")

    request_key = (
        mode,
        query.strip(),
        getattr(uploaded_file, "name", "") if uploaded_file is not None else "",
    )
    if st.session_state.get("analysis_key") != request_key:
        st.session_state.pop("analysis", None)
        st.session_state.pop("analysis_error", None)

    if analyze:
        st.session_state.pop("analysis", None)
        st.session_state["analysis_error"] = ""
        try:
            with st.spinner("正在获取数据并计算指标…"):
                st.session_state.analysis = run_analysis(query, mode, uploaded_file)
                st.session_state.analysis_key = request_key
        except Exception as exc:
            st.session_state.analysis_error = str(exc)
            st.session_state.analysis_key = request_key

    if st.session_state.get("analysis_error"):
        st.error(st.session_state.analysis_error)

    if "analysis" not in st.session_state:
        st.markdown("### 从这里开始")
        st.write("输入股票代码后点击“开始分析”。公开模式获取不到数据时，可改用真实CSV导入。")
        return

    security, history, indicators, financials, results = st.session_state.analysis
    st.header(f"{security.name}（{security.code}）")
    freshness = "正常" if history.status == "ok" else "缓存/需检查"
    source_label = SOURCE_LABELS.get(history.source, history.source)
    st.caption(
        f"交易所：{security.exchange}　状态：{security.market_status}　"
        f"数据源：{source_label}　数据状态：{freshness}"
    )
    if history.message:
        st.info(history.message)
    latest = indicators.latest
    cols = st.columns(4)
    cols[0].metric("最新收盘价", format_number(latest.get("close")))
    cols[1].metric("日涨跌幅", format_percent(latest.get("return1")))
    cols[2].metric("RSI14", format_number(latest.get("rsi14")))
    cols[3].metric("MACD柱", format_number(latest.get("macd_hist"), 4))

    tabs = st.tabs(["综合结论", "行情与指标", "财务与估值", "模拟交易"])
    with tabs[0]:
        for key in ("short", "swing", "long"):
            show_result(results[key])
            if key != "long":
                st.divider()

    with tabs[1]:
        st.altair_chart(build_price_chart(indicators.frame), width="stretch")
        st.dataframe(indicators.frame.tail(30), width="stretch", hide_index=True)

    with tabs[2]:
        labels = {"pe": "PE", "pb": "PB", "roe": "ROE（%）", "revenue_growth": "营收增速（%）", "profit_growth": "利润增速（%）", "debt_ratio": "资产负债率（%）"}
        financial_frame = pd.DataFrame(
            [{"指标": label, "数值": financials.get(key, "—")} for key, label in labels.items()]
        )
        st.dataframe(financial_frame, width="stretch", hide_index=True)
        st.caption("估值和财务评分使用公开字段的简化分档，不能替代完整财报研究。")

    with tabs[3]:
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


if __name__ == "__main__":
    main()
