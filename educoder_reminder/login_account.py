"""头歌账号自动化登录：Playwright 系统 Edge 有头登录，抓取 _educoder_session cookie、username、user_id。

复用 homework-reminder/refresh_cookies.py 的方案：
- 用系统 Edge(channel=msedge) + 有头(headless=False) + 窗口移到屏外(--window-position=-32000,-32000)，
  规避无头浏览器被平台风控拒绝的问题。
- 登录后从浏览器 cookie 提取 _educoder_session(+autologin_trustie)，其值即 Pc-Authorization token。
- username(user login) 与 user_id 从登录后页面 localStorage 的 userInfo 解析（上传接口 zzud 参数需要）。

用法：
    from educoder_reminder.login_account import login_get_cookie
    cookie, username, user_id = login_get_cookie('手机号', '密码')
    # cookie 形如 "_educoder_session=xxx; autologin_trustie=xxx"
    # username 如 "pl9jzvhaf"，user_id 如 2469337
"""
from __future__ import annotations

import json
import re

from playwright.sync_api import sync_playwright

EDUCODER_COOKIE_NAMES = ["autologin_trustie", "_educoder_session"]
HOME = "https://www.educoder.net/"


def cookies_to_str(cookies, names) -> str:
    """浏览器 cookie 列表 → 请求 Cookie 字符串，按名去重（后写覆盖）。"""
    seen = {}
    for c in cookies:
        if c["name"] in names:
            seen[c["name"]] = c["value"]
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def _extract_user_from_storage(ctx) -> tuple[str | None, int | None]:
    """从任一页面 localStorage 的 userInfo 解析 (login, user_id)。"""
    for pg in ctx.pages:
        try:
            raw = pg.evaluate("() => localStorage.getItem('userInfo')")
            if raw:
                d = json.loads(raw)
                return d.get("login"), d.get("user_id")
        except Exception:
            continue
    return None, None


def _extract_username_from_url(ctx) -> str | None:
    for pg in ctx.pages:
        try:
            m = re.search(r"/users/([A-Za-z0-9_-]+)", pg.url)
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


def login_get_cookie(phone: str, password: str, timeout_ms: int = 45000) -> tuple[str, str | None, int | None]:
    """有头登录头歌，返回 (cookie_str, username, user_id)。失败抛 RuntimeError。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=False,
            args=["--window-position=-32000,-32000"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(HOME, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)
            if page.locator('a[href*="/messages/"]').count() == 0:
                page.get_by_text("登录 / 注册").first.click(force=True)
                page.wait_for_timeout(1000)
                page.get_by_placeholder("请输入有效的手机号/邮箱号/账号").fill(phone)
                page.get_by_placeholder("密码").fill(password)
                # 勾选协议/自动登录 checkbox（登录需要）
                for cb in page.locator("input[type=checkbox]").all():
                    try:
                        if not cb.is_checked():
                            cb.check(force=True)
                    except Exception:
                        pass
                page.wait_for_timeout(500)
                page.get_by_role("button", name="登录").click(force=True)
                # 等待登录完成：轮询 _educoder_session cookie 出现
                for _ in range(timeout_ms // 1000):
                    page.wait_for_timeout(1000)
                    if any(c["name"] == "_educoder_session" for c in ctx.cookies()):
                        break
                else:
                    raise RuntimeError("登录未成功：Cookie 里未出现 _educoder_session（可能账号密码错误或需验证）。")
            # 等待 userInfo 写入 localStorage
            username, user_id = None, None
            for _ in range(10):
                username, user_id = _extract_user_from_storage(ctx)
                if username:
                    break
                page.wait_for_timeout(1000)
            cookie = cookies_to_str(ctx.cookies(), EDUCODER_COOKIE_NAMES)
            if "_educoder_session" not in cookie:
                raise RuntimeError("登录后 Cookie 里没有 _educoder_session。")
            if not username:
                username = _extract_username_from_url(ctx)
            return cookie, username, user_id
        finally:
            page.close()
            browser.close()
