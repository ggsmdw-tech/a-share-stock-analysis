# 分享和部署

当前版本：v0.04 多用户云端持久化版。对外分享前，建议先完成两个测试账号的数据隔离和刷新持久化测试。

当前 `http://localhost:8501/` 只对运行程序的电脑有效，不能直接发给别人。要让别人打开网址就能使用，推荐部署到 Streamlit Community Cloud；要把程序文件发给别人，则使用项目里的 Windows 一键启动脚本。

## 方式一：发布一个公共网址

1. 在 GitHub 新建一个仓库，把本项目上传。不要上传 `.venv`、`data/*.db`、`.pytest_cache` 或 `.streamlit/secrets.toml`。
2. 创建 Supabase 项目，在 SQL Editor 中执行 [`supabase/schema.sql`](supabase/schema.sql)。
3. 在 Supabase 的 Authentication → Providers 中开启 Email，并保持邮箱验证开启。
4. 在 Authentication → URL Configuration → Redirect URLs 中加入部署后的 Streamlit 地址，例如 `https://你的应用.streamlit.app/`。
5. 在 Streamlit Community Cloud 的 Advanced settings → Secrets 中填写：

   ```toml
   SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
   SUPABASE_ANON_KEY = "YOUR_PUBLIC_ANON_KEY"
   ```

   只能使用公共 anon key，不要使用 service role key。
6. 打开 [Streamlit Community Cloud](https://share.streamlit.io/)，使用 GitHub 登录并选择这个仓库。
7. 创建应用时选择主分支，入口文件填写 `app.py`；Python 版本选择 3.12，然后点击部署。
8. 部署成功后，把得到的 `streamlit.app` 网址发给别人即可。用户打开后先注册、验证邮箱，再登录使用。

仓库已经包含 `requirements.txt`，云端会自动安装依赖。公开模式请求腾讯真实行情，AKShare作为备用；云端主机如果无法访问这些公开接口，页面会显示数据不足，不会生成虚构价格。

上线前最后检查：

- GitHub Actions 的 `Test A-share stock analysis app` 为绿色；
- Supabase 的 `supabase/schema.sql` 已成功执行；
- Streamlit Secrets 使用的是 `anon` key，而不是 `service_role` key；
- Streamlit 应用的入口文件是 `app.py`，Python 版本为 3.12；
- 先用两个邮箱完成一次数据隔离测试，再把网址发给其他人。

## 方式二：发送一个 Windows 分享包

在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\package_for_sharing.ps1
```

把生成的 ZIP 发给对方。对方解压后双击 `start_app.bat`，脚本会创建虚拟环境、安装依赖、启动应用并打开浏览器。

这种方式要求对方安装 Python 3.12，并允许 Python 访问互联网。若对方电脑没有 Python，公共网址方式更方便；本项目没有把 Python 和全部依赖打包进仓库。

## 多人使用须知

- 线上必须登录；每个用户使用自己的 Supabase UUID，个人数据由 RLS 隔离。
- 刷新页面、关闭浏览器后重新打开网址并登录同一邮箱，搜索、自选股、模拟账户、订单、计划和复盘仍会恢复。
- 行情、财务和技术指标缓存仍是应用本地临时缓存，不是用户数据；部署重启后可重新获取。
- 本地运行时会使用固定的本地账户，并把自选股、最近查询、提醒快照和模拟交易保存到 `data/stock_analysis.db`；重新打开应用即可继续使用。
- 如果需要把本地记录迁移到另一台电脑，请先停止 Streamlit，复制整个 `data/stock_analysis.db`，并放到目标项目的 `data` 文件夹。
- 当前应用只使用收盘日线和公开财务数据，不连接券商，不执行真实订单，也不构成投资建议。
- 公开行情可能有延迟、限流或接口中断。数据日期过旧或关键字段缺失时，应用会显示“数据不足/不可判断”。

## 两个账号隔离测试

1. 注册账号 A，验证邮箱，查询一只股票并加入自选股、创建模拟订单、保存交易计划和复盘。
2. 刷新页面并重新登录账号 A，确认记录仍然存在。
3. 使用无痕窗口注册账号 B，确认看不到账号 A 的任何个人记录。
4. 在 Supabase Table Editor 中确认业务表启用了 RLS；不要为了调试而关闭 RLS。
