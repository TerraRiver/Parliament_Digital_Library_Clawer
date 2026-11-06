"""列表页与详情页的解析。

DSpace 前端是 Angular，DOM 层级很深；这里用一次 page.evaluate 把整块数据
取回来，而不是几百次 DOM 查询往返 —— 单条目详情页有 200 个字段容器，
逐个取会慢一个数量级。
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .browser import dismiss_consent, goto
from .stealth import human_idle_reading_gesture, human_page_scan_scroll, human_reset_gaze

RESULT_SELECTOR = "ds-item-search-result-list-element"
ITEM_READY_SELECTOR = "ds-metadata-field-wrapper"
FILE_ROW_SELECTOR = "div.row.py-2:has(.name-file):has(ds-file-download-link button)"
DOWNLOAD_BTN = f"{FILE_ROW_SELECTOR} ds-file-download-link button"

# 文件行渲染延迟抖动很大，等到数量稳定再读；真没有文件的条目会一直等到超时
_FILE_WAIT_MIN = 1.5       # 至少等这么久才开始认稳定，太快退出会漏掉慢加载的行
_FILE_WAIT_STABLE_POLLS = 3
_FILE_WAIT_INTERVAL = 0.3
_FILE_WAIT_TIMEOUT = 12.0

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# 列表页：每条结果取 uuid / 标题 / 年份 / 类型 / 命中次数
_JS_RESULTS = """
(selector) => Array.from(document.querySelectorAll(selector)).map(el => {
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
})
"""

# 列表页状态检测：从 ds-pagination 提取总条数，并检测加载动画与明确的无结果提示
_JS_LIST_STATUS = r"""
() => {
  const sr = document.querySelector('ds-search-results, ds-themed-search-results');
  const loading = Boolean(
    sr
      ? sr.querySelector('ds-loading, ds-themed-loading, .spinner-border')
      : document.querySelector('ds-loading, ds-themed-loading, .spinner-border')
  );
  const pagEl = sr ? sr.querySelector('ds-pagination, .pagination-info') : null;
  const pagText = (pagEl ? pagEl.innerText : (sr ? sr.innerText : '')) || '';
  const srText = (sr && sr.innerText) ? sr.innerText : '';
  const m = pagText.match(/showing\s+[\d,]+\s*[-–]\s*[\d,]+\s+of\s+([\d,]+)/i);
  const total = m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
  const noResult = /your\s+search\s+returned\s+no\s+results|no\s+results\s+found|(?<![\d,])0\s+results\b/i.test(srText);
  return {
    loading,
    total: Number.isFinite(total) ? total : null,
    no_result: noResult,
  };
}
"""

# 详情页：字段容器（空字段带 d-none，跳过）+ 文件表格
_JS_ITEM = """
() => {
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
}
"""

# 与 _JS_ITEM 里 n && btn 判断口径一致，用来判断"文件行是否渲染完整"
_JS_FILE_COMPLETE_COUNT = """
() => Array.from(document.querySelectorAll('div.row.py-2')).filter(row =>
  row.querySelector('.name-file') && row.querySelector('ds-file-download-link button')
).length
"""


def build_page_url(search_url: str, page: int, page_size: int) -> str:
    """把 spc.page / spc.rpp 塞进搜索 URL（保留同名多值筛选参数）。"""
    parts = urlsplit(search_url)
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("spc.page", "spc.rpp")
    ]
    pairs.append(("spc.page", str(page)))
    pairs.append(("spc.rpp", str(page_size)))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def _is_list_page_eof(status: dict[str, Any], page: int, page_size: int) -> bool:
    """判断列表页是否明确翻到底（无结果）。

    有 total 数字时以数字为准（优先级高于 no_result）；
    若因加载慢导致 total=None 且无 no_result 提示，返回 False 以便外层抛错触发重试。
    """
    if status.get("loading"):
        return False
    total = status.get("total")
    if isinstance(total, int) and page >= 1 and page_size >= 1:
        return (page - 1) * page_size >= total
    return bool(status.get("no_result"))


def scrape_list_page(
    driver, search_url: str, page: int, page_size: int, settle: float = 1.0
) -> tuple[list[dict[str, Any]], int | None]:
    """抓一页搜索结果，返回 (rows, total_expected)。

    返回空列表仅表示明确翻到底了；若因网络慢或渲染超时未拿到卡片，抛出异常交由重试机制处理。
    """
    url = build_page_url(search_url, page, page_size)
    driver.goto(url, wait_until="domcontentloaded")
    dismiss_consent(driver)
    driver.wait_for_selector(f"{RESULT_SELECTOR}, ds-search-results", state="attached", timeout=60_000)
    dismiss_consent(driver)

    # 结果卡片是异步渲染的，等到数量稳定为止
    prev, stable = -1, 0
    locator = driver.locator(RESULT_SELECTOR)
    for _ in range(40):
        count = locator.count()
        if count and count == prev:
            stable += 1
            if stable >= 2:
                break
        elif count == 0:
            status = driver.evaluate(_JS_LIST_STATUS)
            if _is_list_page_eof(status, page, page_size):
                break
            stable = 0
        else:
            stable = 0
        prev = count
        driver.wait_for_timeout(500)

    if settle > 0 and prev > 0:
        human_page_scan_scroll(driver, target_distance=360.0)
        human_reset_gaze(driver)

    status = driver.evaluate(_JS_LIST_STATUS)
    total = status.get("total") if isinstance(status.get("total"), int) else None

    rows = driver.evaluate(_JS_RESULTS, RESULT_SELECTOR)
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

    if not out:
        if _is_list_page_eof(status, page, page_size):
            return [], total
        raise RuntimeError(
            f"第 {page} 页未渲染出结果卡片（total={total}, "
            f"loading={status.get('loading')}, no_result={status.get('no_result')}）"
        )

    return out, total


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
    data = driver.evaluate(_JS_ITEM)
    for f in data["files"]:
        f["size_bytes_expected"] = parse_size(f.get("size_text"))
    return data


def _wait_for_files_settled(driver) -> None:
    """等文件行数量稳定下来再返回。

    只数 .name-file 是不够的：真正算作"文件"还需要 ds-file-download-link
    按钮也渲染出来（见 _JS_ITEM 里 n && btn 的判断），这两者是两次独立的异步
    渲染，文件名经常先于按钮出现。之前只看 .name-file 计数，会在按钮还没
    渲染出来时就误判"稳定"提前退出。这里改成数完整行（名字+按钮都在）。

    另外，稳定在 0 不能提前退出 —— 真没有文件的条目必须等满
    _FILE_WAIT_TIMEOUT 才能下结论，否则会把"异步请求还没回来"当成"没有文件"。
    """
    start = time.monotonic()
    human_idle_reading_gesture(
        driver, driver.locator(ITEM_READY_SELECTOR).first, min_pause_ms=100, max_pause_ms=250
    )
    prev, stable = -1, 0
    interval_ms = int(_FILE_WAIT_INTERVAL * 1000)
    while time.monotonic() - start < _FILE_WAIT_TIMEOUT:
        count = driver.evaluate(_JS_FILE_COMPLETE_COUNT)
        stable = stable + 1 if count == prev else 0
        prev = count
        if count > 0 and time.monotonic() - start >= _FILE_WAIT_MIN and stable >= _FILE_WAIT_STABLE_POLLS:
            return
        driver.wait_for_timeout(interval_ms)


def find_download_buttons(driver):
    """返回与 _JS_ITEM 过滤口径严格一致的文件下载按钮 Locator。"""
    return driver.locator(DOWNLOAD_BTN)


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
