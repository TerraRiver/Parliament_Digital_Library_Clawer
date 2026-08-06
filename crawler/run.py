"""爬取主流程：翻页 -> 逐条打开详情页（View）-> 抓元数据 -> 下载文件。

任何时候 Ctrl+C 或进程被杀，state.json 都保存着已完成的进度，
重跑同一条命令即可从断点继续。
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any

from selenium.common.exceptions import WebDriverException

from . import pages, state as st
from .browser import make_driver
from .download import download_item_files

DEFAULT_SEARCH_URL = (
    "https://elibrary.sansad.in/search"
    "?query=china&scope=ca1f325a-61ab-4d3c-857d-f48a9b78b1a2"
    "&spc.sf=keyword_count&spc.sd=DESC"
)
DEFAULT_API_BASE = "https://elibrary.sansad.in/server/api"


class Interrupted(Exception):
    pass


class Crawler:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.data_dir = Path(args.data_dir).expanduser().resolve()
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
        try:
            self._crawl()
        except Interrupted:
            print("\n[中断] 已收到停止信号，正在保存进度 …")
        finally:
            self.state.save(force=True)
            self._quit_driver()
        self._report(time.time() - started)
        return 0

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
                print(f"[翻页] 第 {page} 页没有结果，全部页面已处理完")
                return

            todo = [
                r for r in rows
                if self.state.needs_work(r["uuid"], self.args.retry_failed, self.args.max_retries)
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
        """拿这一页的条目列表。已抓过的页直接走 state，不再开浏览器。"""
        if self.state.page_done(page) and not self.args.refresh_pages:
            cached = [r for r in self.state.data["items"].values() if r.get("list_page") == page]
            if cached:
                return cached
            if self.state.data["pages"][str(page)].get("count") == 0:
                return None

        self._check_stop()
        driver = self._ensure_driver()
        rows = self._with_retries(
            f"列表页 p{page}",
            lambda: pages.scrape_list_page(driver, self.args.search_url, page, self.args.page_size),
        )
        if not rows:
            self.state.mark_page(page, "done", 0)
            self.state.save(force=True)
            return None

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
        self.state.update(uuid, attempts=rec.get("attempts", 0) + 1)
        attempts = self.state.get(uuid)["attempts"]
        label = f"{title[:60]}" if title else uuid

        if self.items_since_restart >= self.args.restart_every:
            print(f"[driver] 已处理 {self.items_since_restart} 条，重启浏览器释放内存")
            self._restart_driver()

        try:
            driver = self._ensure_driver()
            data = self._with_retries(
                f"详情页 {uuid}",
                lambda: pages.scrape_item_page(driver, row["url"]),
            )
            self.state.update(
                uuid,
                status=st.METADATA,
                title=data["fields"].get("Title", [title])[0] if data["fields"].get("Title") else title,
                field_count=len(data["fields"]),
                error=None,
            )
            self.state.append_metadata(
                {
                    "uuid": uuid,
                    "url": row["url"],
                    "list_page": row.get("list_page"),
                    "year": row.get("year"),
                    "item_type": row.get("item_type"),
                    "keyword_count": row.get("keyword_count"),
                    "fields": data["fields"],
                    "files": data["files"],
                    "scraped_at": st.now(),
                }
            )

            if not data["files"]:
                self.state.update(uuid, status=st.NO_FILE, files=[])
                print(f"  [{self.processed + 1}] {label} — 无附件")
            elif self.args.no_download:
                print(f"  [{self.processed + 1}] {label} — 元数据已存（{len(data['files'])} 个文件，未下载）")
            else:
                files = download_item_files(
                    driver,
                    item=row,
                    files=data["files"],
                    dest_dir=self.files_dir / uuid,
                    staging_dir=self.staging_dir,
                    mode=self.args.download_mode,
                    min_speed=self.args.min_speed,
                    min_timeout=self.args.min_timeout,
                    stall_timeout=self.args.stall_timeout,
                    api_base=self.args.api_base,
                )
                self.state.update(uuid, status=st.DONE, files=files)
                total = sum(f["bytes"] for f in files)
                skipped = sum(1 for f in files if f["skipped"])
                note = f"（{skipped} 个已存在，跳过）" if skipped else ""
                print(f"  [{self.processed + 1}] {label} — {len(files)} 个文件 {_mb(total)}{note}")

        except Interrupted:
            raise
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            failed = attempts >= self.args.max_retries
            self.state.update(uuid, status=st.FAILED if failed else st.PENDING, error=msg)
            print(f"  [!] {label} 第 {attempts} 次失败{'（放弃）' if failed else ''}：{msg}")
            if isinstance(exc, WebDriverException):
                self._restart_driver()
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
            except Exception as exc:
                last = exc
                wait = min(2**attempt, 30) + random.uniform(0, 1)
                print(f"  [retry] {what} 第 {attempt} 次失败（{type(exc).__name__}），{wait:.1f}s 后重试")
                if isinstance(exc, WebDriverException):
                    self._restart_driver()
                time.sleep(wait)
        raise last

    def _sleep(self):
        if self.args.delay > 0:
            time.sleep(self.args.delay + random.uniform(0, self.args.delay * 0.3))

    def _report(self, elapsed: float):
        s = self.state.summary()
        print("\n" + "=" * 60)
        print(f"本次运行 {elapsed / 60:.1f} 分钟，处理 {self.processed} 条")
        print(f"累计：{s.get('total_items', 0)} 条目 / {s.get('pages_done', 0)} 页")
        for k in (st.DONE, st.METADATA, st.NO_FILE, st.PENDING, st.FAILED):
            if s.get(k):
                print(f"  {k:<9} {s[k]}")
        print(f"已下载 {_mb(s.get('downloaded_bytes', 0))}")
        print(f"状态文件 {self.state.path}")
        print(f"元数据   {self.state.metadata_path}")
        print("=" * 60)


def _mb(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parliament-crawler",
        description="爬取 Parliament Digital Library 搜索结果的元数据与附件（Selenium，支持断点续跑）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--search-url", default=DEFAULT_SEARCH_URL, help="搜索结果页 URL")
    p.add_argument("--data-dir", default="data", help="状态、元数据与附件的存放目录")
    p.add_argument("--page-size", type=int, default=100, help="每页条目数（spc.rpp）")
    p.add_argument("--start-page", type=int, default=1, help="从第几页开始")
    p.add_argument("--max-pages", type=int, default=0, help="本次最多翻几页，0 为不限")
    p.add_argument("--max-items", type=int, default=0, help="本次最多处理几条，0 为不限")
    p.add_argument("--refresh-pages", action="store_true", help="即使某页标记为已完成也重新抓一遍列表")

    p.add_argument("--download-mode", choices=("click", "api"), default="click",
                   help="click 点页面按钮下载；api 走 REST 直链（更快、可续传）")
    p.add_argument("--no-download", action="store_true", help="只抓元数据，不下载附件")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help="DSpace REST 根地址（api 模式用）")

    p.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试用）")
    p.add_argument("--driver-path", default=None,
                   help="chromedriver 路径。不给则由 Selenium Manager 自动下载（需联网）；"
                        "也可用环境变量 PDL_CHROMEDRIVER")
    p.add_argument("--chrome-binary", default=None,
                   help="Chrome 可执行文件路径（装在非默认位置时用）；也可用环境变量 PDL_CHROME_BINARY")
    p.add_argument("--delay", type=float, default=1.0, help="每条之间的基础间隔秒数")
    p.add_argument("--max-retries", type=int, default=3, help="单条目累计失败多少次后标记为 failed")
    p.add_argument("--page-retries", type=int, default=3, help="单次页面加载的重试次数")
    p.add_argument("--retry-failed", action="store_true", help="重跑此前标记为 failed 的条目")
    p.add_argument("--restart-every", type=int, default=200, help="每处理多少条重启一次浏览器")
    p.add_argument("--save-every", type=int, default=5, help="每处理多少条写一次 state.json")

    p.add_argument("--min-speed", type=float, default=80_000, help="下载超时按此速度（字节/秒）估算")
    p.add_argument("--min-timeout", type=float, default=180, help="单文件下载最短超时秒数")
    p.add_argument("--stall-timeout", type=float, default=120, help="多少秒无进度判为卡死")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return Crawler(args).run()
