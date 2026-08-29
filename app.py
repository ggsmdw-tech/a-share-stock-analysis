from __future__ import annotations

from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import pandas as pd
import streamlit as st
import altair as alt

from stock_analysis.backtest import BacktestReport, backtest_buy_signals
from stock_analysis.data import StockDataService, create_provider
from stock_analysis.db import SQLiteStore
from stock_analysis.indicators import calculate_indicators
from stock_analysis.models import IndicatorSnapshot, Security
from stock_analysis.paper import PaperTradingService
from stock_analysis.scoring import evaluate_all_horizons


st.set_page_config(page_title="A股智能分析", page_icon="📈", layout="wide")

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_LABELS = {
    "public": "公开数据",
    "akshare": "AKShare公开数据",
    "tencent": "腾讯公开行情",
    "sqlite-cache": "本地公开数据缓存",
}


@st.cache_resource
def get_store() -> SQLiteStore:
    return SQLiteStore(PROJECT_ROOT / "data" / "stock_analysis.db")


def initialize_session_state() -> None:
    shared_symbol = str(st.query_params.get("symbol", "")).strip()
    st.session_state.setdefault("analysis_query", shared_symbol or "600519")
    st.session_state.setdefault("auto_analyze", bool(shared_symbol))
    st.session_state.setdefault("watchlist", {})
    st.session_state.setdefault("recent_queries", [])
    st.session_state.setdefault("analysis_snapshots", {})
    st.session_state.setdefault("analysis_alerts", [])
    st.session_state.setdefault("alerts_enabled", True)
    st.session_state.setdefault("price_alert_threshold", 3.0)
    st.session_state.setdefault("score_alert_threshold", 5.0)


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


def format_number(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def format_percent(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    return f"{value * 100:.2f}%" if abs(value) < 2 else f"{value:.2f}%"


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


def run_analysis(query: str):
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
        percent_cols[0].metric("买入规则支持度", f"{buy_support:.1f}%")
        percent_cols[1].metric("减仓/卖出规则倾向", f"{sell_tendency:.1f}%")
        st.caption(
            "百分比计算：买入规则支持度 = 综合评分；减仓/卖出规则倾向 = 100 − 综合评分。"
            "两者是方向性规则展示，不是上涨/下跌概率、准确率或建议仓位比例。"
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


def show_historical_validation(indicators) -> None:
    st.subheader("历史信号验证")
    st.caption(
        "默认验证短线规则：信号日收盘后，下一交易日开盘模拟入场；统计后续第5个和第20个交易日的收盘表现。"
    )
    report = run_historical_backtest(
        indicators.frame,
        indicators.security.code,
        indicators.security.name,
        indicators.security.exchange,
        indicators.security.market_status,
        indicators.status,
        indicators.message,
    )
    if report.message:
        st.warning(report.message, icon=":material/warning:")
    if report.signal_count == 0:
        if not report.message:
            st.info("当前没有可展示的历史信号统计。", icon=":material/info:")
        return

    metric_row_one = st.columns(3)
    metric_row_one[0].metric("历史买入信号数", str(report.signal_count))
    metric_row_one[1].metric("5日胜率", format_percent(report.win_rate_5d))
    metric_row_one[2].metric("20日胜率", format_percent(report.win_rate_20d))
    metric_row_two = st.columns(3)
    metric_row_two[0].metric("5日平均收益", format_percent(report.avg_return_5d))
    metric_row_two[1].metric("20日平均收益", format_percent(report.avg_return_20d))
    metric_row_two[2].metric("20日内最大回撤", format_percent(report.max_drawdown_20d))

    st.warning(
        "历史统计使用毛收益，未计手续费、滑点、涨跌停和停牌影响；不同信号的观察窗口可能重叠，"
        "这些指标用于检验规则，不等同于完整资金曲线；历史表现不代表未来收益，也不构成投资建议。",
        icon=":material/history:",
    )
    display_frame = report.signals.rename(
        columns={
            "signal_date": "信号日期",
            "entry_date": "次日入场日期",
            "score": "规则评分",
            "entry_price": "入场价格",
            "return_5d": "5日收益",
            "return_20d": "20日收益",
            "max_drawdown_20d": "20日内最大回撤",
        }
    ).copy()
    display_frame["规则评分"] = display_frame["规则评分"].map(lambda value: f"{value:.1f}")
    display_frame["入场价格"] = display_frame["入场价格"].map(format_number)
    for column in ("5日收益", "20日收益", "20日内最大回撤"):
        display_frame[column] = display_frame[column].map(format_percent)
    if len(display_frame) > 100:
        st.caption("下表显示最近100个信号，顶部统计使用全部历史信号。")
        display_frame = display_frame.tail(100)
    st.dataframe(display_frame, width="stretch", hide_index=True)


def build_analysis_report_html(security, history, indicators, financials, results) -> str:
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
<h2>主要分析依据</h2><ul>{reasons or '<li>当前没有可用分析依据</li>'}</ul>
<h2>风险与数据警告</h2><ul>{warnings}</ul>
<h2>财务与估值</h2><table><thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>{financial_rows}</tbody></table>
<p class="notice">本报告仅供研究和学习参考，不构成投资建议。历史表现不代表未来收益。</p>
</body></html>"""


def build_analysis_svg(security, history, indicators, results) -> str:
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


def show_share_and_export(security, history, indicators, financials, results) -> None:
    with st.popover("分享与导出", icon=":material/share:"):
        st.caption("分享链接会打开该股票的分析页面。")
        st.code(build_share_url(security.code), language=None)
        report_html = build_analysis_report_html(security, history, indicators, financials, results)
        report_svg = build_analysis_svg(security, history, indicators, results)
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

    if watchlist or recent:
        st.caption("自选股、最近查询和提醒记录仅保存在当前浏览器会话。")


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
    initialize_session_state()
    st.title("📈 A股股票分析与模拟交易")
    st.caption("真实收盘日线 · 透明规则评分 · 虚拟资金，不连接真实券商")
    st.info("本工具仅供研究和学习参考，不构成投资建议。数据可能存在延迟、缺失或接口异常。")

    with st.sidebar:
        st.header("查询设置")
        st.divider()
        st.caption("当前仅使用腾讯真实历史行情，AKShare作为备用；接口失败时不会填充虚构价格。")
        show_personal_tools_sidebar()

    auto_analyze = bool(st.session_state.get("auto_analyze", False))
    st.session_state["auto_analyze"] = False
    with st.form("analysis_form", border=False):
        with st.container(horizontal=True, vertical_alignment="bottom"):
            query = st.text_input(
                "股票名称或代码",
                placeholder="例如：600519、sh.600519 或 贵州茅台",
                key="analysis_query",
            )
            submitted = st.form_submit_button(
                "开始分析",
                type="primary",
                icon=":material/search:",
            )
    analyze = submitted or auto_analyze
    st.caption("支持6位股票代码、带交易所前缀的代码或股票名称；公开数据异常时不会生成买卖信号。")
    show_scoring_guide()

    request_key = query.strip()
    if st.session_state.get("analysis_key") != request_key:
        st.session_state.pop("analysis", None)
        st.session_state.pop("analysis_error", None)

    if analyze:
        st.session_state.pop("analysis", None)
        st.session_state["analysis_error"] = ""
        st.session_state["analysis_alerts"] = []
        try:
            with st.spinner("正在获取数据并计算指标…"):
                analysis = run_analysis(query)
                st.session_state.analysis = analysis
                analyzed_security, analyzed_history, analyzed_indicators, _, analyzed_results = analysis
                remember_query(analyzed_security, analyzed_history)
                st.session_state["analysis_alerts"] = record_change_alerts(
                    analyzed_security,
                    analyzed_indicators,
                    analyzed_results["short"],
                )
                st.query_params["symbol"] = analyzed_security.code
                st.session_state.analysis_key = request_key
        except Exception as exc:
            st.session_state.analysis_error = str(exc)
            st.session_state.analysis_key = request_key

    if st.session_state.get("analysis_error"):
        st.error(st.session_state.analysis_error)

    if "analysis" not in st.session_state:
        st.markdown("### 从这里开始")
        st.write("输入股票代码后点击“开始分析”。公开数据接口不可用时，页面会显示数据不足。")
        return

    security, history, indicators, financials, results = st.session_state.analysis
    st.header(f"{security.name}（{security.code}）")
    freshness = "正常" if history.status == "ok" else "缓存/需检查"
    source_label = SOURCE_LABELS.get(history.source, history.source)
    st.caption(
        f"交易所：{security.exchange}　状态：{security.market_status}　"
        f"数据源：{source_label}　数据状态：{freshness}"
    )
    show_data_freshness(history)
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
                st.toast("已移出自选股")
            else:
                watchlist[security.symbol] = {
                    "symbol": security.symbol,
                    "code": security.code,
                    "name": security.name,
                    "exchange": security.exchange,
                }
                st.toast("已加入自选股")
            st.rerun()
        show_share_and_export(security, history, indicators, financials, results)
    latest = indicators.latest
    cols = st.columns(4)
    cols[0].metric("最新收盘价", format_number(latest.get("close")))
    cols[1].metric("日涨跌幅", format_percent(latest.get("return1")))
    cols[2].metric("RSI14", format_number(latest.get("rsi14")))
    cols[3].metric("MACD柱", format_number(latest.get("macd_hist"), 4))

    tabs = st.tabs(
        ["综合结论", "行情与指标", "财务与估值", "模拟交易", "历史验证"],
        on_change="rerun",
    )
    if tabs[0].open:
        with tabs[0]:
            show_horizon_overview(results)
            for key in ("short", "swing", "long"):
                show_result(results[key])
                if key != "long":
                    st.divider()

    if tabs[1].open:
        with tabs[1]:
            st.altair_chart(build_price_chart(indicators.frame), width="stretch")
            st.dataframe(indicators.frame.tail(30), width="stretch", hide_index=True)

    if tabs[2].open:
        with tabs[2]:
            labels = {"pe": "PE", "pb": "PB", "roe": "ROE（%）", "revenue_growth": "营收增速（%）", "profit_growth": "利润增速（%）", "debt_ratio": "资产负债率（%）"}
            financial_frame = pd.DataFrame(
                [{"指标": label, "数值": financials.get(key, "—")} for key, label in labels.items()]
            )
            st.dataframe(financial_frame, width="stretch", hide_index=True)
            st.caption("估值和财务评分使用公开字段的简化分档，不能替代完整财报研究。")

    if tabs[3].open:
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

    if tabs[4].open:
        with tabs[4]:
            show_historical_validation(indicators)


if __name__ == "__main__":
    main()
