from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
