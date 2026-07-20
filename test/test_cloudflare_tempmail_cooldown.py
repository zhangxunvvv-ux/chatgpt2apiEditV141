import unittest
from unittest import mock

from services.register import mail_provider


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)

    def close(self):
        pass


class CloudflareTempMailNoCooldownTests(unittest.TestCase):
    def tearDown(self):
        mail_provider.provider_log_sink = None
        with mail_provider.fixed_mailbox_lock:
            mail_provider.fixed_mailbox_reservations.clear()

    @staticmethod
    def provider(session: FakeSession):
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            return mail_provider.CloudflareTempMailProvider(entry, conf)

    def test_429_fails_current_mailbox_without_sleep_or_retry(self):
        session = FakeSession(
            [
                FakeResponse(429, text="rate limited"),
                FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"}),
            ]
        )
        provider = self.provider(session)
        logs = []
        mail_provider.provider_log_sink = logs.append
        with mock.patch.object(mail_provider.time, "sleep") as sleep, self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            provider.create_mailbox("user")

        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_called()
        self.assertFalse(logs)

    def test_non_429_error_is_not_retried(self):
        session = FakeSession([FakeResponse(403, text="forbidden")])
        provider = self.provider(session)

        with mock.patch.object(mail_provider.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                provider.create_mailbox("user")

        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_called()

    def test_blank_subdomain_uses_random_domain_by_default(self):
        session = FakeSession([FakeResponse(200, {"address": "user@one.two.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain": [],
            "random_subdomain_depth": 2,
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with (
            mock.patch.object(mail_provider, "_create_session", return_value=session),
            mock.patch.object(mail_provider, "_random_subdomain_label", side_effect=["one", "two"]),
        ):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "one.two.example.test")

    def test_custom_multilevel_subdomain_is_appended_to_root_domain(self):
        session = FakeSession([FakeResponse(200, {"address": "user@team.mail.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain": ["team.mail"],
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "team.mail.example.test")

    def test_manual_levels_are_composed_from_root_outward(self):
        session = FakeSession([FakeResponse(200, {"address": "user@grtwrwe.sfsfe.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain_levels": ["sfsfe", "grtwrwe"],
            "append_random_suffix": False,
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "grtwrwe.sfsfe.example.test")

    def test_manual_levels_append_distinct_random_suffixes_by_default(self):
        session = FakeSession([FakeResponse(200, {"address": "user@grtwrwed3e4f.sfsfea1b2c.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain_levels": ["sfsfe", "grtwrwe"],
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with (
            mock.patch.object(mail_provider, "_create_session", return_value=session),
            mock.patch.object(mail_provider, "_random_subdomain_suffix", side_effect=["a1b2c", "d3e4f"]),
        ):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "grtwrwed3e4f.sfsfea1b2c.example.test")

    def test_random_subdomain_suffix_has_five_mixed_characters(self):
        for _ in range(25):
            suffix = mail_provider._random_subdomain_suffix()
            self.assertRegex(suffix, r"^(?=.*[a-z])(?=.*\d)[a-z0-9]{5}$")

    def test_manual_level_rejects_dot_separated_value(self):
        session = FakeSession([])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain_levels": ["sfsfe.grtwrwe"],
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            with self.assertRaisesRegex(RuntimeError, "每一级只能填写一个标签"):
                provider.create_mailbox("user")

    def test_full_custom_domain_is_not_duplicated(self):
        session = FakeSession([FakeResponse(200, {"address": "user@team.mail.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "subdomain": ["team.mail.example.test"],
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            provider.create_mailbox("user")

        self.assertEqual(session.calls[0]["json"]["domain"], "team.mail.example.test")

    def test_fixed_address_reuses_existing_mailbox(self):
        session = FakeSession([FakeResponse(200, {"address": "exact@team.example.test", "jwt": "mail-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "fixed_address": "exact@team.example.test",
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            mailbox = provider.create_mailbox()

        self.assertTrue(session.calls[0]["url"].endswith("/admin/get_address"))
        self.assertEqual(session.calls[0]["json"], {"address": "exact@team.example.test"})
        self.assertEqual(mailbox["address"], "exact@team.example.test")
        self.assertTrue(mailbox["fixed_address"])
        mail_provider.mark_mailbox_result(mailbox, success=False, error="test complete")
        self.assertFalse(mail_provider.fixed_mailbox_reservations)

    def test_fixed_address_is_created_exactly_when_missing(self):
        session = FakeSession([
            FakeResponse(404, text="not found"),
            FakeResponse(200, {"address": "exact@team.example.test", "jwt": "mail-token"}),
        ])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "fixed_address": "exact@team.example.test",
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            mailbox = provider.create_mailbox()

        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[1]["url"].endswith("/admin/new_address"))
        self.assertEqual(session.calls[1]["json"], {
            "enablePrefix": False,
            "name": "exact",
            "domain": "team.example.test",
        })
        self.assertEqual(mailbox["address"], "exact@team.example.test")
        mail_provider.release_mailbox(mailbox)

    def test_fixed_address_rejects_concurrent_registration_until_release(self):
        first_session = FakeSession([FakeResponse(200, {"address": "exact@example.test", "jwt": "first-token"})])
        second_session = FakeSession([FakeResponse(200, {"address": "exact@example.test", "jwt": "second-token"})])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "fixed_address": "exact@example.test",
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", side_effect=[first_session, second_session]):
            first = mail_provider.CloudflareTempMailProvider(entry, conf)
            second = mail_provider.CloudflareTempMailProvider(entry, conf)
            mailbox = first.create_mailbox()
            with self.assertRaisesRegex(RuntimeError, "正在被其他注册任务使用"):
                second.create_mailbox()
            mail_provider.release_mailbox(mailbox)
            second_mailbox = second.create_mailbox()

        self.assertEqual(second_mailbox["token"], "second-token")
        mail_provider.release_mailbox(second_mailbox)

    def test_fixed_address_validation_happens_before_api_request(self):
        session = FakeSession([])
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "fixed_address": "not-an-email",
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)
            with self.assertRaisesRegex(RuntimeError, "格式无效"):
                provider.create_mailbox()

        self.assertFalse(session.calls)

    def test_wait_for_code_scans_all_list_messages_without_detail_requests(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "results": [
                            {"id": "notice", "subject": "Welcome", "text": "No verification code here"},
                            {"id": "otp", "subject": "OpenAI verification", "text": "Verification code: 432198"},
                        ]
                    },
                ),
            ]
        )
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}

        code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "432198")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0]["url"].endswith("/api/mails"))

    def test_pre_send_baseline_does_not_consume_just_delivered_code(self):
        session = FakeSession(
            [FakeResponse(200, {"results": [{"id": "otp", "subject": "Verification code: 846210"}]})]
        )
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}

        provider.prepare_code_baseline(mailbox)
        code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "846210")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(mailbox.get("_rejected_verification_codes"), [])

    def test_list_message_is_rechecked_when_body_arrives_later(self):
        session = FakeSession(
            [
                FakeResponse(200, {"results": [{"id": "same", "subject": "OpenAI", "text": ""}]}),
                FakeResponse(200, {"results": [{"id": "same", "subject": "OpenAI", "text": "Your code is 654321"}]}),
            ]
        )
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}

        with mock.patch.object(mail_provider.time, "sleep", return_value=None):
            code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "654321")
        self.assertEqual(len(session.calls), 2)

    def test_observed_cloudflare_payload_shape_parses_raw_mime_code(self):
        raw_mime = (
            "From: ChatGPT <noreply@example.test>\r\n"
            "To: user@example.test\r\n"
            "Subject: Your temporary verification code\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Your ChatGPT verification code is 482731.\r\n"
        )
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "results": [
                            {
                                "id": 1817,
                                "message_id": "message-id",
                                "source": "sender@example.test",
                                "address": "user@example.test",
                                "raw": raw_mime,
                                "metadata": None,
                                "created_at": "2026-07-18 16:48:17",
                            }
                        ],
                        "count": 1,
                    },
                )
            ]
        )
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}

        self.assertEqual(provider.wait_for_code(mailbox), "482731")
        self.assertEqual(provider._last_response_shape, "results:list")
        self.assertEqual(provider._last_raw_batch, 1)
        self.assertEqual(provider._last_matched_batch, 1)

    def test_nested_message_list_is_supported(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "data": {
                            "messages": [
                                {
                                    "id": "otp",
                                    "address": "user@example.test",
                                    "subject": "Verification code: 739152",
                                }
                            ]
                        }
                    },
                )
            ]
        )
        provider = self.provider(session)

        code = provider.wait_for_code({"address": "user@example.test", "token": "mail-token"})

        self.assertEqual(code, "739152")
        self.assertEqual(provider._last_response_shape, "data.messages:list")

    def test_empty_inbox_diagnostic_distinguishes_delivery_from_recognition(self):
        session = FakeSession([FakeResponse(200, {"results": [], "count": 0})])
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}
        logs: list[str] = []
        mail_provider.provider_log_sink = logs.append
        try:
            with (
                mock.patch.object(mail_provider.time, "sleep", return_value=None),
                mock.patch.object(mail_provider.time, "monotonic", side_effect=[0.0, 0.0, 31.0]),
            ):
                self.assertIsNone(provider.wait_for_code(mailbox))
        finally:
            mail_provider.provider_log_sink = None

        summary = "\n".join(logs)
        self.assertIn("response_shape=results:list", summary)
        self.assertIn("last_raw_batch=0", summary)
        self.assertIn("last_matched_batch=0", summary)
        self.assertIn("conclusion=upstream_delivery_not_observed", summary)
        self.assertNotIn("mail-token", summary)

    def test_account_creation_failed_does_not_add_provider_cooldown_state(self):
        session = FakeSession([FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"})])
        provider = self.provider(session)
        mailbox = provider.create_mailbox("user")
        logs = []
        mail_provider.provider_log_sink = logs.append

        mail_provider.mark_mailbox_result(
            mailbox,
            success=False,
            error=RuntimeError(
                'user_register_http_400, detail={"error":{"code":"account_creation_failed"}}'
            ),
        )

        self.assertNotIn("_rate_limit_cooldown_key", mailbox)
        self.assertFalse(logs)

    def test_other_registration_errors_do_not_add_provider_cooldown_state(self):
        session = FakeSession([FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"})])
        provider = self.provider(session)
        mailbox = provider.create_mailbox("user")

        mail_provider.mark_mailbox_result(
            mailbox,
            success=False,
            error=RuntimeError("user_register_http_400, code=invalid_auth_step"),
        )

        self.assertNotIn("_rate_limit_cooldown_key", mailbox)


if __name__ == "__main__":
    unittest.main()
