from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.config import config
from services.log_service import log_service
from services.openai_backend_api import InvalidAccessTokenError
from services.openai_backend_api import OpenAIBackendAPI
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def test_single_account_refresh_does_not_create_nested_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            with (
                patch.object(service, "fetch_remote_info", return_value={"status": "正常"}) as fetch,
                patch("services.account_service.ThreadPoolExecutor") as executor,
            ):
                result = service.refresh_accounts(["test-token"])

        executor.assert_not_called()
        fetch.assert_called_once_with("test-token", "refresh_accounts", True)
        self.assertEqual(result["refreshed"], 1)

    def test_user_info_requests_stay_on_the_session_owner_thread(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "test-token"
        owner_thread = threading.get_ident()
        calls: list[tuple[str, int]] = []

        def record(name: str, result: dict):
            def run() -> dict:
                calls.append((name, threading.get_ident()))
                return result
            return run

        backend._get_me = record("me", {"email": "pool@example.com", "id": "user-1"})
        backend._get_conversation_init = record("init", {"limits_progress": [], "default_model_slug": "auto"})
        backend._get_default_account = record("account", {"plan_type": "free"})

        result = backend.get_user_info()

        self.assertEqual([name for name, _ in calls], ["me", "init", "account"])
        self.assertTrue(all(thread_id == owner_thread for _, thread_id in calls))
        self.assertEqual(result["email"], "pool@example.com")

    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_image_failure_records_sanitized_category_for_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "secret-access-token", "email": "pool@example.com", "status": "正常", "quota": 3}
            ])

            updated = service.mark_image_result(
                "secret-access-token",
                success=False,
                error="HTTP 429 Bearer super-secret-bearer-value",
            )

            self.assertIsNotNone(updated)
            self.assertEqual(updated["last_image_error_category"], "rate_limit")
            self.assertEqual(updated["image_error_counts"], {"rate_limit": 1})
            self.assertNotIn("super-secret-bearer-value", updated["last_image_error"])

    def test_pool_diagnostics_has_no_tokens_and_uses_bounded_recent_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "secret-access-token", "email": "pool@example.com", "status": "正常", "quota": 3}
            ])
            for _ in range(3):
                service.mark_image_result("secret-access-token", success=False, error="image poll timeout")

            recent_log = {
                "time": "2026-07-16 12:00:00",
                "type": "call",
                "summary": "文生图调用失败",
                "detail": {
                    "endpoint": "/v1/images/generations",
                    "status": "failed",
                    "account_email": "pool@example.com",
                    "model": "gpt-image-2",
                    "duration_ms": 1200,
                    "error": "image poll timeout",
                },
            }
            with patch.object(log_service, "tail", return_value=[recent_log]) as tail:
                diagnostics = service.get_pool_diagnostics(recent_limit=10)

            serialized = str(diagnostics)
            self.assertNotIn("secret-access-token", serialized)
            self.assertEqual(diagnostics["summary"]["failures"], 3)
            self.assertEqual(diagnostics["error_categories"][0]["category"], "poll_timeout")
            self.assertEqual(diagnostics["recent_events"][0]["email"], "pool@example.com")
            self.assertEqual(diagnostics["anomalies"][0]["consecutive_failures"], 3)
            self.assertEqual(diagnostics["performance"]["background_jobs_added"], 0)
            tail.assert_called_once()

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_recent_submission_is_spread_to_another_account_after_slot_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-a", "status": "正常", "quota": 3},
                {"access_token": "token-b", "status": "正常", "quota": 3},
            ])
            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            first = service.get_available_access_token()
            service.release_image_slot(first)
            second = service.get_available_access_token()
            service.release_image_slot(second)

            self.assertNotEqual(first, second)

    def test_result_after_early_release_does_not_release_another_submission(self) -> None:
        original_concurrency = config.data.get("image_account_concurrency")
        config.data["image_account_concurrency"] = 3
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
                service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

                first = service.get_available_access_token()
                service.release_image_slot(first)
                second = service.get_available_access_token()
                service.mark_image_result(first, True, release_slot=False)

                self.assertEqual(service._image_inflight.get(second), 1)
                service.release_image_slot(second)
        finally:
            if original_concurrency is None:
                config.data.pop("image_account_concurrency", None)
            else:
                config.data["image_account_concurrency"] = original_concurrency

    def test_chatgpt_session_and_platform_tokens_refresh_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{
                "access_token": "session-old",
                "platform_access_token": "platform-old",
                "refresh_token": "refresh-old",
                "cookie": "session=cookie",
                "status": "正常",
                "quota": 3,
            }])
            service._request_chatgpt_session_refresh = lambda _account: {
                "access_token": "session-new",
                "session_token": "session-id-new",
                "cookie": "session=cookie-new",
            }
            service._request_access_token_refresh = lambda _refresh, _account: {
                "access_token": "platform-new",
                "refresh_token": "refresh-new",
                "id_token": "id-new",
            }

            active = service.refresh_access_token("session-old", force=True, event="test")
            account = service.get_account(active)

            self.assertEqual(active, "session-new")
            self.assertEqual(account["platform_access_token"], "platform-new")
            self.assertEqual(account["refresh_token"], "refresh-new")
            self.assertEqual(account["session_token"], "session-id-new")
            self.assertEqual(account["cookie"], "session=cookie-new")

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
