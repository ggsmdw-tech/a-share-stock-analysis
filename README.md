# A股股票分析与模拟交易应用

这是一个本地运行的中文 Streamlit 应用，支持：

- 输入 A 股名称或代码
- 获取日线行情和公开财务数据
- 生成短线、波段、中长期透明评分
- 展示技术指标、估值、财务质量和风险提示
- 使用虚拟资金进行模拟交易

本项目只用于研究和学习，不连接券商，不执行真实交易，也不构成投资建议。应用不内置虚构行情。

## Windows 安装

在项目目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 Streamlit 显示的本地地址即可。

需要发给别人使用时，优先看 [DEPLOY.md](DEPLOY.md)。Windows 用户也可以双击 `start_app.bat` 一键安装依赖并启动；该方式要求对方电脑已安装 Python 3.12，并且能访问腾讯/AKShare等公开数据接口。

应用固定使用公开数据：先请求腾讯真实历史行情，失败后再尝试 AKShare。不同公开来源的缓存彼此隔离，避免缓存混淆真实来源。

腾讯、AKShare等公开接口可能受网络、限流或本机安全策略影响。所有接口都不可用时，应用只显示数据不足，不会用虚构价格或旧分析结果生成公开模式的买卖信号。

## 项目结构

```text
app.py                         Streamlit 页面
stock_analysis/models.py       数据模型
stock_analysis/data.py         数据源适配、代码解析、缓存服务
stock_analysis/indicators.py   技术指标计算
stock_analysis/scoring.py      多周期规则评分
stock_analysis/db.py           SQLite 存储
stock_analysis/paper.py        模拟交易
tests/                         自动化测试
```

## 评分说明

评分范围为 0–100：

- 70 分及以上：买入候选
- 45–69 分：观望/持有
- 44 分及以下：减仓/卖出倾向

数据不足、数据过期、停牌或风险标记会覆盖评分，结果显示为“数据不足/不可判断”。
