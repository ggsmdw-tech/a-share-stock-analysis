from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import streamlit as st


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    anon_key: str


_BROWSER_STORAGE_KEY = "a_share_stock_analysis_supabase_session"


def _browser_session_changed() -> None:
    state = st.session_state.get("supabase_browser_session", {})
    if isinstance(state, dict) and isinstance(state.get("session"), dict):
        session = state["session"]
        if session.get("access_token") and session.get("refresh_token"):
            st.session_state["supabase_session"] = session


def _browser_hash_session_changed() -> None:
    state = st.session_state.get("supabase_browser_session", {})
    if not isinstance(state, dict):
        return
    session = state.get("hash_session")
    if isinstance(session, dict) and session.get("access_token") and session.get("refresh_token"):
        st.session_state["supabase_session"] = session
        st.session_state["supabase_recovery"] = session.get("type") == "recovery"


def _browser_ready_changed() -> None:
    st.session_state["supabase_browser_ready"] = True


def _component_state_value(state: Any, name: str) -> Any:
    if isinstance(state, dict):
        return state.get(name)
    try:
        return getattr(state, name, None)
    except Exception:
        return None


_BROWSER_SESSION = st.components.v2.component(
    "supabase_session_storage",
    html="""
<div data-session-root aria-hidden="true"></div>
""",
    js="""
export default function (component) {
  const { data, parentElement, setStateValue } = component
  if (!parentElement) return
  const root = typeof parentElement.querySelector === "function"
    ? parentElement.querySelector("[data-session-root]")
    : null
  if (!root) {
    setStateValue("ready", true)
    return
  }

  const storageKey = (data && data.storage_key) || "a_share_stock_analysis_supabase_session"
  const incoming = (data && data.session) || null
  const clear = Boolean(data && data.clear)
  const incomingText = JSON.stringify(incoming || null)

  if (root.dataset.incomingText !== incomingText || clear) {
    root.dataset.incomingText = incomingText
    if (clear) {
      localStorage.removeItem(storageKey)
      setStateValue("session", {})
      setStateValue("hash_session", {})
      setStateValue("ready", true)
      return
    }
    if (incoming && incoming.access_token && incoming.refresh_token) {
      localStorage.setItem(storageKey, JSON.stringify(incoming))
      setStateValue("session", incoming)
      setStateValue("ready", true)
      return
    }
  }

  if (root.dataset.restored !== "1") {
    root.dataset.restored = "1"
    const stored = localStorage.getItem(storageKey)
    if (stored) {
      try {
        const parsed = JSON.parse(stored)
        if (parsed && parsed.access_token && parsed.refresh_token) {
          setStateValue("session", parsed)
        }
      } catch (error) {
        localStorage.removeItem(storageKey)
      }
    }
    if (root.dataset.hashProcessed !== "1") {
      root.dataset.hashProcessed = "1"
      const hashText = (window.location.hash || "").replace(/^#/, "")
      const hashParams = new URLSearchParams(hashText)
      const accessToken = hashParams.get("access_token")
      const refreshToken = hashParams.get("refresh_token")
      if (accessToken && refreshToken) {
        setStateValue("hash_session", {
          access_token: accessToken,
          refresh_token: refreshToken,
          expires_at: hashParams.get("expires_at"),
          expires_in: hashParams.get("expires_in"),
          token_type: hashParams.get("token_type") || "bearer",
          type: hashParams.get("type") || ""
        })
        window.history.replaceState({}, document.title, window.location.pathname + window.location.search)
      }
    }
    setStateValue("ready", true)
  }
}
""",
)


def browser_session_storage(
    session: dict[str, Any] | None = None,
    *,
    clear: bool = False,
) -> dict[str, Any]:
    """Synchronize a short-lived Supabase session with browser local storage.

    The access and refresh tokens are kept in the browser component state and
    Streamlit session only; they are never written to application tables or
    query parameters.
    """

    result = _BROWSER_SESSION(
        data={
            "storage_key": _BROWSER_STORAGE_KEY,
            "session": session or {},
            "clear": clear,
        },
        key="supabase_browser_session",
        on_session_change=_browser_session_changed,
        on_hash_session_change=_browser_hash_session_changed,
        on_ready_change=_browser_ready_changed,
        width="stretch",
        height=1,
    )
    raw_state = st.session_state.get("supabase_browser_session", {})
    if isinstance(raw_state, dict):
        # CCv2 may expose a read-only dict-like ComponentState here.
        state = dict(raw_state)
    else:
        try:
            state = dict(raw_state)
        except (TypeError, ValueError):
            state = {}
    for name in ("session", "hash_session", "ready"):
        value = _component_state_value(result, name)
        if value is not None:
            state[name] = value
            if name == "ready" and bool(value):
                st.session_state["supabase_browser_ready"] = True
    # The CCv2 component owns this session-state key and exposes it read-only.
    # Keep the merged values local instead of writing back into the component key.
    browser_state_session = state.get("session")
    if (
        isinstance(browser_state_session, dict)
        and browser_state_session.get("access_token")
        and browser_state_session.get("refresh_token")
    ):
        st.session_state["supabase_session"] = browser_state_session
    hash_state_session = state.get("hash_session")
    if (
        isinstance(hash_state_session, dict)
        and hash_state_session.get("access_token")
        and hash_state_session.get("refresh_token")
    ):
        st.session_state["supabase_session"] = hash_state_session
        st.session_state["supabase_recovery"] = hash_state_session.get("type") == "recovery"
    return state


def get_supabase_config() -> SupabaseConfig | None:
    try:
        url = str(st.secrets.get("SUPABASE_URL", "")).strip()
        anon_key = str(st.secrets.get("SUPABASE_ANON_KEY", "")).strip()
    except Exception:
        return None
    if not url or not anon_key:
        return None
    return SupabaseConfig(url=url, anon_key=anon_key)


def _auth_redirect_url() -> str | None:
    current_url = getattr(st.context, "url", "") or ""
    parsed = urlsplit(current_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def create_supabase_client(config: SupabaseConfig):
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError(
            "线上模式缺少 supabase 包，请在 requirements.txt 中安装依赖后重新部署。"
        ) from exc
    return create_client(config.url, config.anon_key)


def session_to_dict(session: Any) -> dict[str, Any] | None:
    if session is None:
        return None
    if isinstance(session, dict):
        access_token = session.get("access_token")
        refresh_token = session.get("refresh_token")
    else:
        access_token = getattr(session, "access_token", None)
        refresh_token = getattr(session, "refresh_token", None)
    if not access_token or not refresh_token:
        return None
    payload: dict[str, Any] = {
        "access_token": str(access_token),
        "refresh_token": str(refresh_token),
    }
    for key in ("expires_at", "expires_in", "token_type"):
        value = session.get(key) if isinstance(session, dict) else getattr(session, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _clear_auth_state(*, clear_browser: bool = False) -> None:
    """Remove identity state before showing the login gate again."""
    for key in ("supabase_session", "auth_user_id", "auth_user_email"):
        st.session_state.pop(key, None)
    st.session_state["supabase_recovery"] = False
    if clear_browser:
        st.session_state["_clear_browser_session"] = True


def _response_value(response: Any, name: str) -> Any:
    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _user_value(user: Any, name: str, default: Any = None) -> Any:
    if user is None:
        return default
    if isinstance(user, dict):
        return user.get(name, default)
    return getattr(user, name, default)


def user_id_from_response(response: Any) -> str | None:
    user = _response_value(response, "user")
    value = _user_value(user, "id")
    return str(value) if value else None


def user_email(user: Any) -> str:
    return str(_user_value(user, "email", ""))


def user_is_confirmed(user: Any) -> bool:
    confirmed = _user_value(user, "email_confirmed_at") or _user_value(user, "confirmed_at")
    return bool(confirmed)


def translate_auth_error(exc: Exception) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if "invalid login credentials" in lowered:
        return "邮箱或密码不正确。"
    if "email not confirmed" in lowered or "not confirmed" in lowered:
        return "邮箱尚未验证，请先打开验证邮件完成验证。"
    if "user already registered" in lowered or "already registered" in lowered:
        return "这个邮箱已经注册，请直接登录或使用找回密码。"
    if "password" in lowered and ("short" in lowered or "weak" in lowered):
        return "密码强度不足，请使用至少8位且包含字母和数字的密码。"
    if "rate limit" in lowered or "too many" in lowered:
        return "操作过于频繁，请稍后再试。"
    return f"登录服务暂时不可用：{message or '未知错误'}"


def _set_session(client: Any, session: dict[str, Any]) -> Any:
    try:
        return client.auth.set_session(
            access_token=session["access_token"],
            refresh_token=session["refresh_token"],
        )
    except TypeError:
        return client.auth.set_session(session["access_token"], session["refresh_token"])


def _current_user(client: Any, session: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    response = _set_session(client, session)
    refreshed = _response_value(response, "session")
    refreshed_payload = session_to_dict(refreshed) or session
    user_response = client.auth.get_user()
    user = _response_value(user_response, "user")
    user_id = _user_value(user, "id")
    if not user_id:
        return None, None
    user_payload = {
        "id": str(user_id),
        "email": user_email(user),
        "email_confirmed_at": _user_value(user, "email_confirmed_at"),
    }
    return user_payload, refreshed_payload


def ensure_authenticated() -> bool:
    """Render the online auth gate and return whether the app may continue."""

    config = get_supabase_config()
    if config is None:
        st.error(
            "线上模式尚未配置 Supabase。请在 Streamlit Cloud 的 Secrets 中添加 "
            "SUPABASE_URL 和 SUPABASE_ANON_KEY。"
        )
        st.info("本地运行不需要登录；线上部署必须配置云端认证和数据库。")
        return False

    clear_browser = bool(st.session_state.pop("_clear_browser_session", False))
    try:
        browser_session_storage(
            st.session_state.get("supabase_session"), clear=clear_browser
        )
    except Exception:
        # Browser storage is only a convenience for restoring a session after
        # refresh. It must never prevent the normal login form or the main app
        # from rendering when a hosted Streamlit runtime rejects component
        # state updates.
        st.session_state["supabase_browser_ready"] = True
        if not st.session_state.get("_browser_storage_warning_shown", False):
            st.session_state["_browser_storage_warning_shown"] = True
            st.warning(
                "浏览器自动恢复登录暂时不可用；本次仍可正常登录，刷新后可能需要重新登录。",
                icon=":material/warning:",
            )
    if not st.session_state.get("supabase_browser_ready", False):
        st.info("正在尝试恢复登录状态；如果没有自动恢复，也可以直接登录。")

    try:
        client = create_supabase_client(config)
    except Exception:
        st.error("Supabase 认证服务暂时不可用，请检查项目地址和 anon key 配置。")
        return False

    session = st.session_state.get("supabase_session")
    if isinstance(session, dict) and session.get("access_token") and session.get("refresh_token"):
        try:
            user, refreshed = _current_user(client, session)
            if user and refreshed:
                st.session_state["supabase_session"] = refreshed
                st.session_state["supabase_client_config"] = config
                st.session_state["auth_user_id"] = user["id"]
                st.session_state["auth_user_email"] = user["email"]
                if not user_is_confirmed(user):
                    st.warning("邮箱尚未验证，请先打开注册邮件完成验证后再使用。")
                    if st.button("退出并重新登录", icon=":material/logout:"):
                        logout()
                    return False
                if st.session_state.get("supabase_recovery"):
                    st.title("设置新密码")
                    st.caption("请设置一个新的登录密码，完成后即可继续使用应用。")
                    with st.form("supabase_new_password"):
                        new_password = st.text_input("新密码", type="password", autocomplete="new-password")
                        new_password_again = st.text_input("再次输入新密码", type="password", autocomplete="new-password")
                        submitted = st.form_submit_button("保存新密码", type="primary", icon=":material/key:")
                    if submitted:
                        if new_password != new_password_again:
                            st.error("两次输入的密码不一致。")
                        elif len(new_password) < 8:
                            st.error("密码至少需要8位。")
                        else:
                            try:
                                response = client.auth.update_user({"password": new_password})
                                updated_session = session_to_dict(_response_value(response, "session"))
                                if updated_session:
                                    st.session_state["supabase_session"] = updated_session
                                st.session_state["supabase_recovery"] = False
                                st.success("密码已更新，请继续使用应用。")
                                st.rerun()
                            except Exception as exc:
                                st.error(translate_auth_error(exc))
                    return False
                return True
        except Exception:
            _clear_auth_state(clear_browser=True)
            st.info("登录状态已过期，请重新登录。")

    st.title("登录 A股股票分析应用")
    st.caption("登录后，你的搜索、自选股、模拟交易、交易计划和复盘会保存到云端。")
    login_tab, register_tab, reset_tab = st.tabs(["登录", "注册", "找回密码"])

    with login_tab:
        with st.form("supabase_login"):
            email = st.text_input("邮箱", autocomplete="email")
            password = st.text_input("密码", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("登录", type="primary", icon=":material/login:")
        if submitted:
            try:
                response = client.auth.sign_in_with_password({"email": email.strip(), "password": password})
                new_session = session_to_dict(_response_value(response, "session"))
                user = _response_value(response, "user")
                if not new_session or not user:
                    raise RuntimeError("登录成功但没有取得有效会话，请稍后重试。")
                if not user_is_confirmed(user):
                    st.warning("邮箱尚未验证，请先打开验证邮件完成验证。")
                else:
                    st.session_state["supabase_session"] = new_session
                    st.session_state["supabase_recovery"] = False
                    st.rerun()
            except Exception as exc:
                st.error(translate_auth_error(exc))

    with register_tab:
        with st.form("supabase_register"):
            email = st.text_input("注册邮箱", autocomplete="email")
            password = st.text_input("设置密码", type="password", autocomplete="new-password")
            password_again = st.text_input("再次输入密码", type="password", autocomplete="new-password")
            submitted = st.form_submit_button("注册", icon=":material/person_add:")
        if submitted:
            if password != password_again:
                st.error("两次输入的密码不一致。")
            elif len(password) < 8:
                st.error("密码至少需要8位。")
            else:
                try:
                    options = {}
                    redirect_url = _auth_redirect_url()
                    if redirect_url:
                        options["email_redirect_to"] = redirect_url
                    payload = {"email": email.strip(), "password": password}
                    if options:
                        payload["options"] = options
                    response = client.auth.sign_up(payload)
                    new_session = session_to_dict(_response_value(response, "session"))
                    user = _response_value(response, "user")
                    if new_session and user and user_is_confirmed(user):
                        st.session_state["supabase_session"] = new_session
                        st.rerun()
                    st.success("注册成功，请打开邮箱中的验证链接，验证后再登录。")
                except Exception as exc:
                    st.error(translate_auth_error(exc))

    with reset_tab:
        with st.form("supabase_reset_password"):
            email = st.text_input("注册邮箱", autocomplete="email")
            submitted = st.form_submit_button("发送找回邮件", icon=":material/forward_to_inbox:")
        if submitted:
            try:
                redirect_url = _auth_redirect_url()
                if redirect_url:
                    try:
                        client.auth.reset_password_for_email(email.strip(), {"redirect_to": redirect_url})
                    except TypeError:
                        client.auth.reset_password_for_email(email.strip())
                else:
                    client.auth.reset_password_for_email(email.strip())
                st.success("如果该邮箱已注册，系统会发送密码重置邮件，请检查收件箱。")
            except Exception as exc:
                st.error(translate_auth_error(exc))

    return False


def logout() -> None:
    config = get_supabase_config()
    if config is not None:
        try:
            create_supabase_client(config).auth.sign_out()
        except Exception:
            pass
    _clear_auth_state(clear_browser=True)
    st.rerun()


def current_user_email() -> str:
    return str(st.session_state.get("auth_user_email", ""))


def active_supabase_client():
    config = get_supabase_config()
    session = st.session_state.get("supabase_session")
    if config is None or not isinstance(session, dict):
        raise RuntimeError("Supabase 登录会话不可用，请重新登录。")
    client = create_supabase_client(config)
    _set_session(client, session)
    return client
