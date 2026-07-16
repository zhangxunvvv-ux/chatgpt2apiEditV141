from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.log_service import LOG_TYPE_ACCOUNT, LOG_TYPE_CALL, LogService


class LogServiceTailTests(unittest.TestCase):
    def test_tail_returns_newest_matching_items_without_full_list_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            for index in range(20):
                service.add(LOG_TYPE_CALL if index % 2 else LOG_TYPE_ACCOUNT, f"event-{index}")

            items = service.tail(LOG_TYPE_CALL, limit=3, scan_limit=10)

            self.assertEqual([item["summary"] for item in items], ["event-19", "event-17", "event-15"])

    def test_recent_line_reader_preserves_utf8_across_small_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            service.add(LOG_TYPE_CALL, "第一次生图")
            service.add(LOG_TYPE_CALL, "第二次生图")

            lines = list(service._recent_raw_lines(2, chunk_size=5))

            self.assertIn("第二次生图", lines[0])
            self.assertIn("第一次生图", lines[1])


if __name__ == "__main__":
    unittest.main()
