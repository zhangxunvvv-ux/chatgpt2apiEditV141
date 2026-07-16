from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import register_service as register_service_module


class RegisterConcurrencyTests(unittest.TestCase):
    def test_start_applies_three_threads_atomically_and_runs_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            release = threading.Event()
            all_started = threading.Event()
            state_lock = threading.Lock()
            active = 0
            max_active = 0

            def worker(index: int) -> dict:
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == 3:
                        all_started.set()
                release.wait(5)
                with state_lock:
                    active -= 1
                return {"ok": True, "index": index}

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register, "worker", side_effect=worker
            ):
                try:
                    started = service.start({"threads": 3, "total": 3, "mode": "total"})
                    self.assertEqual(started["threads"], 3)
                    self.assertEqual(started["stats"]["threads"], 3)
                    self.assertTrue(all_started.wait(2), "three workers did not start concurrently")
                    running_deadline = time.monotonic() + 2
                    while service.get()["stats"]["running"] != 3 and time.monotonic() < running_deadline:
                        time.sleep(0.01)
                    self.assertEqual(service.get()["stats"]["running"], 3)
                finally:
                    release.set()
                deadline = time.monotonic() + 3
                while service.get()["enabled"] and time.monotonic() < deadline:
                    time.sleep(0.01)

            self.assertEqual(max_active, 3)
            self.assertFalse(service.get()["enabled"])


if __name__ == "__main__":
    unittest.main()
