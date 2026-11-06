"""Playwright 浏览器会话的创建与页面通用操作。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .stealth import STEALTH_INIT_SCRIPT, human_move_and_click, human_reset_gaze

# 站点用的是 Klaro 隐私弹窗，不关掉会挡住 Download 按钮
CONSENT_ACCEPT = "button.cm-btn-success"
CONSENT_OVERLAY = ".cm-bg, .cookie-notice, .cn-ok"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

BROWSER_HINT = """
无法启动浏览器。Playwright 默认使用自带的 Chromium，未安装时会尝试调用本机 Chrome。

解决办法（任选其一）：
  1) 安装 Playwright 匹配的 Chromium（推荐）：
       uv run playwright install chromium
  2) 安装标准版 Google Chrome，或手动指定本机浏览器可执行文件路径：
       uv run pdl-crawl --chrome-binary "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
     或设环境变量 PDL_CHROME_BINARY。
"""

_DRIVER_PATH_WARNED = False


class BrowserSession:
    """包装 Playwright / Browser / BrowserContext / Page 生命周期，对外暴露与原 driver 兼容的接口。"""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
    ) -> None:
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.page = page
        self._pdl_session = None

    def __getattr__(self, name: str):
        return getattr(self.page, name)

    def quit(self) -> None:
        if self._pdl_session is not None:
            try:
                self._pdl_session.close()
            except Exception:
                pass
            self._pdl_session = None
        for closer in (self.page.close, self.context.close, self.browser.close, self.playwright.stop):
            try:
                closer()
            except Exception:
                pass


def make_driver(
    download_dir: Path,
    headless: bool = True,
    page_load_timeout: int = 120,
    driver_path: str | None = None,
    browser_binary: str | None = None,
) -> BrowserSession:
    """创建 Playwright Chromium 会话。

    优先使用 --chrome-binary / PDL_CHROME_BINARY；未指定时默认启动 Playwright 自带的
    Chromium，若尚未执行 `playwright install chromium`，自动回退尝试本机安装的 Chrome。
    """
    global _DRIVER_PATH_WARNED
    if (driver_path or os.environ.get("PDL_CHROMEDRIVER")) and not _DRIVER_PATH_WARNED:
        print("[driver] 提示：已迁移至 Playwright，不再需要 chromedriver，忽略 --driver-path / PDL_CHROMEDRIVER")
        _DRIVER_PATH_WARNED = True

    browser_binary = browser_binary or os.environ.get("PDL_CHROME_BINARY")
    download_dir.mkdir(parents=True, exist_ok=True)

    launch_args = ["--disable-gpu", "--disable-blink-features=AutomationControlled"]
    if sys.platform.startswith("linux"):
        launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])

    launch_kwargs = {
        "headless": headless,
        "args": launch_args,
        "downloads_path": str(download_dir),
        # 交由 crawler/run.py 的信号处理器实现"完成当前条目后安全退出"
        "handle_sigint": False,
        "handle_sigterm": False,
        "handle_sighup": False,
    }

    pw = sync_playwright().start()
    browser: Browser | None = None
    try:
        if browser_binary:
            try:
                browser = pw.chromium.launch(executable_path=browser_binary, **launch_kwargs)
            except PlaywrightError as exc:
                raise PlaywrightError(f"{exc}\n{BROWSER_HINT}") from exc
        else:
            try:
                browser = pw.chromium.launch(**launch_kwargs)
            except PlaywrightError as first_exc:
                try:
                    browser = pw.chromium.launch(channel="chrome", **launch_kwargs)
                except PlaywrightError as second_exc:
                    raise PlaywrightError(
                        f"自带 Chromium 启动失败：{first_exc}\n"
                        f"本机 Chrome 启动失败：{second_exc}\n"
                        f"{BROWSER_HINT}"
                    ) from second_exc

        context = browser.new_context(
            viewport={"width": 1600, "height": 1200},
            user_agent=USER_AGENT,
            accept_downloads=True,
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()
        page.set_default_navigation_timeout(page_load_timeout * 1000)
        page.set_default_timeout(page_load_timeout * 1000)
        return BrowserSession(pw, browser, context, page)
    except BaseException:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            pw.stop()
        except Exception:
            pass
        raise


def dismiss_consent(driver: BrowserSession | Page, timeout: float = 0.0) -> bool:
    """关闭隐私弹窗。接受后站点会写 cookie，同一 session 内通常只需一次。"""
    btn = driver.locator(CONSENT_ACCEPT).first
    try:
        if timeout > 0:
            btn.wait_for(state="visible", timeout=timeout * 1000)
        elif btn.count() == 0 or not btn.is_visible():
            return False
    except (PlaywrightTimeoutError, PlaywrightError):
        return False

    try:
        human_move_and_click(driver, btn, timeout=2_000, scroll_if_needed=False)
    except PlaywrightError:
        try:
            btn.evaluate("el => el.click()", timeout=2_000)
        except PlaywrightError:
            return False

    # 等遮罩淡出（若点击未生效，半途补一次 JS click 兜底）
    overlay = driver.locator(CONSENT_OVERLAY)
    cleared = False
    for i in range(20):
        try:
            if overlay.count() == 0:
                cleared = True
                break
            if i == 8:
                btn.evaluate("el => el.click()", timeout=1_000)
        except PlaywrightError:
            break
        driver.wait_for_timeout(100)
    human_reset_gaze(driver)
    return cleared


def goto(driver: BrowserSession | Page, url: str, wait_for: str, timeout: int = 60) -> None:
    """打开页面并等待 Angular 把关键元素渲染出来。"""
    driver.goto(url, wait_until="domcontentloaded")
    dismiss_consent(driver)
    driver.wait_for_selector(wait_for, state="attached", timeout=timeout * 1000)
    dismiss_consent(driver)
