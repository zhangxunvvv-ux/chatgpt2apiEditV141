from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.register import mail_provider


class FakeResponse:
    def __init__(self, status_code: int, payload: object, text: str = ""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class TempMailLolProviderTests(unittest.TestCase):
    conf = {
        "request_timeout": 30.0,
        "wait_timeout": 2.0,
        "wait_interval": 0.01,
        "user_agent": "test-agent",
        "proxy": "",
    }

    def make_provider(self, entry: dict[str, object], session: FakeSession) -> mail_provider.TempMailLolProvider:
        with mock.patch.object(mail_provider, "_create_session", return_value=session):
            return mail_provider.TempMailLolProvider(entry, dict(self.conf))

    def test_parses_multiple_keys_and_domains(self) -> None:
        self.assertEqual(mail_provider._parse_tempmail_keys(" key-a, key-b\nkey-a "), ["key-a", "key-b"])
        self.assertEqual(mail_provider._parse_tempmail_keys(""), [""])
        self.assertEqual(
            mail_provider._parse_tempmail_domains(["https://Mail.Example.com/path", "*.alt.example.com"]),
            ["mail.example.com", "*.alt.example.com"],
        )

    def test_create_rotates_key_after_429_and_randomizes_wildcard_domain(self) -> None:
        session = FakeSession(
            [
                FakeResponse(429, {"error": "rate limited"}, "rate limited"),
                FakeResponse(201, {"address": "user@abc12.example.com", "token": "token-429"}),
            ]
        )
        provider = self.make_provider(
            {
                "provider_ref": "tempmail-test-429",
                "api_key": "key-one\nkey-two",
                "domain": ["*.example.com"],
                "rate_per_window": 24,
                "window_seconds": 300,
                "max_wait": 0,
                "create_total_budget": 15,
            },
            session,
        )

        mailbox = provider.create_mailbox()

        self.assertEqual(mailbox["token"], "token-429")
        self.assertEqual([request["headers"] for request in session.requests], [
            {"Authorization": "Bearer key-one"},
            {"Authorization": "Bearer key-two"},
        ])
        second_payload = session.requests[1]["json"]
        self.assertIsInstance(second_payload, dict)
        assert isinstance(second_payload, dict)
        self.assertRegex(str(second_payload["domain"]), r"^[a-z][a-z0-9]{4}\.example\.com$")
        self.assertTrue(re.fullmatch(r"[a-z0-9]{12}", str(second_payload["prefix"])))

    def test_create_fails_fast_for_fatal_4xx(self) -> None:
        session = FakeSession([FakeResponse(403, {"error": "forbidden"}, "forbidden")])
        provider = self.make_provider(
            {
                "provider_ref": "tempmail-test-fatal",
                "api_key": "bad-key\nunused-key",
                "domain": [],
                "max_wait": 0,
            },
            session,
        )

        with self.assertRaisesRegex(RuntimeError, r"创建失败 \(HTTP 403\)"):
            provider.create_mailbox()
        self.assertEqual(len(session.requests), 1)

    def test_create_rotates_key_after_transient_network_error(self) -> None:
        session = FakeSession(
            [
                OSError("temporary disconnect"),
                FakeResponse(201, {"address": "mail@example.com", "token": "token-network"}),
            ]
        )
        provider = self.make_provider(
            {
                "provider_ref": "tempmail-test-network",
                "api_key": "key-one,key-two",
                "domain": [],
                "max_wait": 0,
            },
            session,
        )

        with mock.patch.object(mail_provider.time, "sleep", return_value=None):
            mailbox = provider.create_mailbox()

        self.assertEqual(mailbox["token"], "token-network")
        self.assertEqual(
            [request["headers"] for request in session.requests],
            [{"Authorization": "Bearer key-one"}, {"Authorization": "Bearer key-two"}],
        )

    def test_invalid_domain_is_rejected_before_request(self) -> None:
        session = FakeSession([])
        provider = self.make_provider(
            {
                "provider_ref": "tempmail-test-domain",
                "api_key": "key",
                "domain": ["not-a-domain"],
            },
            session,
        )

        with self.assertRaisesRegex(RuntimeError, "自定义域名无效"):
            provider.create_mailbox()
        self.assertEqual(session.requests, [])

    def test_key_pool_enforces_sliding_window_limit(self) -> None:
        pool = mail_provider._TempMailKeyPool(["only-key"], rate=1, window=300)

        self.assertEqual(pool.acquire(0), "only-key")
        with self.assertRaisesRegex(RuntimeError, "所有 API Key 均已达到限速"):
            pool.acquire(0)

    def test_poll_uses_creation_key_then_switches_after_three_errors(self) -> None:
        entry = {
            "provider_ref": "tempmail-test-poll",
            "api_key": "primary-key\nfallback-key",
            "domain": [],
            "max_wait": 0,
        }
        create_session = FakeSession([FakeResponse(201, {"address": "mail@example.com", "token": "token-poll"})])
        creator = self.make_provider(entry, create_session)
        mailbox = creator.create_mailbox()
        mailbox["_code_not_before"] = datetime.now(timezone.utc)

        poll_session = FakeSession(
            [
                FakeResponse(500, {}, "temporary"),
                FakeResponse(503, {}, "temporary"),
                FakeResponse(520, {}, "temporary"),
                FakeResponse(
                    200,
                    {
                        "emails": [
                            {"id": "newest", "subject": "Status update", "body": "No code here"},
                            {"id": "code", "subject": "Your verification code is 432198", "body": "Use it now"},
                        ]
                    },
                ),
            ]
        )
        poller = self.make_provider(entry, poll_session)

        with mock.patch.object(mail_provider.time, "sleep", return_value=None):
            code = poller.wait_for_code(mailbox)

        self.assertEqual(code, "432198")
        self.assertEqual(
            [request["headers"] for request in poll_session.requests],
            [
                {"Authorization": "Bearer primary-key"},
                {"Authorization": "Bearer primary-key"},
                {"Authorization": "Bearer primary-key"},
                {"Authorization": "Bearer fallback-key"},
            ],
        )

    def test_poll_rechecks_message_when_body_arrives_later(self) -> None:
        entry = {"provider_ref": "tempmail-recheck", "api_key": "key", "domain": []}
        session = FakeSession(
            [
                FakeResponse(200, {"emails": [{"id": "same-message", "subject": "OpenAI", "body": ""}]}),
                FakeResponse(
                    200,
                    {"emails": [{"id": "same-message", "subject": "OpenAI", "body": "Your ChatGPT code is 654321"}]},
                ),
            ]
        )
        provider = self.make_provider(entry, session)
        mailbox = {"address": "mail@example.com", "token": "token-recheck"}

        with mock.patch.object(mail_provider.time, "sleep", return_value=None):
            code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "654321")
        self.assertEqual(len(session.requests), 2)

    def test_poll_accepts_unseen_message_id_despite_clock_skew(self) -> None:
        entry = {"provider_ref": "tempmail-clock-skew", "api_key": "key", "domain": []}
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "emails": [
                            {
                                "id": "new-message",
                                "subject": "Verification code: 321654",
                                "created_at": "2020-01-01T00:00:00Z",
                            }
                        ]
                    },
                )
            ]
        )
        provider = self.make_provider(entry, session)
        mailbox = {
            "address": "mail@example.com",
            "token": "token-clock-skew",
            "_code_not_before": datetime.now(timezone.utc),
        }

        self.assertEqual(provider.wait_for_code(mailbox), "321654")

    def test_poll_429_pauses_key_until_window_reset_without_leaking_secrets(self) -> None:
        entry = {
            "provider_ref": "tempmail-poll-429",
            "api_key": "api-secret",
            "domain": [],
            "window_seconds": 300,
        }
        session = FakeSession([FakeResponse(429, {"error": "rate limited"}, "rate limited")])
        provider = self.make_provider(entry, session)
        mailbox = {"address": "mail@example.com", "token": "token-secret"}
        clock = [0.0]
        sleeps: list[float] = []
        logs: list[str] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        mail_provider.provider_log_sink = logs.append
        try:
            with mock.patch.object(mail_provider.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(
                mail_provider.time, "sleep", side_effect=fake_sleep
            ):
                self.assertIsNone(provider.wait_for_code(mailbox))
        finally:
            mail_provider.provider_log_sink = None

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(sleeps, [2.0])
        summary = "\n".join(logs)
        self.assertIn("http_429=1", summary)
        self.assertIn("cooldown_pauses=1", summary)
        self.assertNotIn("api-secret", summary)
        self.assertNotIn("token-secret", summary)

    def test_cooled_domain_is_skipped_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            mail_provider, "TEMPMAIL_DOMAIN_STATS_FILE", Path(temp_dir) / "domain-stats.json"
        ):
            for _ in range(3):
                mail_provider._record_tempmail_domain_result(
                    "abc.airfryersbg.com",
                    received=False,
                    cooldown_threshold=3,
                    cooldown_seconds=600,
                )
            session = FakeSession(
                [
                    FakeResponse(201, {"address": "bad@next.airfryersbg.com", "token": "discarded-token"}),
                    FakeResponse(201, {"address": "good@tal.gardianwaves.org", "token": "accepted-token"}),
                ]
            )
            provider = self.make_provider(
                {
                    "provider_ref": "tempmail-domain-cooldown",
                    "api_key": "key",
                    "domain": [],
                    "max_wait": 0,
                    "domain_cooldown_threshold": 3,
                    "domain_cooldown_seconds": 600,
                },
                session,
            )

            mailbox = provider.create_mailbox()
            stats = {item["domain"]: item for item in mail_provider.tempmail_domain_stats_snapshot()}

        self.assertEqual(mailbox["address"], "good@tal.gardianwaves.org")
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(stats["airfryersbg.com"]["cooling"])
        self.assertEqual(stats["airfryersbg.com"]["skipped"], 1)

    def test_delivery_result_is_recorded_only_once_per_mailbox(self) -> None:
        mailbox = {
            "provider": "tempmail_lol",
            "address": "mail@tal.gardianwaves.org",
            "_domain_cooldown_threshold": 3,
            "_domain_cooldown_seconds": 600,
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            mail_provider, "TEMPMAIL_DOMAIN_STATS_FILE", Path(temp_dir) / "domain-stats.json"
        ):
            mail_provider.mark_verification_code_received(mailbox)
            mail_provider.mark_verification_code_received(mailbox)
            mail_provider.mark_mailbox_result(mailbox, success=False, error="等待注册验证码超时")
            stats = {item["domain"]: item for item in mail_provider.tempmail_domain_stats_snapshot()}

        self.assertEqual(stats["gardianwaves.org"]["received"], 1)
        self.assertEqual(stats["gardianwaves.org"]["timeouts"], 0)


if __name__ == "__main__":
    unittest.main()
