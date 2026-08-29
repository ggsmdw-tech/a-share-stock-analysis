# 分享和部署

当前版本：v0.01 内部测试版。对外分享前，建议先由内部测试人员核对数据日期、评分依据和模拟交易流程。

当前 `http://localhost:8501/` 只对运行程序的电脑有效，不能直接发给别人。要让别人打开网址就能使用，推荐部署到 Streamlit Community Cloud；要把程序文件发给别人，则使用项目里的 Windows 一键启动脚本。

## 方式一：发布一个公共网址

1. 在 GitHub 新建一个仓库，把本项目上传。不要上传 `.venv`、`data/*.db`、`.pytest_cache` 或 `.streamlit/secrets.toml`。
2. 打开 [Streamlit Community Cloud](https://share.streamlit.io/)，使用 GitHub 登录并选择这个仓库。
3. 创建应用时选择主分支，入口文件填写 `app.py`，然后点击部署。
4. 部署成功后会得到一个 `streamlit.app` 网址，把这个网址发给别人即可。

仓库已经包含 `requirements.txt`，云端会自动安装依赖。公开模式请求腾讯真实行情，AKShare作为备用；云端主机如果无法访问这些公开接口，页面会显示数据不足，不会生成虚构价格。

## 方式二：发送一个 Windows 分享包

在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\package_for_sharing.ps1
```

把生成的 ZIP 发给对方。对方解压后双击 `start_app.bat`，脚本会创建虚拟环境、安装依赖、启动应用并打开浏览器。

这种方式要求对方安装 Python 3.12，并允许 Python 访问互联网。若对方电脑没有 Python，公共网址方式更方便；本项目没有把 Python 和全部依赖打包进仓库。

## 多人使用须知

- 模拟交易账户按浏览器会话隔离，不同访问者不会共用同一个账户。
- Streamlit Cloud 的本地 SQLite 文件不适合永久保存多人数据；应用重启或重新部署后，模拟交易记录可能丢失。
- 当前应用只使用收盘日线和公开财务数据，不连接券商，不执行真实订单，也不构成投资建议。
- 公开行情可能有延迟、限流或接口中断。数据日期过旧或关键字段缺失时，应用会显示“数据不足/不可判断”。
