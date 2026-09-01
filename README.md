# A股股票分析与模拟交易应用 · v0.04 多用户云端持久化版

当前版本为 v0.04 多用户云端持久化版，用于验证真实公开行情、数据质量检查、评分依据、市场对照、历史验证、模拟交易和多用户数据隔离流程。

这是一个本地运行的中文 Streamlit 应用，支持：

- 输入 A 股名称或代码
- 获取日线行情和公开财务数据
- 生成短线、波段、中长期透明评分
- 使用趋势动量波段策略进行5–20个交易日的规则验证
- 查看数据可信度中心、行情完整性和财务数据覆盖率
- 对照沪深300相对表现
- 根据当前持仓比例计算规则化的新增仓位和减仓比例
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

Windows 用户也可以双击 `start_app.bat`。启动脚本会自动检测 8501 端口；如果应用已经运行，会直接打开已有页面。依赖没有变化时不会重复联网安装，适合每天重启电脑后使用。

需要发给别人使用时，优先看 [DEPLOY.md](DEPLOY.md)。公开网址模式使用 Supabase 邮箱登录和云端个人数据；Windows 本地分享包仍要求对方电脑安装 Python 3.12。

## v0.04 多用户云端模式

线上部署后，用户必须注册并验证邮箱。Supabase 用户 UUID 作为个人账户标识，搜索、自选股、提醒、模拟账户、订单、交易计划和复盘记录保存在 Supabase PostgreSQL 中；RLS 确保用户只能访问自己的数据。

部署前请执行 [`supabase/schema.sql`](supabase/schema.sql)，并在 Streamlit Community Cloud Secrets 中配置：

```toml
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_ANON_KEY = "YOUR_PUBLIC_ANON_KEY"
```

只使用 Supabase 公共 `anon` key，绝不把 `service_role` key 放入代码、Secrets 或 GitHub。完整步骤见 [DEPLOY.md](DEPLOY.md) 和 [supabase/README.md](supabase/README.md)。

本地配置示例见 [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example)。仓库的 GitHub Actions 会在提交到 `main` 或创建 Pull Request 时自动运行语法检查和完整测试。

## 本地数据持久化

本地运行时，自选股、最近查询、评分提醒快照以及模拟交易账户都会保存到 `data/stock_analysis.db`。关闭浏览器、重新启动 Streamlit 或重新打开应用后，仍会从这个数据库恢复。

- 不要删除或移动 `data/stock_analysis.db`；迁移电脑时先停止 Streamlit，再复制这个文件。
- 数据库文件已被 `.gitignore` 忽略，不会自动上传 GitHub，也不应把个人模拟交易记录提交到公开仓库。
- 旧版本随机模拟账户会在本地首次启动时尝试迁移到固定本地账户；原账户不会被删除。
- 如果只删除浏览器缓存不会影响本地数据库，但删除数据库文件后无法从代码恢复其中的个人记录。

应用固定使用公开数据：先请求腾讯真实历史行情，失败后再尝试 AKShare。不同公开来源的缓存彼此隔离，避免缓存混淆真实来源。

腾讯、AKShare等公开接口可能受网络、限流或本机安全策略影响。所有接口都不可用时，应用只显示数据不足，不会用虚构价格或旧分析结果生成公开模式的买卖信号。

## 项目结构

```text
app.py                         Streamlit 页面
stock_analysis/models.py       数据模型
stock_analysis/data.py         数据源适配、代码解析、缓存服务
stock_analysis/indicators.py   技术指标计算
stock_analysis/scoring.py      多周期规则评分
stock_analysis/strategy.py     趋势动量波段策略
stock_analysis/backtest.py     原规则与优化策略历史回测
stock_analysis/quality.py      数据可信度检查
stock_analysis/db.py           SQLite 存储
stock_analysis/cloud_store.py  Supabase 用户数据存储和 HybridStore
stock_analysis/auth.py         Supabase 邮箱认证和浏览器会话恢复
stock_analysis/paper.py        模拟交易
supabase/schema.sql            云端表、RLS 和原子订单 RPC
tests/                         自动化测试
```

## 评分说明

评分范围为 0–100：

- 70 分及以上：买入候选
- 45–69 分：观望/持有
- 44 分及以下：减仓/卖出倾向

数据不足、数据过期、停牌或风险标记会覆盖评分，结果显示为“数据不足/不可判断”。

## 优化策略与历史验证

综合结论会优先展示“趋势动量波段策略”，原综合评分保留在对照区。页面顶部先展示数据可信度中心，行情页再提供沪深300相对表现对照。优化策略要求趋势、动量、量价、风险和评分同时确认，买入后按 ATR 止损/止盈、趋势转弱或最长持有期退出。

综合结论还可以输入当前该股占总资产的持仓比例，查看规则目标仓位、最高仓位、建议新增仓位和建议卖出比例。该比例只用于固定规则的风险控制展示，不是个性化投资建议。

历史验证使用下一交易日开盘入场，并计入手续费、卖出印花税和滑点。页面同时展示5日/20日信号后表现、实际净收益、盈亏比、最大回撤、顺序交易权益曲线和历史后30%的样本外结果。信号少于30个时会提示样本不足；历史胜率不代表未来收益。
