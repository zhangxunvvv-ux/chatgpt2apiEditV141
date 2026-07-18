from __future__ import annotations

import unittest
from unittest import mock

from services.register import openai_register


class ChatGPTSessionTokenRegistrationTests(unittest.TestCase):
    def test_platform_oauth_exchange_uses_form_encoding_for_refresh_token_flow(self) -> None:
        class Response:
            status_code = 200
            text = '{"access_token":"platform","refresh_token":"refresh","id_token":"id"}'

            @staticmethod
            def json():
                return {"access_token": "platform", "refresh_token": "refresh", "id_token": "id"}

        class Session:
            def __init__(self) -> None:
                self.kwargs = {}

            def post(self, _url, **kwargs):
                self.kwargs = kwargs
                return Response()

        session = Session()
        result = openai_register.request_platform_oauth_token(session, "auth-code", "verifier")

        self.assertEqual(result["refresh_token"], "refresh")
        self.assertEqual(session.kwargs["data"]["grant_type"], "authorization_code")
        self.assertNotIn("json", session.kwargs)
        self.assertEqual(
            session.kwargs["headers"]["content-type"],
            "application/x-www-form-urlencoded",
        )

    def test_registration_keeps_chatgpt_and_platform_tokens_separate(self) -> None:
        registrar = openai_register.PlatformRegistrar("")
        registrar._chatgpt_authorize = mock.Mock()
        registrar._register_user = mock.Mock()
        registrar._send_otp = mock.Mock()
        registrar._validate_mailbox_otp = mock.Mock()
        registrar._create_account = mock.Mock()
        registrar._finish_chatgpt_registration = mock.Mock(return_value={
            "access_token": "chatgpt-session-access",
            "session_token": "chatgpt-session-id",
            "cookie": "next-auth=session",
        })
        registrar._platform_authorize = mock.Mock()
        registrar._exchange_registered_tokens = mock.Mock(return_value={
            "access_token": "platform-access",
            "refresh_token": "platform-refresh",
            "id_token": "platform-id",
        })

        with (
            mock.patch.object(openai_register, "create_mailbox", return_value={"address": "user@example.com"}),
            mock.patch.object(openai_register.mail_provider, "mark_mailbox_result"),
        ):
            result = registrar.register(1)

        registrar.close()
        self.assertEqual(result["access_token"], "chatgpt-session-access")
        self.assertEqual(result["platform_access_token"], "platform-access")
        self.assertEqual(result["refresh_token"], "platform-refresh")
        self.assertEqual(result["session_token"], "chatgpt-session-id")
        self.assertEqual(result["cookie"], "next-auth=session")
        registrar._platform_authorize.assert_called_once_with("user@example.com", 1, screen_hint="login")

    def test_registration_keeps_chatgpt_session_when_platform_oauth_fails(self) -> None:
        registrar = openai_register.PlatformRegistrar("")
        registrar._chatgpt_authorize = mock.Mock()
        registrar._register_user = mock.Mock()
        registrar._send_otp = mock.Mock()
        registrar._validate_mailbox_otp = mock.Mock()
        registrar._create_account = mock.Mock()
        registrar._finish_chatgpt_registration = mock.Mock(return_value={
            "access_token": "chatgpt-session-access",
            "session_token": "chatgpt-session-id",
            "cookie": "next-auth=session",
        })
        registrar._platform_authorize = mock.Mock(side_effect=RuntimeError("platform_authorize_missing_code"))
        registrar._exchange_registered_tokens = mock.Mock()

        with (
            mock.patch.object(openai_register, "create_mailbox", return_value={"address": "user@example.com"}),
            mock.patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_result,
        ):
            result = registrar.register(1)

        registrar.close()
        self.assertEqual(result["access_token"], "chatgpt-session-access")
        self.assertEqual(result["platform_access_token"], "")
        self.assertEqual(result["refresh_token"], "")
        self.assertEqual(result["session_token"], "chatgpt-session-id")
        registrar._exchange_registered_tokens.assert_not_called()
        mark_result.assert_called_once_with({"address": "user@example.com"}, success=True)


if __name__ == "__main__":
    unittest.main()
