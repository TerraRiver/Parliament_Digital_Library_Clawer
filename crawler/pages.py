"""列表页与详情页的解析。

DSpace 前端是 Angular，DOM 层级很深；这里用一次 execute_script 把整块数据
取回来，而不是几百次 find_element 往返 —— 单条目详情页有 200 个字段容器，
逐个取会慢一个数量级。
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .browser import dismiss_consent, goto

RESULT_SELECTOR = "ds-item-search-result-list-element"
ITEM_READY_SELECTOR = "ds-metadata-field-wrapper"
FILE_ROW_SELECTOR = "div.row.py-2 .name-file"
DOWNLOAD_BTN = "button.link-download-doc"

# 文件行渲染延迟抖动很大，等到数量稳定再读；真没有文件的条目会一直等到超时
_FILE_WAIT_MIN = 1.5       # 至少等这么久才开始认稳定，太快退出会漏掉慢加载的行
_FILE_WAIT_STABLE_POLLS = 3
_FILE_WAIT_INTERVAL = 0.3
_FILE_WAIT_TIMEOUT = 12.0

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# 列表页：每条结果取 uuid / 标题 / 年份 / 类型 / 命中次数
_JS_RESULTS = """
return Array.from(document.querySelectorAll(arguments[0])).map(el => {
  const a = el.querySelector('a.item-list-title');
  const cols = el.querySelectorAll('.row > div');
  const kw = el.querySelector('.keyword-count');
  const type = el.querySelector('.item-type');
  return {
    href: a ? a.href : null,
    title: a ? a.textContent.trim() : null,
    year: cols.length ? cols[0].textContent.trim() : null,
    item_type: type ? type.textContent.trim() : null,
    keyword_count: kw ? kw.textContent.replace(/[^0-9]/g, '') : null,
  };
});
"""

# 详情页：字段容器（空字段带 d-none，跳过）+ 文件表格
_JS_ITEM = """
const fields = {};
document.querySelectorAll('ds-metadata-field-wrapper div.wrap-row').forEach(row => {
  if (row.classList.contains('d-none')) return;
  const t = row.querySelector('.title-col');
  const d = row.querySelector('.desc-col');
  if (!t || !d) return;
  const label = t.textContent.trim();
  if (!label) return;
  const values = Array.from(d.querySelectorAll(':scope > div > span'))
    .map(s => s.textContent.trim()).filter(Boolean);
  const text = d.innerText.trim();
  if (!values.length && !text) return;
  fields[label] = values.length ? values : [text];
});
const files = Array.from(document.querySelectorAll('div.row.py-2')).map(row => {
  const n = row.querySelector('.name-file');
  const s = row.querySelector('.size-file');
  const btn = row.querySelector('ds-file-download-link button');
  if (!n || !btn) return null;
  return {name: n.textContent.trim(), size_text: s ? s.textContent.trim() : null};
}).filter(Boolean);
return {fields, files, title: document.title};
"""


def build_page_url(search_url: str, page: int, page_size: int) -> str:
    """把 spc.page / spc.rpp 塞进搜索 URL（DSpace 的分页参数）。"""
    parts = urlsplit(search_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["spc.page"] = str(page)
    params["spc.rpp"] = str(page_size)
    return urlunsplit(parts._replace(query=urlencode(params)))


def scrape_list_page(driver, search_url: str, page: int, page_size: int, settle: float = 1.0) -> list[dict[str, Any]]:
    """抓一页搜索结果。返回空列表表示这一页没有结果（翻到底了）。"""
    url = build_page_url(search_url, page, page_size)
    driver.get(url)
    dismiss_consent(driver)
    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, f"{RESULT_SELECTOR}, ds-search-results"))
        )
    except TimeoutException:
        return []
    dismiss_consent(driver)

    # 结果卡片是异步渲染的，等到数量稳定为止
    prev, stable = -1, 0
    for _ in range(40):
        count = len(driver.find_elements(By.CSS_SELECTOR, RESULT_SELECTOR))
        if count and count == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = count
        time.sleep(0.5)
    time.sleep(settle)

    rows = driver.execute_script(_JS_RESULTS, RESULT_SELECTOR)
    out = []
    for row in rows:
        uuid = _uuid_from_href(row.get("href") or "")
        if not uuid:
            continue
        out.append(
            {
                "uuid": uuid,
                "url": row["href"],
                "title": row.get("title"),
                "year": row.get("year"),
                "item_type": row.get("item_type"),
                "keyword_count": _to_int(row.get("keyword_count")),
            }
        )
    return out


def scrape_item_page(driver, item_url: str) -> dict[str, Any]:
    """打开详情页（等价于点 View），抓字段与文件清单。

    ds-item-page-file-section 这个容器本身出现得早，但里面的文件行是另一次
    异步请求（拿 bitstream 列表）填进去的，实测延迟在 0.5~3s 之间抖动很大。
    之前固定 sleep(0.6) 经常抢跑，把还没渲染出来的文件列表当成"无附件"
    （实测抽样误判率 97.5%）。这里改成等文件行数量稳定下来再读，真没有文件
    的条目最多等 _FILE_WAIT_TIMEOUT 秒兜底。
    """
    goto(driver, item_url, ITEM_READY_SELECTOR, timeout=60)
    _wait_for_files_settled(driver)
    data = driver.execute_script(_JS_ITEM)
    for f in data["files"]:
        f["size_bytes_expected"] = parse_size(f.get("size_text"))
    return data


def _wait_for_files_settled(driver) -> None:
    """等文件行数量稳定下来（不管稳不稳定在 0 还是 N）再返回。"""
    start = time.monotonic()
    prev, stable = -1, 0
    while time.monotonic() - start < _FILE_WAIT_TIMEOUT:
        count = driver.execute_script(
            "return document.querySelectorAll(arguments[0]).length", FILE_ROW_SELECTOR
        )
        stable = stable + 1 if count == prev else 0
        prev = count
        if time.monotonic() - start >= _FILE_WAIT_MIN and stable >= _FILE_WAIT_STABLE_POLLS:
            return
        time.sleep(_FILE_WAIT_INTERVAL)


def find_download_buttons(driver):
    return driver.find_elements(By.CSS_SELECTOR, DOWNLOAD_BTN)


def parse_size(text: str | None) -> int | None:
    """'39.07 MB' -> 40969011（页面显示的是四舍五入值，只用于粗校验）。"""
    if not text:
        return None
    m = re.match(r"\s*([\d.,]+)\s*([KMGT]?B)\s*$", text, re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")) * _SIZE_UNITS[m.group(2).upper()])
    except (ValueError, KeyError):
        return None


def _uuid_from_href(href: str) -> str | None:
    m = re.search(r"/items/([0-9a-f-]{36})", href)
    return m.group(1) if m else None


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
