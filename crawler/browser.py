"""Selenium 驱动的创建与页面通用操作。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    SessionNotCreatedException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 站点用的是 Klaro 隐私弹窗，不关掉会挡住 Download 按钮
CONSENT_ACCEPT = "button.cm-btn-success"
CONSENT_OVERLAY = ".cm-bg, .cookie-notice, .cn-ok"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


DRIVER_HINT = """
无法启动 Chrome。Selenium Manager 需要访问 googlechromelabs.github.io 才能下载匹配的
chromedriver，网络不通时它会退回用缓存里的旧版本，就会出现版本对不上的报错。

解决办法（任选其一）：
  1) 手动下载与本机 Chrome 大版本一致的 chromedriver，然后指定路径：
       uv run pdl-crawl --driver-path "C:\\tools\\chromedriver.exe"
     或设环境变量 PDL_CHROMEDRIVER。
     国内可用镜像：https://registry.npmmirror.com/binary.html?path=chrome-for-testing/
     选与 Chrome 同大版本的目录，下 chromedriver-win64.zip 解压即可。
  2) 让 Selenium Manager 能联网（挂代理），并清掉旧缓存后重试：
       Windows: rmdir /s /q "%USERPROFILE%\\.cache\\selenium"
       Linux:   rm -rf ~/.cache/selenium
"""


def make_driver(
    download_dir: Path,
    headless: bool = True,
    page_load_timeout: int = 120,
    driver_path: str | None = None,
    browser_binary: str | None = None,
):
    """创建 Chrome 驱动。

    默认交给 Selenium Manager 自动准备 chromedriver；它需要联网，网络不通时
    会退回缓存里的旧版本并跟本机 Chrome 版本冲突 —— 这种环境下用 driver_path
    显式指定即可（也可通过 PDL_CHROMEDRIVER / PDL_CHROME_BINARY 环境变量给）。
    """
    driver_path = driver_path or os.environ.get("PDL_CHROMEDRIVER")
    browser_binary = browser_binary or os.environ.get("PDL_CHROME_BINARY")

    download_dir.mkdir(parents=True, exist_ok=True)
    opts = Options()
    if browser_binary:
        opts.binary_location = browser_binary
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,1200")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            # 不要用内置阅读器打开 PDF，否则点 Download 只会在标签页里预览
            "plugins.always_open_pdf_externally": True,
        },
    )
    service = Service(executable_path=driver_path) if driver_path else None
    try:
        driver = webdriver.Chrome(options=opts, service=service)
    except SessionNotCreatedException as exc:
        raise SessionNotCreatedException(f"{exc.msg}\n{DRIVER_HINT}") from exc
    driver.set_page_load_timeout(page_load_timeout)
    # headless 模式下 prefs 有时不生效，用 CDP 再钉一次下载目录
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)}
        )
    except Exception:
        pass
    return driver


def dismiss_consent(driver, timeout: float = 0.0) -> bool:
    """关闭隐私弹窗。接受后站点会写 cookie，同一 session 内通常只需一次。"""
    try:
        if timeout:
            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, CONSENT_ACCEPT))
            )
        btn = driver.find_element(By.CSS_SELECTOR, CONSENT_ACCEPT)
    except (NoSuchElementException, TimeoutException):
        return False
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)
    # 等遮罩淡出
    for _ in range(20):
        if not driver.find_elements(By.CSS_SELECTOR, CONSENT_OVERLAY):
            break
        time.sleep(0.1)
    return True


def goto(driver, url: str, wait_for: str, timeout: int = 60) -> None:
    """打开页面并等待 Angular 把关键元素渲染出来。"""
    driver.get(url)
    dismiss_consent(driver)
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, wait_for))
    )
    dismiss_consent(driver)
