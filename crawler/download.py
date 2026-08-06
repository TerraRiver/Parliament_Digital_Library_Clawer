"""文件下载。

两种模式：
  click —— 真的去点页面上的 Download 按钮，由 Chrome 完成下载（默认，行为与人工一致）
  api   —— 从 DSpace REST 接口取 bitstream 直链，用 requests 流式下载
            （支持断点续传的 Range 请求，大文件更稳、更快）
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests

from .browser import USER_AGENT
from .pages import find_download_buttons

PARTIAL_SUFFIXES = (".crdownload", ".tmp", ".part")


class DownloadError(RuntimeError):
    pass


# ---------------------------------------------------------------- 公共入口


def download_item_files(
    driver,
    *,
    item: dict[str, Any],
    files: list[dict[str, Any]],
    dest_dir: Path,
    staging_dir: Path,
    mode: str,
    min_speed: float,
    min_timeout: float,
    stall_timeout: float,
    api_base: str,
) -> list[dict[str, Any]]:
    """下载一个条目下的全部文件，返回带落盘信息的文件记录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, meta in enumerate(files):
        name = safe_filename(meta["name"], max_len=_name_budget(dest_dir))
        target = dest_dir / name
        expected = meta.get("size_bytes_expected")

        if _already_ok(target, expected):
            results.append({**meta, "path": str(target), "bytes": target.stat().st_size, "skipped": True})
            continue

        if mode == "api":
            got = _download_via_api(driver, item["uuid"], meta["name"], target, api_base)
        else:
            got = _download_via_click(
                driver,
                index=index,
                target=target,
                staging_dir=staging_dir,
                expected=expected,
                min_speed=min_speed,
                min_timeout=min_timeout,
                stall_timeout=stall_timeout,
            )
        results.append({**meta, "path": str(target), "bytes": got, "skipped": False})
    return results


def _already_ok(target: Path, expected: int | None) -> bool:
    if not target.exists() or target.stat().st_size == 0:
        return False
    if expected is None:
        return True
    # 页面上的体积是四舍五入到两位小数的，留 2% 容差
    return abs(target.stat().st_size - expected) <= max(expected * 0.02, 4096)


# ---------------------------------------------------------------- click 模式


def _download_via_click(
    driver,
    *,
    index: int,
    target: Path,
    staging_dir: Path,
    expected: int | None,
    min_speed: float,
    min_timeout: float,
    stall_timeout: float,
) -> int:
    staging_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir(staging_dir)

    buttons = find_download_buttons(driver)
    if index >= len(buttons):
        raise DownloadError(f"页面上只有 {len(buttons)} 个下载按钮，取不到第 {index + 1} 个")
    btn = buttons[index]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)

    timeout = max(min_timeout, (expected or 0) / max(min_speed, 1))
    finished = _wait_for_download(staging_dir, timeout=timeout, stall_timeout=stall_timeout)

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(finished), str(target))
    return target.stat().st_size


def _wait_for_download(staging_dir: Path, *, timeout: float, stall_timeout: float) -> Path:
    """等 Chrome 把文件下完：先出现 .crdownload，写完后重命名成正式文件。"""
    deadline = time.monotonic() + timeout
    last_size, last_progress = -1, time.monotonic()

    while time.monotonic() < deadline:
        files = [p for p in staging_dir.iterdir() if p.is_file()]
        done = [p for p in files if not p.name.endswith(PARTIAL_SUFFIXES)]
        partial = [p for p in files if p.name.endswith(PARTIAL_SUFFIXES)]

        if done and not partial:
            return max(done, key=lambda p: p.stat().st_mtime)

        size = sum(p.stat().st_size for p in files)
        if size != last_size:
            last_size, last_progress = size, time.monotonic()
        elif files and time.monotonic() - last_progress > stall_timeout:
            raise DownloadError(f"下载卡住：{stall_timeout:.0f}s 内体积没有增长（已收 {size} 字节）")
        time.sleep(0.5)

    raise DownloadError(f"下载超时（{timeout:.0f}s）")


def _clear_dir(path: Path) -> None:
    for p in path.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)
        else:
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- api 模式


def _session_from_driver(driver, api_base: str) -> requests.Session:
    """复用同一个 driver 上的 session，保持连接池和 cookie。"""
    s = getattr(driver, "_pdl_session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        s.headers["Referer"] = api_base.rsplit("/server", 1)[0] + "/"
        driver._pdl_session = s
    for c in driver.get_cookies():
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def _with_http_retries(fn, attempts: int = 3, what: str = "请求"):
    """网络抖动（SSL EOF、连接重置）很常见，就地重试几次，不浪费条目的重试额度。"""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (requests.RequestException, OSError) as exc:
            last = exc
            if i < attempts:
                time.sleep(min(2**i, 15))
    raise DownloadError(f"{what}重试 {attempts} 次仍失败：{type(last).__name__}: {last}") from last


def _download_via_api(driver, uuid: str, filename: str, target: Path, api_base: str) -> int:
    s = _session_from_driver(driver, api_base)
    url = _with_http_retries(
        lambda: _resolve_bitstream_url(s, api_base, uuid, filename), what="解析 bitstream"
    )
    return _with_http_retries(lambda: _stream_download(s, url, target), what="下载文件")


def _resolve_bitstream_url(s: requests.Session, api_base: str, uuid: str, filename: str) -> str:
    r = s.get(f"{api_base}/core/items/{uuid}/bundles", params={"embed": "bitstreams"}, timeout=60)
    r.raise_for_status()
    bundles = r.json().get("_embedded", {}).get("bundles", [])
    candidates: list[tuple[str, str]] = []
    for b in bundles:
        if b.get("name") != "ORIGINAL":
            continue
        for bs in b.get("_embedded", {}).get("bitstreams", {}).get("_embedded", {}).get("bitstreams", []):
            candidates.append((bs.get("name", ""), bs["_links"]["content"]["href"]))
    for name, href in candidates:
        if name == filename:
            return href
    if candidates:
        return candidates[0][1]
    raise DownloadError(f"REST 接口里找不到 {filename} 对应的 bitstream")


def _stream_download(s: requests.Session, url: str, target: Path, chunk: int = 1 << 20) -> int:
    """流式下载，支持用 Range 从 .part 续传。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}

    with s.get(url, headers=headers, stream=True, timeout=(30, 300)) as r:
        if offset and r.status_code == 200:  # 服务端不认 Range，从头来
            offset = 0
            part.unlink(missing_ok=True)
        elif r.status_code not in (200, 206):
            raise DownloadError(f"HTTP {r.status_code} 下载 {url}")
        with part.open("ab" if offset else "wb") as fh:
            for block in r.iter_content(chunk_size=chunk):
                if block:
                    fh.write(block)

    part.replace(target)
    return target.stat().st_size


# ---------------------------------------------------------------- 工具


_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _name_budget(dest_dir: Path, floor: int = 24) -> int:
    """Windows 默认 MAX_PATH 是 260，文件名要按剩余预算裁。

    还要给 api 模式的 '.part' 后缀留位置。非 Windows 上给个宽松上限即可。
    """
    if os.name != "nt":
        return 180
    remaining = 260 - len(str(dest_dir)) - len("\\") - len(".part") - 1
    return max(floor, min(180, remaining))


def safe_filename(name: str, max_len: int = 180) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", (name or "file").strip()) or "file"
    name = name.rstrip(". ") or "file"  # Windows 不允许结尾是点或空格
    stem, dot, ext = name.rpartition(".")
    if (stem or name).upper() in _WIN_RESERVED:
        name = "_" + name
    if len(name) <= max_len:
        return name
    stem, dot, ext = name.rpartition(".")
    return (stem[: max(1, max_len - len(ext) - 1)] + dot + ext) if dot else name[:max_len]
