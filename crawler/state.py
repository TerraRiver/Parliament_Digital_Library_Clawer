"""断点续跑的状态存储。

state.json 记录爬取进度（哪些页翻过了、哪些条目抓过了/下载过了），
metadata.jsonl 追加写入每个条目的完整元数据。
两者都是崩溃后可直接续跑的依据。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

STATE_VERSION = 1

# 条目状态
PENDING = "pending"  # 已在列表页发现，尚未处理
METADATA = "metadata"  # 元数据已抓，文件未下完
DONE = "done"  # 元数据 + 文件全部完成
NO_FILE = "no_file"  # 条目下没有可下载文件
FAILED = "failed"  # 重试耗尽


class State:
    def __init__(self, path: Path, metadata_path: Path):
        self.path = path
        self.metadata_path = metadata_path
        self._lock = threading.Lock()
        self._dirty = 0
        self.data: dict[str, Any] = {
            "version": STATE_VERSION,
            "search_url": None,
            "page_size": None,
            "created_at": now(),
            "updated_at": now(),
            "pages": {},  # "1" -> {"status": "done", "count": 100}
            "items": {},  # uuid -> item record
            "stats": {},
        }
        if path.exists():
            self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            backup = self.path.with_suffix(f".corrupt.{int(time.time())}.json")
            self.path.rename(backup)
            print(f"[state] 状态文件损坏（{exc}），已备份到 {backup}，从零开始")
            return
        if loaded.get("version") != STATE_VERSION:
            print(f"[state] 状态文件版本 {loaded.get('version')} 与当前 {STATE_VERSION} 不符，仍尝试载入")
        self.data.update(loaded)
        self.data.setdefault("pages", {})
        self.data.setdefault("items", {})
        print(
            f"[state] 已载入：{len(self.data['pages'])} 页，{len(self.data['items'])} 条目"
            f"（其中完成 {self.count(DONE)}）"
        )

    def save(self, force: bool = False, every: int = 1) -> None:
        """原子写盘：先写临时文件再 rename，避免中断产生半截 JSON。"""
        with self._lock:
            self._dirty += 1
            if not force and self._dirty < every:
                return
            self._dirty = 0
            self.data["updated_at"] = now()
            self.data["stats"] = self.summary()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def append_metadata(self, record: dict[str, Any]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with self.metadata_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------- 列表页 ----------

    def page_done(self, page: int) -> bool:
        return self.data["pages"].get(str(page), {}).get("status") == "done"

    def mark_page(self, page: int, status: str, count: int) -> None:
        self.data["pages"][str(page)] = {"status": status, "count": count, "at": now()}

    # ---------- 条目 ----------

    def get(self, uuid: str) -> dict[str, Any] | None:
        return self.data["items"].get(uuid)

    def add_discovered(self, uuid: str, title: str, url: str, page: int, extra: dict | None = None) -> bool:
        """列表页发现的新条目。返回 True 表示是新增的。"""
        if uuid in self.data["items"]:
            return False
        self.data["items"][uuid] = {
            "uuid": uuid,
            "title": title,
            "url": url,
            "list_page": page,
            "status": PENDING,
            "attempts": 0,
            "files": [],
            "error": None,
            "updated_at": now(),
            **(extra or {}),
        }
        return True

    def update(self, uuid: str, **fields: Any) -> dict[str, Any]:
        rec = self.data["items"][uuid]
        rec.update(fields)
        rec["updated_at"] = now()
        return rec

    def needs_work(self, uuid: str, retry_failed: bool, max_retries: int, recheck_no_file: bool = False) -> bool:
        rec = self.data["items"].get(uuid)
        if rec is None:
            return True
        if rec["status"] == NO_FILE:
            return recheck_no_file
        if rec["status"] == DONE:
            return False
        if rec["status"] == FAILED and not retry_failed:
            return False
        return rec.get("attempts", 0) < max_retries or retry_failed

    # ---------- 统计 ----------

    def count(self, status: str) -> int:
        return sum(1 for r in self.data["items"].values() if r["status"] == status)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self.data["items"].values():
            out[rec["status"]] = out.get(rec["status"], 0) + 1
        out["total_items"] = len(self.data["items"])
        out["pages_done"] = sum(1 for p in self.data["pages"].values() if p["status"] == "done")
        out["downloaded_bytes"] = sum(
            f.get("bytes") or 0 for r in self.data["items"].values() for f in r.get("files", [])
        )
        return out


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
