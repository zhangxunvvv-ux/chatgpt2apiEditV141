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

    def test_normalize_removes_legacy_failure_cooldown(self) -> None:
        config = register_service_module._normalize({"failure_backoff_seconds": 1200})

        self.assertNotIn("failure_backoff_seconds", config)

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
                            "rate_per_window": 24,
                            "window_seconds": 300,
                            "rate_limit_cooldown_seconds": 600,
                            "max_wait": 600,
                            "create_total_budget": 90,
                        }
                    ]
                }
            }
        )

        provider = config["mail"]["providers"][0]
        self.assertEqual(provider["domain"], ["first.example", "second.example"])
        self.assertNotIn("domain_cooldown_threshold", provider)
        self.assertNotIn("domain_cooldown_seconds", provider)
        self.assertNotIn("rate_per_window", provider)
        self.assertNotIn("window_seconds", provider)
        self.assertNotIn("rate_limit_cooldown_seconds", provider)
        self.assertNotIn("max_wait", provider)
        self.assertNotIn("create_total_budget", provider)

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

            def worker(index: int, _stop_event: threading.Event, generation: int) -> dict:
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == 3:
                        all_started.set()
                release.wait(5)
                with state_lock:
                    active -= 1
                return {"ok": True, "index": index, "generation": generation}

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

    def test_restart_applies_latest_provider_flags_while_old_runner_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            register_service_module.openai_register.config,
            {},
            clear=False,
        ):
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            first_started = threading.Event()
            generations: list[int] = []

            def worker(index: int, stop_event: threading.Event, generation: int) -> dict:
                generations.append(generation)
                if generation == 1:
                    first_started.set()
                    stop_event.wait(3)
                    return {"ok": False, "cancelled": True, "index": index, "generation": generation}
                return {"ok": True, "index": index, "generation": generation}

            providers = [
                {
                    "type": "cloudflare_temp_email",
                    "enable": False,
                    "api_base": "https://mail.example.test",
                    "admin_password": "secret",
                    "domain": [],
                },
                {
                    "type": "tempmail_lol",
                    "enable": True,
                    "api_key": "test-key",
                    "domain": [],
                },
            ]

            with mock.patch.object(
                service,
                "_pool_metrics",
                return_value={"current_quota": 0, "current_available": 0},
            ), mock.patch.object(register_service_module.openai_register, "worker", side_effect=worker):
                first = service.start({"threads": 1, "total": 1, "mode": "total"})
                self.assertEqual(first["stats"]["generation"], 1)
                self.assertTrue(first_started.wait(1))
                result = service.start(
                    {"threads": 1, "total": 1, "mode": "total", "mail": {"providers": providers}}
                )
                self.assertEqual(result["stats"]["generation"], 2)
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=3))

            self.assertFalse(result["mail"]["providers"][0]["enable"])
            self.assertTrue(result["mail"]["providers"][1]["enable"])
            runtime_providers = register_service_module.openai_register.config["mail"]["providers"]
            self.assertFalse(runtime_providers[0]["enable"])
            self.assertTrue(runtime_providers[1]["enable"])
            self.assertEqual(generations, [1, 2])
            self.assertEqual(service.get()["stats"]["generation"], 2)
            self.assertEqual(service.get()["stats"]["success"], 1)
            self.assertEqual(service.get()["stats"]["fail"], 0)

    def test_start_reapplies_stored_provider_config_before_launching_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            providers = [
                {
                    "type": "tempmail_lol",
                    "enable": True,
                    "api_key": "test-key",
                    "domain": ["mail.example"],
                }
            ]
            service.update({"threads": 1, "total": 1, "mode": "total", "mail": {"providers": providers}})
            register_service_module.openai_register.config["mail"] = {
                "providers": [{"type": "cloudflare_temp_email", "enable": True}]
            }

            with mock.patch.object(
                service,
                "_pool_metrics",
                return_value={"current_quota": 0, "current_available": 0},
            ), mock.patch.object(
                register_service_module.openai_register,
                "worker",
                return_value={"ok": True, "generation": 1},
            ):
                service.start()
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=3))

            runtime_providers = register_service_module.openai_register.config["mail"]["providers"]
            self.assertEqual(runtime_providers, providers)

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
                register_service_module.openai_register, "worker", side_effect=lambda *_args: next(results)
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

            def worker(_index: int, _stop_event: threading.Event, _generation: int) -> dict:
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

            def worker(_index: int, _stop_event: threading.Event, _generation: int) -> dict:
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
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            self.assertEqual(len(attempt_times), 2)
            self.assertLess(attempt_times[1] - attempt_times[0], 0.5)
            log_text = "\n".join(item["text"] for item in service.get()["logs"])
            self.assertNotIn("HTTP 429", log_text)
            self.assertNotIn("自动恢复", log_text)

    def test_http_429_continues_immediately_without_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            attempt_times: list[float] = []

            def worker(_index: int, _stop_event: threading.Event, _generation: int) -> dict:
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
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=4))

            self.assertEqual(len(attempt_times), 2)
            self.assertLess(attempt_times[1] - attempt_times[0], 0.5)
            log_text = "\n".join(item["text"] for item in service.get()["logs"])
            self.assertNotIn("冷却", log_text)
            self.assertIsNone(service.get()["stats"].get("retry_at"))

    def test_manual_stop_still_interrupts_continuous_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            attempted = threading.Event()

            def fail(_index: int, _stop_event: threading.Event, _generation: int) -> dict:
                attempted.set()
                return {"ok": False, "error": "HTTP 429 Too Many Requests"}

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                register_service_module.openai_register,
                "worker",
                side_effect=fail,
            ):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                    }
                )
                self.assertTrue(attempted.wait(1))
                service.stop()
                self.assertTrue(self.wait_until(lambda: not service._runner or not service._runner.is_alive(), timeout=3))

            self.assertFalse(service.get()["enabled"])
            self.assertIsNone(service.get()["stats"].get("retry_at"))

    def test_scheduler_exception_is_supervised_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = register_service_module.RegisterService(Path(temp_dir) / "register.json")
            original_run_loop = service._run_loop
            calls = 0
            started_at = time.monotonic()

            def flaky_run_loop(generation: int, stop_event: threading.Event) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary scheduler failure")
                original_run_loop(generation, stop_event)

            with mock.patch.object(service, "_pool_metrics", return_value={"current_quota": 0, "current_available": 0}), mock.patch.object(
                service, "_run_loop", side_effect=flaky_run_loop
            ), mock.patch.object(register_service_module.openai_register, "worker", return_value={"ok": True}):
                service.start(
                    {
                        "threads": 1,
                        "total": 1,
                        "mode": "total",
                    }
                )
                self.assertTrue(self.wait_until(lambda: not service.get()["enabled"], timeout=3))

            stats = service.get()["stats"]
            self.assertEqual(calls, 2)
            self.assertEqual(stats["scheduler_restarts"], 1)
            self.assertEqual(stats["success"], 1)
            self.assertLess(time.monotonic() - started_at, 3)
            self.assertIsNone(stats.get("retry_at"))


if __name__ == "__main__":
    unittest.main()
