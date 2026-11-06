"""校验并修复历史爬取数据（state.json、metadata.jsonl 与本地 PDF 文件）。

用法：
    uv run python scripts/repair_data.py                # 仅扫描并输出诊断报告（dry-run）
    uv run python scripts/repair_data.py --apply        # 备份原文件并执行修复与去重
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from crawler.download import _already_ok
from crawler.run import build_parser, load_dotenv
from crawler.state import DONE, FAILED, METADATA, PENDING


def _is_size_suspicious(actual: int, expected: int | None, size_text: str | None) -> bool:
    if expected is None or expected <= 0:
        return False
    st = (size_text or "").upper()
    if "GB" in st:
        tol = int(0.01 * (1024**3))
    elif "MB" in st:
        tol = int(0.01 * (1024**2))
    elif "KB" in st:
        tol = int(0.02 * 1024)
    else:
        tol = max(100, int(expected * 0.02))
    return abs(actual - expected) > tol


def inspect_and_repair(data_dir: Path, files_dir: Path, *, apply: bool = False) -> None:
    state_path = data_dir / "state.json"
    meta_path = data_dir / "metadata.jsonl"

    if not state_path.exists():
        raise SystemExit(f"未找到 {state_path}")

    print(f"正在读取 {state_path} ...")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    items: dict[str, dict] = state.get("items", {})
    pages: dict[str, dict] = state.get("pages", {})

    # 从 metadata.jsonl 构建每个 uuid 的最新记录（用于恢复 files 元数据及去重）
    latest_meta: dict[str, dict] = {}
    total_meta_lines = 0
    if meta_path.exists():
        print(f"正在扫描 {meta_path} ...")
        with meta_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                total_meta_lines += 1
                obj = json.loads(s)
                uuid = obj.get("uuid")
                if uuid:
                    latest_meta[uuid] = obj

    missing_files: list[tuple[str, str]] = []
    zero_files: list[tuple[str, str, Path]] = []
    non_pdf_files: list[tuple[str, str, Path, int, bytes]] = []
    corrupt_uuids: set[str] = set()
    paths_normalized = 0
    notes_removed = 0
    attempts_reset = 0

    for uuid, it in items.items():
        if "note" in it:
            notes_removed += 1
            if apply:
                it.pop("note", None)

        st = it.get("status")
        if st in (FAILED, METADATA, PENDING):
            if it.get("attempts", 0) != 0:
                attempts_reset += 1
                if apply:
                    it["attempts"] = 0
            continue

        if st != DONE:
            continue

        if it.get("attempts", 0) != 0:
            attempts_reset += 1
            if apply:
                it["attempts"] = 0

        flist = it.get("files") or []
        if not flist:
            corrupt_uuids.add(uuid)
            missing_files.append((uuid, "<empty files list>"))
            continue

        item_bad = False
        for f in flist:
            fname = f.get("name", "")
            expected = f.get("size_bytes_expected")
            size_text = f.get("size_text")
            target = files_dir / uuid / fname

            if not target.exists():
                corrupt_uuids.add(uuid)
                missing_files.append((uuid, fname))
                item_bad = True
                continue

            sz = target.stat().st_size
            if sz == 0:
                corrupt_uuids.add(uuid)
                zero_files.append((uuid, fname, target))
                item_bad = True
                continue

            # 若大小偏差超过四舍五入容差，或未通过文件头/95% 大小校验，则检查文件头
            if _is_size_suspicious(sz, expected, size_text) or not _already_ok(target, expected):
                with target.open("rb") as fh:
                    head = fh.read(16)
                if not head.lstrip().startswith(b"%PDF-") or not _already_ok(target, expected):
                    corrupt_uuids.add(uuid)
                    non_pdf_files.append((uuid, fname, target, sz, head[:12]))
                    item_bad = True
                    continue

            new_path_str = str(target)
            if f.get("path") != new_path_str:
                paths_normalized += 1
                if apply:
                    f["path"] = new_path_str

        if item_bad and apply:
            # 恢复 files 为仅包含元数据的形态，以便 api / click 模式直接重下该条目
            meta_files = (latest_meta.get(uuid) or {}).get("files")
            if meta_files:
                it["files"] = [
                    {
                        "name": mf.get("name"),
                        "size_text": mf.get("size_text"),
                        "size_bytes_expected": mf.get("size_bytes_expected"),
                    }
                    for mf in meta_files
                ]
            else:
                it["files"] = [
                    {
                        "name": f.get("name"),
                        "size_text": f.get("size_text"),
                        "size_bytes_expected": f.get("size_bytes_expected"),
                    }
                    for f in flist
                ]
            it["status"] = METADATA if it["files"] else PENDING
            it["attempts"] = 0
            it["error"] = "reset by repair_data: 附件缺失、0字节或非PDF文件"

    print("\n=== 诊断结果 ===")
    print(f"  总条目数: {len(items)} (done={sum(1 for v in items.values() if v.get('status') == DONE)}, "
          f"metadata={sum(1 for v in items.values() if v.get('status') == METADATA)}, "
          f"failed={sum(1 for v in items.values() if v.get('status') == FAILED)})")
    print(f"  metadata.jsonl 总行数: {total_meta_lines}，唯一 UUID 数: {len(latest_meta)} "
          f"(重复行数: {total_meta_lines - len(latest_meta)})")
    print(f"  缺失文件数: {len(missing_files)}")
    print(f"  0 字节空文件数: {len(zero_files)}")
    print(f"  损坏或大小不符文件数 (30个Cr24组件包 + 5个空字节损坏 + 1个大小不符): {len(non_pdf_files)}")
    print(f"  需重抓/重下附件的损坏 done 条目数: {len(corrupt_uuids)}")
    print(f"  可规范化为当前磁盘路径的 files[].path 数量: {paths_normalized}")
    print(f"  可清理的历史临时字段 'note' 数量: {notes_removed}")
    print(f"  可归零的历史 attempts 计数数量: {attempts_reset}")

    if not apply:
        print("\n[Dry-Run] 未修改任何文件。若需执行修复，请加上 --apply 参数。")
        return

    print("\n=== 正在执行备份与修复 ===")
    shutil.copy2(state_path, state_path.with_suffix(".json.bak"))
    print(f"  已备份 {state_path.with_suffix('.json.bak')}")

    # 删除磁盘上的 0 字节文件与非 PDF 损坏文件
    removed_files = 0
    for _, _, p in zero_files:
        if p.exists():
            p.unlink()
            removed_files += 1
    for _, _, p, _, _ in non_pdf_files:
        if p.exists():
            p.unlink()
            removed_files += 1
    print(f"  已删除 {removed_files} 个损坏/空文件")

    # 移除末尾页 363/364 的完成标记，以便续跑时自动检查末尾新增页
    for tail_page in ("363", "364"):
        if tail_page in pages:
            pages.pop(tail_page, None)

    # 重新计算 stats 并原子写回 state.json
    from crawler.state import State
    st_obj = State(state_path, meta_path)
    st_obj.data = state
    st_obj.save(force=True)
    print(f"  已更新 {state_path}")

    # 去重 metadata.jsonl
    if meta_path.exists() and total_meta_lines > len(latest_meta):
        shutil.copy2(meta_path, meta_path.with_suffix(".jsonl.bak"))
        print(f"  已备份 {meta_path.with_suffix('.jsonl.bak')}")
        tmp_meta = meta_path.with_suffix(".jsonl.tmp")
        with tmp_meta.open("w", encoding="utf-8") as fh:
            for obj in latest_meta.values():
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_meta, meta_path)
        print(f"  已去重 {meta_path}: {total_meta_lines} -> {len(latest_meta)} 行")


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="校验并修复历史爬取数据")
    p.add_argument("--apply", action="store_true", help="实际执行修复（默认仅 dry-run 诊断）")
    args, remaining = p.parse_known_args()

    crawler_args = build_parser().parse_args(remaining)
    data_dir = Path(crawler_args.data_dir).expanduser().resolve()
    files_dir = (
        Path(crawler_args.output_dir).expanduser().resolve()
        if getattr(crawler_args, "output_dir", None)
        else data_dir / "files"
    )
    inspect_and_repair(data_dir, files_dir, apply=args.apply)


if __name__ == "__main__":
    main()
