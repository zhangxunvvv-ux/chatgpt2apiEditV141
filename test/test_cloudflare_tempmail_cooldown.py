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


class CloudflareTempMailCooldownTests(unittest.TestCase):
    def setUp(self):
        with mail_provider._cloudflare_cooldown_lock:
            mail_provider._cloudflare_cooldowns.clear()

    def tearDown(self):
        mail_provider.provider_log_sink = None
        with mail_provider._cloudflare_cooldown_lock:
            mail_provider._cloudflare_cooldowns.clear()

    @staticmethod
    def provider(session: FakeSession):
        entry = {
            "provider_ref": "cloudflare-test",
            "api_base": "https://mail.example.test",
            "admin_password": "secret",
            "domain": ["example.test"],
            "rate_limit_cooldown_seconds": 600,
        }
        conf = {"request_timeout": 30, "wait_timeout": 30, "wait_interval": 2, "user_agent": "test", "proxy": ""}
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            return mail_provider.CloudflareTempMailProvider(entry, conf)

    def test_429_pauses_for_600_seconds_then_retries(self):
        session = FakeSession(
            [
                FakeResponse(429, text="rate limited"),
                FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"}),
            ]
        )
        provider = self.provider(session)
        clock = [100.0]
        sleeps = []
        logs = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        mail_provider.provider_log_sink = logs.append
        with mock.patch.object(mail_provider.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(
            mail_provider.time,
            "sleep",
            side_effect=fake_sleep,
        ):
            mailbox = provider.create_mailbox("user")

        self.assertEqual(mailbox["address"], "user@example.test")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [600.0])
        self.assertTrue(any("暂停 600 秒" in item for item in logs))
        self.assertTrue(any("冷却结束" in item for item in logs))

    def test_non_429_error_is_not_retried(self):
        session = FakeSession([FakeResponse(403, text="forbidden")])
        provider = self.provider(session)

        with mock.patch.object(mail_provider.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                provider.create_mailbox("user")

        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_called()

    def test_wait_for_code_scans_all_mail_details(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "results": [
                            {"id": "notice", "subject": "Welcome"},
                            {"id": "otp", "subject": "OpenAI verification"},
                        ]
                    },
                ),
                FakeResponse(200, {"id": "notice", "text": "No verification code here"}),
                FakeResponse(200, {"id": "otp", "text": "Verification code: 432198"}),
            ]
        )
        provider = self.provider(session)
        mailbox = {"address": "user@example.test", "token": "mail-token"}

        code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "432198")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(session.calls[0]["url"].endswith("/api/mails"))
        self.assertTrue(session.calls[1]["url"].endswith("/api/mails/notice"))
        self.assertTrue(session.calls[2]["url"].endswith("/api/mails/otp"))

    def test_account_creation_failed_activates_the_same_600_second_cooldown(self):
        session = FakeSession([FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"})])
        provider = self.provider(session)
        mailbox = provider.create_mailbox("user")
        logs = []
        mail_provider.provider_log_sink = logs.append

        with mock.patch.object(mail_provider.time, "monotonic", return_value=100.0):
            mail_provider.mark_mailbox_result(
                mailbox,
                success=False,
                error=RuntimeError(
                    'user_register_http_400, detail={"error":{"code":"account_creation_failed"}}'
                ),
            )

        cooldown = mail_provider._cloudflare_cooldowns[mailbox["_rate_limit_cooldown_key"]]
        self.assertEqual(cooldown, (700.0, False))
        self.assertTrue(any("account_creation_failed" in item and "600 秒" in item for item in logs))

    def test_other_registration_errors_do_not_activate_email_cooldown(self):
        session = FakeSession([FakeResponse(200, {"address": "user@example.test", "jwt": "mail-token"})])
        provider = self.provider(session)
        mailbox = provider.create_mailbox("user")

        mail_provider.mark_mailbox_result(
            mailbox,
            success=False,
            error=RuntimeError("user_register_http_400, code=invalid_auth_step"),
        )

        self.assertNotIn(mailbox["_rate_limit_cooldown_key"], mail_provider._cloudflare_cooldowns)


if __name__ == "__main__":
    unittest.main()
