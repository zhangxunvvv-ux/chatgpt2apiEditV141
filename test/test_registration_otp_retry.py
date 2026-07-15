from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from services.register import mail_provider, openai_register


def message(message_id: str, code: str) -> dict:
    return {
        "provider": "fake",
        "mailbox": "user@example.com",
        "message_id": message_id,
        "subject": f"Verification code: {code}",
        "text_content": "",
        "html_content": "",
        "received_at": datetime.now(timezone.utc),
    }


class FakeProvider(mail_provider.BaseMailProvider):
    name = "fake"

    def __init__(self, current: dict | None = None) -> None:
        super().__init__({"wait_timeout": 0.1, "wait_interval": 0.01})
        self.current = current

    def fetch_latest_message(self, _mailbox: dict) -> dict | None:
        return self.current


class FakeResponse:
    def __init__(self, status_code: int, code: str = "") -> None:
        self.status_code = status_code
        self._code = code
        self.text = "" if status_code == 200 else f'{{"error":{{"code":"{code}"}}}}'

    def json(self) -> dict:
        return {} if self.status_code == 200 else {"error": {"code": self._code}}


class RegistrationOtpRetryTests(unittest.TestCase):
    def test_send_baseline_excludes_preexisting_message(self) -> None:
        old = message("old", "111111")
        provider = FakeProvider(old)
        mailbox = {"address": "user@example.com"}

        provider.prepare_code_baseline(mailbox)
        provider.current = message("new", "222222")

        self.assertEqual(provider.wait_for_code(mailbox), "222222")
        self.assertIn(mail_provider._message_tracking_ref(old), mailbox["_seen_code_message_refs"])
        self.assertIn("111111", mailbox["_rejected_verification_codes"])

    def test_wrong_code_is_rejected_and_newer_code_is_retried(self) -> None:
        registrar = openai_register.PlatformRegistrar("")
        mailbox = {"address": "user@example.com"}
        wrong = FakeResponse(401, "wrong_email_otp_code")
        accepted = FakeResponse(200)

        with (
            mock.patch.object(openai_register, "wait_for_code", side_effect=["111111", "222222"]),
            mock.patch.object(openai_register, "validate_otp", side_effect=[(wrong, ""), (accepted, "")]) as validate,
            mock.patch.object(openai_register.time, "sleep", return_value=None),
        ):
            registrar._validate_mailbox_otp(mailbox, 1)

        registrar.close()
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(validate.call_args_list[0].args[2], "111111")
        self.assertEqual(validate.call_args_list[1].args[2], "222222")
        self.assertEqual(mailbox["_rejected_verification_codes"], ["111111"])

    def test_validate_does_not_resubmit_same_known_wrong_code_with_sentinel(self) -> None:
        wrong = FakeResponse(401, "wrong_email_otp_code")

        class Session:
            def __init__(self) -> None:
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                return wrong

        session = Session()
        response, _error = openai_register.validate_otp(session, "device", "111111")

        self.assertIs(response, wrong)
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
