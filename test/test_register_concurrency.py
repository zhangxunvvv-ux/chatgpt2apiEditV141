from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import register_service as register_service_module


class RegisterConcurrencyTests(unittest.TestCase):
    @staticmethod
    def wait_until(predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_only_explicit_rate_limits_trigger_immediate_backoff(self) -> None:
        self.assertEqual(register_service_module._immediate_backoff_reason("account_creation_failed"), "")
        self.assertEqual(register_service_module._immediate_backoff_reason("Could not resolve host"), "")
        self.assertEqual(register_service_module._immediate_backoff_reason("等待注册验证码超时"), "")
        self.assertEqual(register_service_module._immediate_backoff_reason("HTTP 429 Too Many Requests"), "rate_limit")

    def test_normalize_keeps_tempmail_domains_without_legacy_cooldown(self) -> None:
        config = register_service_module._normalize(
            {
                "mail": {
                    "providers": [
                        {
                            "type": "tempmail_lol",
                            "enable": True,
                            "api_key": "test-key",
                            "domain": "First.Example\nsecond.example,first.example",
                            "domain_cooldown_threshold": 3,
                            "domain_cooldown_seconds": 21600,
                        }
                    ]
                }
            }
        )

        provider = config["mail"]["providers"][0]
        self.assertEqual(provider["domain"], ["first.example", "second.example"])
        self.assertNotIn("domain_cooldown_threshold", provider)
        self.assertNotIn("domain_cooldown_seconds", provider)

    def test_normalize_clears_stale_runtime_state_when_disabled(self) -> None:
        config = register_service_module._normalize(
            {
                "enabled": False,
                "stats": {
                    "running": 2,
                    "retry_at": "2099-01-01T00:00:00+00:00",
                    "pause_reason": "account_creation_risk",
                },
            }
        )

        self.assertEqual(config["stats"]["running"], 0)
        self.assertIsNone(config["stats"]["retry_at"])
        self.assertEqual(config["stats"]["pause_reason"], "")

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

    def test_total_mode_counts_successes_instead_of_failed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            results = iter(
                [
                    {"ok": False, "error": "等待注册验证码超时"},
                    {"ok": False, "error": "registration failed"},
                    {"ok": True},
                ]
            )

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register, "worker", side_effect=lambda _index: next(results)
            ) as worker:
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"]))

            stats = service.get()["stats"]
            self.assertEqual(worker.call_count, 3)
            self.assertEqual(stats["success"], 1)
            self.assertEqual(stats["fail"], 2)
            self.assertEqual(stats["done"], 3)

    def test_verification_failure_continues_without_global_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            attempt_times: list[float] = []

            def worker(_index: int) -> dict:
                attempt_times.append(time.monotonic())
                return {"ok": len(attempt_times) > 1, "error": "等待注册验证码超时"}

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register, "worker", side_effect=worker
            ):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                        "failure_backoff_seconds": 1,
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            self.assertEqual(len(attempt_times), 2)
            self.assertLess(attempt_times[1] - attempt_times[0], 0.5)
            log_text = "\n".join(item["text"] for item in service.get()["logs"])
            self.assertNotIn("HTTP 429", log_text)
            self.assertNotIn("自动恢复", log_text)

    def test_account_creation_failure_continues_without_global_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            attempt_times: list[float] = []

            def worker(_index: int) -> dict:
                attempt_times.append(time.monotonic())
                if len(attempt_times) == 1:
                    return {"ok": False, "error": "user_register_http_400: account_creation_failed"}
                return {"ok": True}

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register, "worker", side_effect=worker
            ):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                        "failure_backoff_seconds": 1,
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            self.assertEqual(len(attempt_times), 2)
            self.assertLess(attempt_times[1] - attempt_times[0], 0.5)
            log_text = "\n".join(item["text"] for item in service.get()["logs"])
            self.assertNotIn("HTTP 429", log_text)
            self.assertNotIn("自动恢复", log_text)

    def test_http_429_enters_backoff_then_resumes_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            attempt_times: list[float] = []

            def worker(_index: int) -> dict:
                attempt_times.append(time.monotonic())
                if len(attempt_times) == 1:
                    return {"ok": False, "error": "mail request failed: HTTP 429 Too Many Requests"}
                return {"ok": True}

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register, "worker", side_effect=worker
            ):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                        "failure_backoff_seconds": 1,
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            self.assertEqual(len(attempt_times), 2)
            self.assertGreaterEqual(attempt_times[1] - attempt_times[0], 0.9)
            log_text = "\n".join(item["text"] for item in service.get()["logs"])
            self.assertIn("HTTP 429/明确限流", log_text)
            self.assertIn("自动恢复", log_text)

    def test_manual_stop_interrupts_long_failure_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register,
                "worker",
                return_value={"ok": False, "error": "HTTP 429 Too Many Requests"},
            ):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                        "failure_backoff_seconds": 1200,
                    }
                )
                self.assertTrue(self.wait_until(lambda: bool(service.get()["stats"].get("retry_at"))))
                service.stop()
                self.assertTrue(self.wait_until(lambda: not service._runner or not service._runner.is_alive(), timeout=3))

            self.assertFalse(service.get()["enabled"])
            self.assertIsNone(service.get()["stats"].get("retry_at"))

    def test_scheduler_exception_is_supervised_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            original_run_loop = service._run_loop
            calls = 0

            def flaky_run_loop() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary scheduler failure")
                original_run_loop()

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                service, "_run_loop", side_effect=flaky_run_loop
            ), mock.patch.object(register_service_module.openai_register, "worker", return_value={"ok": True}):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                        "failure_backoff_seconds": 1,
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            stats = service.get()["stats"]
            self.assertEqual(calls, 2)
            self.assertEqual(stats["scheduler_restarts"], 1)
            self.assertEqual(stats["success"], 1)


if __name__ == "__main__":
    unittest.main()
