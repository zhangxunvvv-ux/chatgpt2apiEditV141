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

    def test_parses_multiple_keys(self) -> None:
        self.assertEqual(mail_provider._parse_tempmail_keys(" key-a, key-b\nkey-a "), ["key-a", "key-b"])
        self.assertEqual(mail_provider._parse_tempmail_keys(""), [""])

    def test_create_rotates_key_after_429_without_domain_filter(self) -> None:
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
                "rate_limit_cooldown_seconds": 600,
                "max_wait": 0,
                "create_total_budget": 15,
            },
            session,
        )
        clock = [100.0]
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
                mailbox = provider.create_mailbox()
        finally:
            mail_provider.provider_log_sink = None

        self.assertEqual(mailbox["token"], "token-429")
        self.assertEqual(sleeps, [600.0])
        self.assertEqual(sum("触发 HTTP 429" in item for item in logs), 1)
        self.assertEqual(sum("冷却结束" in item for item in logs), 1)
        self.assertNotIn("key-one", "\n".join(logs))
        self.assertNotIn("token-429", "\n".join(logs))
        self.assertEqual([request["headers"] for request in session.requests], [
            {"Authorization": "Bearer key-one"},
            {"Authorization": "Bearer key-two"},
        ])
        second_payload = session.requests[1]["json"]
        self.assertIsInstance(second_payload, dict)
        assert isinstance(second_payload, dict)
        self.assertNotIn("domain", second_payload)
        self.assertTrue(re.fullmatch(r"[a-z]{5}\d{1,3}[a-z]{1,3}", str(second_payload["prefix"])))

    def test_429_cooldown_is_shared_by_provider_instances(self) -> None:
        entry = {
            "provider_ref": "tempmail-shared-429",
            "api_key": "shared-key",
            "window_seconds": 10,
            "rate_limit_cooldown_seconds": 600,
        }
        first = self.make_provider(entry, FakeSession([]))
        second = self.make_provider(entry, FakeSession([]))
        clock = [50.0]
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
                first._activate_rate_limit_cooldown()
                second._activate_rate_limit_cooldown()
                waited = second.key_pool.wait_for_global_cooldown()
        finally:
            mail_provider.provider_log_sink = None

        self.assertIs(first.key_pool, second.key_pool)
        self.assertEqual(waited, 600.0)
        self.assertEqual(sleeps, [600.0])
        self.assertEqual(sum("触发 HTTP 429" in item for item in logs), 1)
        self.assertEqual(sum("冷却结束" in item for item in logs), 1)

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

    def test_configured_domain_is_ignored_and_created_address_is_accepted(self) -> None:
        session = FakeSession([FakeResponse(201, {"address": "mail@random-provider.example", "token": "token"})])
        provider = self.make_provider(
            {
                "provider_ref": "tempmail-test-domain",
                "api_key": "key",
                "domain": ["not-a-domain"],
            },
            session,
        )

        mailbox = provider.create_mailbox()

        self.assertEqual(mailbox["address"], "mail@random-provider.example")
        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("domain", session.requests[0]["json"])

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

    def test_pre_send_baseline_does_not_consume_just_delivered_code(self) -> None:
        entry = {"provider_ref": "tempmail-baseline", "api_key": "key", "domain": []}
        session = FakeSession(
            [FakeResponse(200, {"emails": [{"id": "new-message", "subject": "Verification code: 846210"}]})]
        )
        provider = self.make_provider(entry, session)
        mailbox = {"address": "mail@example.com", "token": "token-baseline"}

        provider.prepare_code_baseline(mailbox)
        code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "846210")
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(mailbox.get("_rejected_verification_codes"), [])

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

    def test_domain_history_never_skips_created_address(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            mail_provider, "TEMPMAIL_DOMAIN_STATS_FILE", Path(temp_dir) / "domain-stats.json"
        ):
            for _ in range(3):
                mail_provider._record_tempmail_domain_result(
                    "abc.airfryersbg.com",
                    received=False,
                )
            session = FakeSession(
                [
                    FakeResponse(201, {"address": "direct@next.airfryersbg.com", "token": "accepted-token"}),
                ]
            )
            provider = self.make_provider(
                {
                    "provider_ref": "tempmail-domain-cooldown",
                    "api_key": "key",
                    "domain": ["whitelist.example"],
                    "max_wait": 0,
                    "domain_cooldown_threshold": 3,
                    "domain_cooldown_seconds": 600,
                },
                session,
            )

            mailbox = provider.create_mailbox()
            stats = {item["domain"]: item for item in mail_provider.tempmail_domain_stats_snapshot()}

        self.assertEqual(mailbox["address"], "direct@next.airfryersbg.com")
        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("domain", session.requests[0]["json"])
        self.assertEqual(stats["airfryersbg.com"]["consecutive_timeouts"], 3)
        self.assertNotIn("cooling", stats["airfryersbg.com"])

    def test_delivery_result_is_recorded_only_once_per_mailbox(self) -> None:
        mailbox = {
            "provider": "tempmail_lol",
            "address": "mail@tal.gardianwaves.org",
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
