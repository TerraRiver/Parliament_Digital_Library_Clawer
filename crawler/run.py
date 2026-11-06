"""爬取主流程：翻页 -> 逐条打开详情页（View）-> 抓元数据 -> 下载文件。

任何时候 Ctrl+C 或进程被杀，state.json 都保存着已完成的进度，
重跑同一条命令即可从断点继续。
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from . import pages, state as st
from .browser import make_driver
from .download import download_item_files
from .stealth import human_wait

DEFAULT_SEARCH_URL = (
    "https://elibrary.sansad.in/search"
    "?query=china&scope=ca1f325a-61ab-4d3c-857d-f48a9b78b1a2"
    "&spc.sf=keyword_count&spc.sd=DESC"
)
DEFAULT_API_BASE = "https://elibrary.sansad.in/server/api"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(dotenv_path: Path | None = None, *, override: bool = False) -> None:
    """加载项目根目录或当前工作目录下的 .env 文件到 os.environ。"""
    candidates = [dotenv_path] if dotenv_path else [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            if len(val) >= 2 and val[0] in ("'", '"'):
                q = val[0]
                end_idx = val.find(q, 1)
                if end_idx != -1 and (not val[end_idx + 1 :].strip() or val[end_idx + 1 :].strip().startswith("#")):
                    val = val[1:end_idx]
            else:
                # 去掉未加引号值后面的行内注释
                val = val.split(" #", 1)[0].split("\t#", 1)[0].strip()
            if not val:
                continue
            if override or key not in os.environ:
                os.environ[key] = val


class Interrupted(Exception):
    pass


class Crawler:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.data_dir = Path(args.data_dir).expanduser().resolve()
        if getattr(args, "output_dir", None):
            self.files_dir = Path(args.output_dir).expanduser().resolve()
            self.staging_dir = self.files_dir / ".staging"
        else:
            self.files_dir = self.data_dir / "files"
            self.staging_dir = self.data_dir / ".staging"
        self.state = st.State(self.data_dir / "state.json", self.data_dir / "metadata.jsonl")
        self._warn_if_changed("search_url", args.search_url)
        # 换了每页条数会让已记录的页号对不上，续跑时必须保持一致
        self._warn_if_changed("page_size", args.page_size)
        self.state.data["search_url"] = args.search_url
        self.state.data["page_size"] = args.page_size
        self.driver = None
        self.items_since_restart = 0
        self.processed = 0
        self._reached_eof = False
        self._stop = False

    def _warn_if_changed(self, key: str, value) -> None:
        old = self.state.data.get(key)
        if old is not None and old != value and self.state.data["pages"]:
            print(f"[警告] {key} 与已有进度不同（{old!r} -> {value!r}），页号可能对不上，建议换一个 --data-dir")

    # ---------------------------------------------------------- 驱动生命周期

    def _ensure_driver(self):
        if self.driver is None:
            print("[driver] 启动 Chrome …")
            self.driver = make_driver(
                self.staging_dir,
                headless=not self.args.headed,
                driver_path=self.args.driver_path,
                browser_binary=self.args.chrome_binary,
            )
            self.items_since_restart = 0
        return self.driver

    def _restart_driver(self):
        self._quit_driver()
        self._ensure_driver()

    def _quit_driver(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    # ---------------------------------------------------------- 主循环

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        started = time.time()
        exit_code = 0
        try:
            self._crawl()
        except Interrupted:
            print("\n[中断] 已收到停止信号，正在保存进度 …")
            exit_code = 130
        except Exception as exc:
            print(f"\n[错误] 爬取因异常中止：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            exit_code = 1
        finally:
            self.state.save(force=True)
            self._quit_driver()
            self._report(time.time() - started)
        return exit_code

    def _on_signal(self, *_):
        if self._stop:  # 连按两次直接退出
            sys.exit(130)
        self._stop = True
        print("\n[中断] 收到信号，处理完当前条目后停止（再按一次强制退出）")

    def _check_stop(self):
        if self._stop:
            raise Interrupted

    def _crawl(self):
        page = self.args.start_page
        pages_done = 0
        while True:
            self._check_stop()
            if self.args.max_pages and pages_done >= self.args.max_pages:
                print(f"[翻页] 已达 --max-pages {self.args.max_pages}，停止")
                return
            if self.args.max_items and self.processed >= self.args.max_items:
                print(f"[条目] 已达 --max-items {self.args.max_items}，停止")
                return

            rows = self._page_rows(page)
            if rows is None:  # 翻到底了
                self._reached_eof = True
                print(f"[翻页] 第 {page} 页没有结果，全部页面已处理完")
                return

            todo = [
                r for r in rows
                if self.state.needs_work(
                    r["uuid"],
                    self.args.retry_failed,
                    self.args.max_retries,
                    self.args.recheck_no_file,
                    no_download=self.args.no_download,
                )
            ]
            print(f"[翻页] 第 {page} 页：{len(rows)} 条结果，{len(todo)} 条待处理")

            for row in todo:
                self._check_stop()
                if self.args.max_items and self.processed >= self.args.max_items:
                    break
                self._process_item(row)

            self.state.save(force=True)
            pages_done += 1
            page += 1

    def _page_rows(self, page: int) -> list[dict[str, Any]] | None:
        """拿这一页的条目列表。已抓过且非空的页直接走 state，不再开浏览器。"""
        if self.state.page_done(page) and not self.args.refresh_pages:
            cached = [r for r in self.state.data["items"].values() if r.get("list_page") == page]
            if cached:
                return cached

        self._check_stop()
        rows, total = self._with_retries(
            f"列表页 p{page}",
            lambda: pages.scrape_list_page(
                self._ensure_driver(), self.args.search_url, page, self.args.page_size
            ),
        )
        if not rows:
            return None
        if isinstance(total, int) and total > 0:
            self.state.data["total_expected"] = total

        new = sum(
            self.state.add_discovered(
                r["uuid"], r["title"], r["url"], page,
                extra={"year": r["year"], "item_type": r["item_type"], "keyword_count": r["keyword_count"]},
            )
            for r in rows
        )
        self.state.mark_page(page, "done", len(rows))
        self.state.save(force=True)
        if new != len(rows):
            print(f"[翻页] 第 {page} 页有 {len(rows) - new} 条此前已见过")
        return [self.state.data["items"][r["uuid"]] for r in rows]

    # ---------------------------------------------------------- 单条目

    def _process_item(self, row: dict[str, Any]):
        uuid, title = row["uuid"], row.get("title") or ""
        rec = self.state.get(uuid) or row
        label = f"{title[:60]}" if title else uuid
        already_has_meta = (
            rec.get("status") == st.METADATA
            and bool(rec.get("files"))
            and not self.args.no_download
        )

        if self.items_since_restart >= self.args.restart_every:
            print(f"[driver] 已处理 {self.items_since_restart} 条，重启浏览器释放内存")
            self._restart_driver()

        try:
            if already_has_meta and self.args.download_mode == "api":
                # api 模式下若此前 --no-download 已抓过元数据与文件清单，直接复用，免去重开详情页
                files_meta = rec["files"]
            else:
                data = self._with_retries(
                    f"详情页 {uuid}",
                    lambda: pages.scrape_item_page(self._ensure_driver(), row["url"]),
                )
                files_meta = data["files"]
                self.state.update(
                    uuid,
                    status=st.METADATA,
                    title=data["fields"].get("Title", [title])[0] if data["fields"].get("Title") else title,
                    field_count=len(data["fields"]),
                    files=files_meta,
                    error=None,
                )
                if not already_has_meta:
                    self.state.append_metadata(
                        {
                            "uuid": uuid,
                            "url": row["url"],
                            "list_page": row.get("list_page"),
                            "year": row.get("year"),
                            "item_type": row.get("item_type"),
                            "keyword_count": row.get("keyword_count"),
                            "fields": data["fields"],
                            "files": files_meta,
                            "scraped_at": st.now(),
                        }
                    )

            if not files_meta:
                self.state.update(uuid, status=st.NO_FILE, files=[], attempts=0, error=None)
                print(f"  [{self.processed + 1}] {label} — 无附件")
            elif self.args.no_download:
                self.state.update(uuid, status=st.METADATA, files=files_meta, attempts=0, error=None)
                print(f"  [{self.processed + 1}] {label} — 元数据已存（{len(files_meta)} 个文件，未下载）")
            else:
                files = download_item_files(
                    self._ensure_driver(),
                    item=row,
                    files=files_meta,
                    dest_dir=self.files_dir / uuid,
                    staging_dir=self.staging_dir,
                    mode=self.args.download_mode,
                    min_speed=self.args.min_speed,
                    min_timeout=self.args.min_timeout,
                    stall_timeout=self.args.stall_timeout,
                    api_base=self.args.api_base,
                    concurrency=self.args.concurrency,
                )
                self.state.update(uuid, status=st.DONE, files=files, attempts=0, error=None)
                total = sum(f["bytes"] for f in files)
                skipped = sum(1 for f in files if f["skipped"])
                note = f"（{skipped} 个已存在，跳过）" if skipped else ""
                print(f"  [{self.processed + 1}] {label} — {len(files)} 个文件 {_mb(total)}{note}")

        except Interrupted:
            raise
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            cur_rec = self.state.get(uuid) or rec
            attempts = cur_rec.get("attempts", 0) + 1
            failed = attempts >= self.args.max_retries
            fallback_status = st.METADATA if cur_rec.get("status") == st.METADATA else st.PENDING
            self.state.update(
                uuid,
                attempts=attempts,
                status=st.FAILED if failed else fallback_status,
                error=msg,
            )
            print(f"  [!] {label} 第 {attempts} 次失败{'（放弃）' if failed else ''}：{msg}")
            if isinstance(exc, PlaywrightError) or isinstance(exc.__cause__, PlaywrightError):
                self._quit_driver()
        finally:
            self.processed += 1
            self.items_since_restart += 1
            self.state.save(every=self.args.save_every)
            self._sleep()

    # ---------------------------------------------------------- 辅助

    def _with_retries(self, what: str, fn):
        last = None
        for attempt in range(1, self.args.page_retries + 1):
            self._check_stop()
            try:
                return fn()
            except Interrupted:
                raise
            except Exception as exc:
                last = exc
                wait = min(2**attempt, 30) + random.uniform(0, 1)
                print(f"  [retry] {what} 第 {attempt} 次失败（{type(exc).__name__}），{wait:.1f}s 后重试")
                if isinstance(exc, PlaywrightError) or isinstance(exc.__cause__, PlaywrightError):
                    self._quit_driver()
                if attempt < self.args.page_retries:
                    time.sleep(wait)
        raise last

    def _sleep(self):
        if self.args.delay > 0:
            min_ms = int(self.args.delay * 1000)
            max_ms = int(self.args.delay * 1300)
            if self.driver is not None:
                try:
                    human_wait(self.driver, min_ms, max_ms)
                    return
                except Exception:
                    pass
            time.sleep(self.args.delay + random.uniform(0, self.args.delay * 0.3))

    def _report(self, elapsed: float):
        s = self.state.summary()
        total_items = s.get("total_items", 0)
        total_expected = s.get("total_expected")
        expected_note = f"（站点显示总数 {total_expected}）" if isinstance(total_expected, int) else ""
        print("\n" + "=" * 60)
        print(f"本次运行 {elapsed / 60:.1f} 分钟，处理 {self.processed} 条")
        print(f"累计：{total_items} 条目{expected_note} / {s.get('pages_done', 0)} 页")
        if self._reached_eof and isinstance(total_expected, int) and total_items < total_expected:
            diff = total_expected - total_items
            print(
                f"  [对账] 已发现条目数比站点总数少 {diff} 条（可能因同分排序跨页漂移），"
                f"可加 --refresh-pages 重扫列表页补齐"
            )
        for k in (st.DONE, st.METADATA, st.NO_FILE, st.PENDING, st.FAILED):
            if s.get(k):
                print(f"  {k:<9} {s[k]}")
        print(f"已下载 {_mb(s.get('downloaded_bytes', 0))}")
        print(f"状态文件 {self.state.path}")
        print(f"元数据   {self.state.metadata_path}")
        print(f"文件目录 {self.files_dir}")
        print("=" * 60)


def _mb(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    default_data_dir = os.environ.get("PDL_DATA_DIR") or os.environ.get("DATA_DIR") or "data"
    default_output_dir = (
        os.environ.get("PDL_OUTPUT_DIR")
        or os.environ.get("OUTPUT_DIR")
        or os.environ.get("PDL_FILES_DIR")
        or None
    )

    p = argparse.ArgumentParser(
        prog="parliament-crawler",
        description="爬取 Parliament Digital Library 搜索结果的元数据与附件（Playwright，支持断点续跑）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="搜索结果页 URL")
    p.add_argument("--data-dir", default=default_data_dir,
                   help="状态、元数据与默认附件的存放目录（也可在 .env 中设 PDL_DATA_DIR）")
    p.add_argument("--output-dir", default=default_output_dir,
                   help="下载的 PDF 附件输出目录，不指定则存于 <data-dir>/files（也可在 .env 中设 PDL_OUTPUT_DIR）")
    p.add_argument("--page-size", type=int, default=100, help="每页条目数（spc.rpp）")
    p.add_argument("--start-page", type=int, default=1, help="从第几页开始")
    p.add_argument("--max-pages", type=int, default=0, help="本次最多翻几页，0 为不限")
    p.add_argument("--max-items", type=int, default=0, help="本次最多处理几条，0 为不限")
    p.add_argument("--refresh-pages", action="store_true", help="即使某页标记为已完成也重新抓一遍列表")

    p.add_argument("--download-mode", choices=("click", "api"), default="click",
                   help="click 点页面按钮下载；api 走 REST 直链（更快、可续传）")
    p.add_argument("--no-download", action="store_true", help="只抓元数据，不下载附件")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help="DSpace REST 根地址（api 模式用）")
    p.add_argument("--concurrency", type=int, default=3,
                   help="api 模式下，同一条目内并发下载的文件数（click 模式恒为顺序下载）")

    p.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试用）")
    p.add_argument("--driver-path", default=None,
                   help="已废弃（Playwright 无需 chromedriver，保留仅为兼容旧脚本）")
    p.add_argument("--chrome-binary", default=None,
                   help="Chrome/Chromium 可执行文件路径（装在非默认位置时用）；也可用环境变量 PDL_CHROME_BINARY")
    p.add_argument("--delay", type=float, default=1.0, help="每条之间的基础间隔秒数")
    p.add_argument("--max-retries", type=int, default=3, help="单条目累计失败多少次后标记为 failed")
    p.add_argument("--page-retries", type=int, default=3, help="单次页面加载的重试次数")
    p.add_argument("--retry-failed", action="store_true", help="重跑此前标记为 failed 的条目")
    p.add_argument("--recheck-no-file", action="store_true",
                   help="重新核实此前标记为 no_file 的条目（用于修复过去误判"
                        "\"无附件\"的老数据、回填漏抓的文件）")
    p.add_argument("--restart-every", type=int, default=200, help="每处理多少条重启一次浏览器")
    p.add_argument("--save-every", type=int, default=5, help="每处理多少条写一次 state.json")

    p.add_argument("--min-speed", type=float, default=80_000, help="下载超时按此速度（字节/秒）估算")
    p.add_argument("--min-timeout", type=float, default=180, help="单文件下载最短超时秒数")
    p.add_argument("--stall-timeout", type=float, default=120, help="多少秒无进度判为卡死")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return Crawler(args).run()
