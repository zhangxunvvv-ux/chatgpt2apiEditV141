from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services.config import config
from services.image_task_service import ImageTaskService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            self.assertEqual(first["status"], "running")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_redundant_generation_returns_first_success_without_waiting_for_slow_copy(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            config.data,
            {
                "image_redundant_generation_enabled": True,
                "image_redundant_copies": 2,
                "image_redundant_max_attempts": 1,
            },
        ):
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            call_lock = threading.Lock()
            slow_started = threading.Event()
            slow_cancelled = threading.Event()
            calls = 0

            def handler(payload):
                nonlocal calls
                with call_lock:
                    calls += 1
                    current = calls
                if current == 1:
                    slow_started.set()
                    while not payload["cancel_event"].is_set():
                        time.sleep(0.005)
                    slow_cancelled.set()
                    raise RuntimeError("cancelled loser")
                self.assertTrue(slow_started.wait(0.5))
                return {"data": [{"url": "http://example.test/fast.png"}]}

            started = time.monotonic()
            result = service._run_redundant_handler(
                handler,
                {"cancel_event": threading.Event(), "_batch_size": 1},
                "missing-task",
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result["data"][0]["url"], "http://example.test/fast.png")
            self.assertLess(elapsed, 0.5)
            self.assertTrue(slow_cancelled.wait(1.0))
            self.assertEqual(calls, 2)

    def test_batch_generation_disables_redundant_copies(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            config.data,
            {
                "image_redundant_generation_enabled": True,
                "image_redundant_copies": 2,
                "image_redundant_max_attempts": 3,
            },
        ):
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                return {"data": [{"url": "http://example.test/batch.png"}]}

            result = service._run_redundant_handler(
                handler,
                {"cancel_event": threading.Event(), "_batch_size": 10},
                "missing-task",
            )

            self.assertEqual(result["data"][0]["url"], "http://example.test/batch.png")
            self.assertEqual(calls, 1)

            def failing_handler(_payload):
                nonlocal calls
                calls += 1
                raise RuntimeError("batch attempt failed")

            calls = 0
            with self.assertRaisesRegex(RuntimeError, "batch attempt failed"):
                service._run_redundant_handler(
                    failing_handler,
                    {"cancel_event": threading.Event(), "_batch_size": 10},
                    "missing-task",
                )
            self.assertEqual(calls, 1)

    def test_live_worker_is_not_marked_failed_by_outer_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            config.data,
            {
                "image_poll_timeout_secs": 1,
                "image_redundant_generation_enabled": False,
            },
        ):
            started = threading.Event()
            release = threading.Event()

            def handler(_payload):
                started.set()
                self.assertTrue(release.wait(2.0))
                return {"data": [{"url": "http://example.test/late-success.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            service.submit_generation(
                OWNER,
                client_task_id="slow-success",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            self.assertTrue(started.wait(1.0))
            key = "owner-1:slow-success"
            with service._lock:
                service._tasks[key]["created_ts"] = time.time() - 100

            running = service.list_tasks(OWNER, ["slow-success"])["items"][0]
            self.assertEqual(running["status"], "running")

            release.set()
            completed = wait_for_task(service, OWNER, "slow-success", "success")
            self.assertEqual(completed["data"][0]["url"], "http://example.test/late-success.png")

    def test_stop_task_signals_running_handler(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            config.data,
            {"image_redundant_generation_enabled": False},
        ):
            started = threading.Event()
            cancelled = threading.Event()

            def handler(payload):
                started.set()
                while not payload["cancel_event"].is_set():
                    time.sleep(0.005)
                cancelled.set()
                raise RuntimeError("stopped")

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            service.submit_generation(
                OWNER,
                client_task_id="stoppable",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            self.assertTrue(started.wait(1.0))

            stopped = service.stop_task(OWNER, "stoppable")

            self.assertEqual(stopped["status"], "error")
            self.assertTrue(cancelled.wait(1.0))
            deadline = time.time() + 1.0
            while time.time() < deadline and service._workers:
                time.sleep(0.01)
            self.assertEqual(service._workers, {})


if __name__ == "__main__":
    unittest.main()
