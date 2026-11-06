"""核心纯函数、流式下载分支与状态机的单元测试。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from crawler.download import (
    DownloadError,
    _already_ok,
    _deduplicate_filename,
    _match_bitstream,
    _parse_content_range_total,
    _stream_download,
    _validate_downloaded_file,
    safe_filename,
)
from crawler.pages import _is_list_page_eof, build_page_url, parse_size
from crawler.run import Crawler, Interrupted, build_parser
from crawler.state import DONE, FAILED, METADATA, NO_FILE, PENDING, State


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size: int = 1024):
        yield from self._chunks

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class TestPagesUtils(unittest.TestCase):
    def test_build_page_url_preserves_duplicate_keys(self) -> None:
        url = (
            "https://elibrary.sansad.in/search"
            "?query=china&f.author=Nehru&f.author=Patel&spc.page=1&spc.rpp=20"
        )
        out = build_page_url(url, page=3, page_size=100)
        self.assertIn("f.author=Nehru", out)
        self.assertIn("f.author=Patel", out)
        self.assertIn("spc.page=3", out)
        self.assertIn("spc.rpp=100", out)
        self.assertNotIn("spc.page=1", out)

    def test_parse_size(self) -> None:
        self.assertEqual(parse_size("1024 B"), 1024)
        self.assertEqual(parse_size("1.5 KB"), 1536)
        self.assertEqual(parse_size("39.07 MB"), int(39.07 * 1024**2))
        self.assertIsNone(parse_size(None))
        self.assertIsNone(parse_size("invalid"))

    def test_is_list_page_eof(self) -> None:
        # 1. 页面加载慢，分页条和无结果提示都未出来（total=None, no_result=False） -> 绝不能判为到底
        self.assertFalse(_is_list_page_eof({"loading": False, "total": None, "no_result": False}, page=2, page_size=100))
        # 2. 仍在加载中 -> False
        self.assertFalse(_is_list_page_eof({"loading": True, "total": 100, "no_result": True}, page=5, page_size=100))
        # 3. 明确出现 Your search returned no results. 且无 total -> True
        self.assertTrue(_is_list_page_eof({"loading": False, "total": None, "no_result": True}, page=365, page_size=100))
        # 4. 页码未超出总条数 -> False
        self.assertFalse(_is_list_page_eof({"loading": False, "total": 250, "no_result": False}, page=3, page_size=100))
        # 5. 页码已超出总条数 -> True
        self.assertTrue(_is_list_page_eof({"loading": False, "total": 250, "no_result": False}, page=4, page_size=100))
        # 6. total 与 no_result 冲突时以 total 数字为准
        self.assertFalse(_is_list_page_eof({"loading": False, "total": 36263, "no_result": True}, page=5, page_size=100))


class TestDownloadUtils(unittest.TestCase):
    def test_safe_filename_and_deduplication(self) -> None:
        self.assertEqual(safe_filename("a/b:c*?.pdf"), "a_b_c__.pdf")
        self.assertEqual(safe_filename("CON.pdf"), "_CON.pdf")
        self.assertEqual(safe_filename("trailing. "), "trailing")

        used: set[str] = set()
        f1 = _deduplicate_filename("Report.pdf", used)
        f2 = _deduplicate_filename("report.pdf", used)  # 大小写冲突
        f3 = _deduplicate_filename("Report.pdf", used)
        self.assertEqual(f1, "Report.pdf")
        self.assertEqual(f2, "report_2.pdf")
        self.assertEqual(f3, "Report_3.pdf")

    def test_match_bitstream_strict_on_multiple_files(self) -> None:
        candidates = [
            ("AU1665.pdf", "https://example.com/1"),
            ("AU1665_hindi.pdf", "https://example.com/2"),
        ]
        self.assertEqual(_match_bitstream(candidates, "AU1665_hindi.pdf"), "https://example.com/2")
        self.assertEqual(_match_bitstream(candidates, " au1665.pdf "), "https://example.com/1")
        with self.assertRaises(DownloadError):
            _match_bitstream(candidates, "nonexistent.pdf")

        # 单文件条目允许兜底
        single = [("actual.pdf", "https://example.com/only")]
        self.assertEqual(_match_bitstream(single, "display_name.pdf"), "https://example.com/only")

    def test_validate_downloaded_file_and_already_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p_pdf = Path(tmp) / "valid.pdf"
            p_pdf.write_bytes(b"%PDF-1.4\n..." + b"0" * 5000)
            self.assertTrue(_validate_downloaded_file(p_pdf))
            self.assertTrue(_already_ok(p_pdf, expected=5000))

            p_html = Path(tmp) / "error.pdf"
            p_html.write_bytes(b"<!DOCTYPE html><html><body>Error</body></html>")
            self.assertFalse(_validate_downloaded_file(p_html))
            self.assertFalse(_already_ok(p_html, expected=None))

    def test_parse_content_range_total(self) -> None:
        self.assertEqual(_parse_content_range_total("bytes */371200"), 371200)
        self.assertEqual(_parse_content_range_total("bytes 0-99/371200"), 371200)
        self.assertIsNone(_parse_content_range_total("bytes */*"))
        self.assertIsNone(_parse_content_range_total(None))

    def test_stream_download_416_full_part_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "doc.pdf"
            part = target.with_suffix(".pdf.part")
            payload = b"%PDF-1.5\nfull-content-bytes"
            part.write_bytes(payload)

            session = MagicMock()
            session.get.return_value = _FakeResponse(
                status_code=416,
                headers={"Content-Range": f"bytes */{len(payload)}"},
            )
            size = _stream_download(session, "https://example.com/doc.pdf", target)
            self.assertEqual(size, len(payload))
            self.assertTrue(target.exists())
            self.assertFalse(part.exists())

    def test_stream_download_incomplete_bytes_keeps_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "doc.pdf"
            part = target.with_suffix(".pdf.part")
            chunk = b"%PDF-1.5\npartial"

            session = MagicMock()
            session.get.return_value = _FakeResponse(
                status_code=200,
                headers={"Content-Type": "application/pdf", "Content-Length": "1000"},
                chunks=[chunk],
            )
            with self.assertRaises(requests.RequestException):
                _stream_download(session, "https://example.com/doc.pdf", target)
            # .part 应保留以便下次 Range 续传，target 不应存在
            self.assertTrue(part.exists())
            self.assertEqual(part.stat().st_size, len(chunk))
            self.assertFalse(target.exists())

    def test_stream_download_rejects_html_error_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "doc.pdf"
            session = MagicMock()
            session.get.return_value = _FakeResponse(
                status_code=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                chunks=[b"<html><body>502 Bad Gateway</body></html>"],
            )
            with self.assertRaises(requests.RequestException):
                _stream_download(session, "https://example.com/doc.pdf", target)
            self.assertFalse(target.exists())


class TestStateAndAttempts(unittest.TestCase):
    def test_needs_work_and_two_stage_metadata_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st = State(Path(tmp) / "state.json", Path(tmp) / "metadata.jsonl")
            uuid = "11111111-2222-3333-4444-555555555555"
            st.add_discovered(uuid, "Title", "https://example.com/item", page=1)
            self.assertTrue(st.needs_work(uuid, retry_failed=False, max_retries=3, no_download=True))

            # 阶段 1：--no-download 抓完元数据
            st.update(uuid, status=METADATA, files=[{"name": "a.pdf"}], attempts=0)
            self.assertFalse(st.needs_work(uuid, retry_failed=False, max_retries=3, no_download=True))
            # 阶段 2：正常跑下载时应识别为待处理
            self.assertTrue(st.needs_work(uuid, retry_failed=False, max_retries=3, no_download=False))

            # 完成后均跳过
            st.update(uuid, status=DONE, attempts=0)
            self.assertFalse(st.needs_work(uuid, retry_failed=False, max_retries=3, no_download=False))

            # NO_FILE 仅在 recheck_no_file=True 时重跑
            st.update(uuid, status=NO_FILE)
            self.assertFalse(st.needs_work(uuid, retry_failed=False, max_retries=3, recheck_no_file=False))
            self.assertTrue(st.needs_work(uuid, retry_failed=False, max_retries=3, recheck_no_file=True))

            # FAILED 仅在 retry_failed=True 时重跑
            st.update(uuid, status=FAILED, attempts=3)
            self.assertFalse(st.needs_work(uuid, retry_failed=False, max_retries=3))
            self.assertTrue(st.needs_work(uuid, retry_failed=True, max_retries=3))

    @patch("crawler.run.make_driver")
    def test_process_item_attempts_only_on_failure_and_cleared_on_success(self, mock_make_driver: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            args = build_parser().parse_args(
                ["--data-dir", tmp, "--no-download", "--delay", "0", "--page-retries", "1", "--max-retries", "3"]
            )
            crawler = Crawler(args)
            uuid = "22222222-3333-4444-5555-666666666666"
            crawler.state.add_discovered(uuid, "Doc Title", "https://example.com/item", page=1)
            row = crawler.state.get(uuid)

            # 1. 失败一次 -> attempts 变为 1，状态保持 PENDING
            with patch("crawler.run.pages.scrape_item_page", side_effect=RuntimeError("network error")):
                crawler._process_item(row)
            self.assertEqual(crawler.state.get(uuid)["attempts"], 1)
            self.assertEqual(crawler.state.get(uuid)["status"], PENDING)

            # 2. 用户 Ctrl+C 中断 -> 不增加 attempts
            with patch("crawler.run.pages.scrape_item_page", side_effect=Interrupted()):
                with self.assertRaises(Interrupted):
                    crawler._process_item(row)
            self.assertEqual(crawler.state.get(uuid)["attempts"], 1)

            # 3. 随后成功 -> attempts 清零，状态变为 METADATA
            fake_item_data = {
                "fields": {"Title": ["Doc Title"]},
                "files": [{"name": "doc.pdf", "size_text": "1 MB", "size_bytes_expected": 1048576}],
                "title": "Doc Title",
            }
            with patch("crawler.run.pages.scrape_item_page", return_value=fake_item_data):
                crawler._process_item(row)
            self.assertEqual(crawler.state.get(uuid)["attempts"], 0)
            self.assertEqual(crawler.state.get(uuid)["status"], METADATA)


if __name__ == "__main__":
    unittest.main()
