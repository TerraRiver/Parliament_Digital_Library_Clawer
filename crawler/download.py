"""文件下载。

两种模式：
  click —— 真的去点页面上的 Download 按钮，由浏览器完成下载（默认，行为与人工一致）
  api   —— 从 DSpace REST 接口取 bitstream 直链，用 requests 流式下载
            （支持断点续传的 Range 请求，大文件更稳、更快）
"""

from __future__ import annotations

import asyncio
import os
import queue
import random
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from tqdm import tqdm

from .browser import USER_AGENT
from .pages import find_download_buttons
from .stealth import human_move_and_click, human_reset_gaze, human_scroll_to_element


class DownloadError(RuntimeError):
    pass


def _make_bar(label: str, total: int | None, position: int, initial: int = 0) -> tqdm:
    """并发下载时每个文件占一个固定 position（行号），避免多个进度条互相踩行。"""
    return tqdm(
        total=total,
        initial=initial,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=label[:40],
        position=position,
        leave=False,
        dynamic_ncols=True,
        mininterval=0.3,
    )


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
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """下载一个条目下的全部文件，返回带落盘信息的文件记录。

    api 模式下 concurrency > 1 时，同一条目内的多个文件并发下载
    （click 模式受限于"点哪个按钮对应哪个下载"的匹配问题，始终顺序下载）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any] | None] = [None] * len(files)
    pending: list[tuple[int, dict[str, Any], Path, int | None]] = []
    used_names: set[str] = set()
    budget = _name_budget(dest_dir)

    for index, meta in enumerate(files):
        raw_safe = safe_filename(meta["name"], max_len=budget)
        name = _deduplicate_filename(raw_safe, used_names, max_len=budget)
        target = dest_dir / name
        expected = meta.get("size_bytes_expected")
        if _already_ok(target, expected):
            results[index] = {**meta, "path": str(target), "bytes": target.stat().st_size, "skipped": True}
        else:
            pending.append((index, meta, target, expected))

    if pending:
        if mode == "api":
            _download_many_via_api(driver, item["uuid"], pending, results, api_base, concurrency)
        else:
            for index, meta, target, expected in pending:
                got = _download_via_click(
                    driver,
                    index=index,
                    total_files=len(files),
                    target=target,
                    staging_dir=staging_dir,
                    expected=expected,
                    min_speed=min_speed,
                    min_timeout=min_timeout,
                    stall_timeout=stall_timeout,
                    label=meta["name"],
                )
                results[index] = {**meta, "path": str(target), "bytes": got, "skipped": False}

    return results


def _already_ok(target: Path, expected: int | None) -> bool:
    if not _validate_downloaded_file(target):
        return False
    if expected is None:
        return True
    # 页面上的体积是四舍五入到两位小数的，留 2% 容差
    return abs(target.stat().st_size - expected) <= max(expected * 0.02, 4096)


def _validate_downloaded_file(path: Path, suffix_hint: str | None = None) -> bool:
    """校验文件非空，且非伪装成附件的 HTML 错误页；若是 PDF 则校验 %PDF- 文件头。"""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False

    ext = (suffix_hint if suffix_hint is not None else path.suffix).lower()
    if ext == ".pdf":
        return b"%PDF-" in head
    if ext not in (".html", ".htm"):
        stripped = head.lstrip()[:32].lower()
        if stripped.startswith((b"<!doctype html", b"<html")):
            return False
    return True


# ---------------------------------------------------------------- click 模式


def _download_via_click(
    driver,
    *,
    index: int,
    total_files: int | None = None,
    target: Path,
    staging_dir: Path,
    expected: int | None,
    min_speed: float,
    min_timeout: float,
    stall_timeout: float,
    label: str | None = None,
) -> int:
    staging_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir(staging_dir)

    buttons = find_download_buttons(driver)
    count = buttons.count()
    if total_files is not None and count != total_files:
        raise DownloadError(
            f"页面下载按钮数量（{count}）与解析出的文件数（{total_files}）不一致，拒绝按序号盲点"
        )
    if index >= count:
        raise DownloadError(f"页面上只有 {count} 个下载按钮，取不到第 {index + 1} 个")
    btn = buttons.nth(index)
    human_scroll_to_element(driver, btn)

    timeout = max(min_timeout, (expected or 0) / max(min_speed, 1))
    try:
        with driver.expect_download(timeout=min(timeout, 60.0) * 1000) as dl_info:
            try:
                human_move_and_click(driver, btn, timeout=5_000, scroll_if_needed=False)
            except Exception:
                btn.evaluate("el => el.click()", timeout=5_000)
    except PlaywrightTimeoutError as exc:
        raise DownloadError(f"点击下载按钮后未触发下载响应：{exc}") from exc

    download = dl_info.value
    _save_download_with_progress(
        download,
        driver=driver,
        target=target,
        staging_dir=staging_dir,
        timeout=timeout,
        stall_timeout=stall_timeout,
        expected=expected,
        label=label,
    )
    human_reset_gaze(driver)
    return target.stat().st_size


def _save_download_with_progress(
    download,
    *,
    driver=None,
    target: Path,
    staging_dir: Path,
    timeout: float,
    stall_timeout: float,
    expected: int | None = None,
    label: str | None = None,
) -> None:
    """等待 Playwright 在 staging_dir 内下载完成并移入 target，同时后台监控刷新进度条与检测卡死。"""
    bar = _make_bar(label, expected, position=0) if label else None
    stop_event = threading.Event()
    abort_error: list[DownloadError] = []

    def _watch() -> None:
        deadline = time.monotonic() + timeout
        last_size, last_progress = -1, time.monotonic()
        while not stop_event.wait(0.5):
            try:
                files = [p for p in staging_dir.iterdir() if p.is_file()]
                size = sum(_safe_file_size(p) for p in files)
            except OSError:
                size = last_size if last_size >= 0 else 0

            if size != last_size:
                last_size, last_progress = size, time.monotonic()
            elif time.monotonic() - last_progress > stall_timeout:
                abort_error.append(
                    DownloadError(f"下载卡住：{stall_timeout:.0f}s 内体积没有增长（已收 {size} 字节）")
                )
                _cancel_download_threadsafe(download, driver, stop_event)
                return

            if time.monotonic() >= deadline:
                abort_error.append(DownloadError(f"下载超时（{timeout:.0f}s）"))
                _cancel_download_threadsafe(download, driver, stop_event)
                return

            if bar and size > bar.n:
                bar.update(size - bar.n)

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        finished_path = download.path()
        if abort_error:
            raise abort_error[0]
        err = download.failure()
        if err:
            raise DownloadError(f"浏览器下载失败：{err}")
        if not finished_path or not _validate_downloaded_file(finished_path, suffix_hint=target.suffix):
            raise DownloadError(f"下载完成但文件校验失败（为空或非有效 {target.suffix or '文件'}）")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(finished_path), str(target))
        if bar:
            final_size = target.stat().st_size
            if final_size > bar.n:
                bar.update(final_size - bar.n)
    except PlaywrightError as exc:
        if abort_error:
            raise abort_error[0] from exc
        raise DownloadError(f"下载异常：{exc}") from exc
    finally:
        stop_event.set()
        watcher.join(timeout=2.0)
        if bar:
            bar.close()
        try:
            download.delete()
        except Exception:
            pass


def _cancel_download_threadsafe(download, driver=None, stop_event: threading.Event | None = None) -> None:
    """跨线程取消下载；若 3 秒内主线程仍未解除阻塞，则强制关闭浏览器会话防止无限挂起。"""
    try:
        impl = getattr(download, "_impl_obj", None)
        loop = getattr(impl, "_loop", None) if impl is not None else None
        if impl is not None and loop is not None:
            asyncio.run_coroutine_threadsafe(impl.cancel(), loop)
    except Exception:
        pass

    if stop_event is not None and not stop_event.wait(3.0) and driver is not None:
        try:
            browser_impl = getattr(getattr(driver, "browser", None), "_impl_obj", None)
            loop = getattr(browser_impl, "_loop", None) if browser_impl is not None else None
            if browser_impl is not None and loop is not None:
                asyncio.run_coroutine_threadsafe(browser_impl.close(), loop)
        except Exception:
            pass


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _clear_dir(path: Path) -> None:
    """清掉上一条目留下的残留文件。浏览器/杀软有时还短暂占着文件句柄
    （WinError 32），重试一次，还不行就跳过，不因为清残留失败搞垮这一条目。
    """
    for p in path.iterdir():
        for attempt in (1, 2):
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
                else:
                    shutil.rmtree(p, ignore_errors=True)
                break
            except PermissionError:
                if attempt == 2:
                    print(f"  [警告] 清理残留文件失败（被占用），跳过：{p}")
                    break
                time.sleep(1)


# ---------------------------------------------------------------- api 模式


def _session_from_driver(driver, api_base: str) -> requests.Session:
    """复用同一个 driver 上的 session，保持连接池和 cookie。"""
    s = getattr(driver, "_pdl_session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        s.headers["Referer"] = api_base.rsplit("/server", 1)[0] + "/"
        # 禁用透明 gzip 压缩，确保 Content-Length 与实际落盘字节数、Range 偏移量严格一致
        s.headers["Accept-Encoding"] = "identity"
        driver._pdl_session = s
    for c in driver.context.cookies():
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def _with_http_retries(fn, attempts: int = 3, what: str = "请求"):
    """网络抖动（SSL EOF、连接重置）或 429 限流时就地重试，不浪费条目的重试额度。"""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (requests.RequestException, OSError) as exc:
            last = exc
            if i < attempts:
                if (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code == 429
                ):
                    wait = 30.0 + random.uniform(0, 5)
                    print(f"  [限流] {what}触发 HTTP 429，冷却 {wait:.1f}s 后重试 …")
                    time.sleep(wait)
                else:
                    time.sleep(min(2**i, 15))
    raise DownloadError(f"{what}重试 {attempts} 次仍失败：{type(last).__name__}: {last}") from last


def _download_many_via_api(
    driver,
    uuid: str,
    pending: list[tuple[int, dict[str, Any], Path, int | None]],
    results: list[dict[str, Any] | None],
    api_base: str,
    concurrency: int,
) -> None:
    """并发下载同一条目下的多个文件。

    bundles 接口按条目返回全部 bitstream，之前每个文件各请求一次纯属浪费，
    这里改成每条目只请求一次，并发的多个文件线程共用同一份候选列表。
    """
    s = _session_from_driver(driver, api_base)
    candidates = _with_http_retries(
        lambda: _resolve_bundle_files(s, api_base, uuid), what="解析文件列表"
    )

    workers = max(1, min(concurrency, len(pending)))
    # 每个并发槽位固定占一行进度条，用完归还，避免多个文件的进度条抢同一行
    positions: queue.Queue[int] = queue.Queue()
    for i in range(workers):
        positions.put(i)

    def work(index: int, meta: dict[str, Any], target: Path, expected: int | None):
        pos = positions.get()
        try:
            url = _match_bitstream(candidates, meta["name"])
            got = _with_http_retries(
                lambda: _stream_download(s, url, target, progress_label=meta["name"], position=pos),
                what=f"下载 {meta['name']}",
            )
        finally:
            positions.put(pos)
        return index, {**meta, "path": str(target), "bytes": got, "skipped": False}

    if workers == 1:
        for args in pending:
            index, rec = work(*args)
            results[index] = rec
        return

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, *args) for args in pending]
        for fut in as_completed(futures):
            index, rec = fut.result()
            results[index] = rec


def _resolve_bundle_files(s: requests.Session, api_base: str, uuid: str) -> list[tuple[str, str]]:
    r = s.get(f"{api_base}/core/items/{uuid}/bundles", params={"embed": "bitstreams"}, timeout=60)
    r.raise_for_status()
    bundles = r.json().get("_embedded", {}).get("bundles", [])
    candidates: list[tuple[str, str]] = []
    for b in bundles:
        if b.get("name") != "ORIGINAL":
            continue
        for bs in b.get("_embedded", {}).get("bitstreams", {}).get("_embedded", {}).get("bitstreams", []):
            candidates.append((bs.get("name", ""), bs["_links"]["content"]["href"]))
    return candidates


def _match_bitstream(candidates: list[tuple[str, str]], filename: str) -> str:
    """将页面上的文件名匹配到 REST bundles 返回的 bitstream 直链。

    多文件条目下必须精确或规范化匹配成功；仅在条目下总共只有 1 个候选文件时才允许兜底。
    """
    for name, href in candidates:
        if name == filename:
            return href
    norm_target = filename.strip().lower()
    safe_target = safe_filename(filename).lower()
    for name, href in candidates:
        if name.strip().lower() == norm_target or safe_filename(name).lower() == safe_target:
            return href
    if len(candidates) == 1:
        return candidates[0][1]
    raise DownloadError(
        f"REST 接口里找不到与 {filename!r} 匹配的 bitstream（候选：{[c[0] for c in candidates]}）"
    )


def _stream_download(
    s: requests.Session,
    url: str,
    target: Path,
    chunk: int = 1 << 20,
    progress_label: str | None = None,
    position: int = 0,
) -> int:
    """流式下载，支持用 Range 从 .part 续传。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}

    with s.get(url, headers=headers, stream=True, timeout=(30, 300)) as r:
        if offset and r.status_code == 416:
            remote_total = _parse_content_range_total(r.headers.get("Content-Range"))
            if (remote_total is None or offset == remote_total) and _validate_downloaded_file(
                part, suffix_hint=target.suffix
            ):
                part.replace(target)
                return target.stat().st_size
            part.unlink(missing_ok=True)
            raise requests.RequestException(
                f"HTTP 416 Range 冲突（本地 .part={offset} 字节，Content-Range={r.headers.get('Content-Range')}），已清理并重下",
                response=r,
            )

        if offset and r.status_code == 200:  # 服务端不认 Range，从头来
            offset = 0
            part.unlink(missing_ok=True)
        elif r.status_code not in (200, 206):
            r.raise_for_status()
            raise requests.RequestException(f"HTTP {r.status_code} 下载 {url}", response=r)

        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/html") and target.suffix.lower() not in (".html", ".htm"):
            raise requests.RequestException(
                f"服务端返回了 HTML 页面而非附件 (Content-Type: {r.headers.get('Content-Type')})",
                response=r,
            )

        total = _response_total_bytes(r, offset)
        bar = _make_bar(progress_label, total, position, initial=offset) if progress_label else None
        try:
            with part.open("ab" if offset else "wb") as fh:
                for block in r.iter_content(chunk_size=chunk):
                    if not block:
                        continue
                    fh.write(block)
                    if bar:
                        bar.update(len(block))
        finally:
            if bar:
                bar.close()

    actual_size = part.stat().st_size if part.exists() else 0
    if total is not None and actual_size != total:
        raise requests.RequestException(
            f"下载字节数不完整：已收 {actual_size} / 预期 {total} 字节"
        )
    if not _validate_downloaded_file(part, suffix_hint=target.suffix):
        part.unlink(missing_ok=True)
        raise DownloadError(f"下载的文件内容校验失败（非有效 {target.suffix or '文件'} 或疑似错误页）：{target.name}")

    part.replace(target)
    return target.stat().st_size


def _parse_content_range_total(content_range: str | None) -> int | None:
    """解析 'bytes */12345' 或 'bytes 0-99/12345' 中的总长度 12345。"""
    if not content_range or "/" not in content_range:
        return None
    total_str = content_range.rsplit("/", 1)[-1].strip()
    if total_str == "*" or not total_str.isdigit():
        return None
    return int(total_str)


def _response_total_bytes(r: requests.Response, offset: int) -> int | None:
    """206 响应的 Content-Length 只是剩余字节数，要加回已有的 offset 才是整个文件大小。"""
    cl = r.headers.get("Content-Length")
    if cl is None:
        return None
    try:
        return offset + int(cl)
    except ValueError:
        return None


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


def _deduplicate_filename(name: str, used_lower: set[str], max_len: int = 180) -> str:
    """同一条目内若出现重名（或大小写不敏感冲突），自动追加 _2, _3 后缀避免覆盖或并发写冲突。"""
    if name.lower() not in used_lower:
        used_lower.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, dot, ext = name, "", ""
    idx = 2
    while True:
        tag = f"_{idx}"
        budget = max(1, max_len - len(tag) - len(dot) - len(ext))
        candidate = f"{stem[:budget]}{tag}{dot}{ext}"
        if candidate.lower() not in used_lower:
            used_lower.add(candidate.lower())
            return candidate
        idx += 1
