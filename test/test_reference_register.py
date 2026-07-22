from __future__ import annotations

import unittest
from unittest import mock

from services.register import openai_register, reference_register


class ReferenceRegisterTests(unittest.TestCase):
    def test_password_flow_uses_login_hint_and_reference_otp_sender(self) -> None:
        mail_config = {
            "providers": [{
                "type": "cloudflare_temp_email",
                "enable": True,
                "domain": ["example.test"],
                "subdomain_levels": ["level-one", "level-two"],
                "append_random_suffix": True,
            }],
        }
        mailbox = {
            "address": "user@example.test",
            "label": "shared-provider",
            "provider": "test",
        }
        registrar = reference_register.ReferencePlatformRegistrar("", mail_config=mail_config)
        mail_config["providers"][0]["subdomain_levels"] = ["mutated-after-start"]
        with (
            mock.patch.object(openai_register, "create_mailbox", return_value=mailbox) as create_mailbox,
            mock.patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_result,
            mock.patch.object(registrar, "_chatgpt_authorize") as authorize,
            mock.patch.object(
                registrar,
                "_authorize_signup",
                return_value=("password", ""),
            ) as signup,
            mock.patch.object(registrar, "_register_user") as register_user,
            mock.patch.object(registrar, "_send_email_otp_reference") as send_otp,
            mock.patch.object(registrar, "_validate_mailbox_otp") as validate_otp,
            mock.patch.object(registrar, "_create_account") as create_account,
            mock.patch.object(
                registrar,
                "_finish_chatgpt_registration",
                return_value={"access_token": "chatgpt-token", "session_token": "", "cookie": ""},
            ),
        ):
            result = registrar.register(1)

        registrar.close()
        create_mailbox.assert_called_once()
        runtime_mail = create_mailbox.call_args.kwargs["mail_config"]
        self.assertEqual(runtime_mail["providers"][0]["subdomain_levels"], ["level-one", "level-two"])
        self.assertTrue(runtime_mail["providers"][0]["append_random_suffix"])
        authorize.assert_called_once_with("user@example.test", 1, include_login_hint=True)
        signup.assert_called_once_with("user@example.test", 1, screen_hint="login_or_signup")
        register_user.assert_called_once()
        send_otp.assert_called_once_with(1, mailbox)
        validate_otp.assert_called_once_with(mailbox, 1)
        create_account.assert_called_once()
        self.assertEqual(result["access_token"], "chatgpt-token")
        self.assertEqual(result["source_type"], "web")
        self.assertEqual(result["registration_engine"], "reference")
        self.assertEqual(result["registration_environment_version"], 1)
        self.assertEqual(result["fp"]["user-agent"], registrar._browser_user_agent())
        self.assertEqual(result["fp"]["oai-device-id"], registrar.device_id)
        self.assertEqual(result["fp"]["oai-session-id"], registrar.session_id)
        self.assertEqual(result["fp"]["sec-ch-ua-mobile"], registrar._profile_mobile)
        mark_result.assert_called_once_with(mailbox, success=True)

    def test_direct_otp_flow_keeps_first_code_without_immediate_resend(self) -> None:
        mailbox = {"address": "user@example.test", "provider": "test"}
        registrar = reference_register.ReferencePlatformRegistrar("")
        with (
            mock.patch.object(openai_register, "create_mailbox", return_value=mailbox),
            mock.patch.object(openai_register.mail_provider, "mark_mailbox_result"),
            mock.patch.object(openai_register.mail_provider, "prepare_code_baseline") as baseline,
            mock.patch.object(registrar, "_chatgpt_authorize"),
            mock.patch.object(
                registrar,
                "_authorize_signup",
                return_value=("otp", "passwordless_signup"),
            ),
            mock.patch.object(registrar, "_register_user") as register_user,
            mock.patch.object(registrar, "_send_email_otp_reference") as send_otp,
            mock.patch.object(registrar, "_resend_signup_otp") as resend,
            mock.patch.object(registrar, "_validate_mailbox_otp"),
            mock.patch.object(registrar, "_create_account"),
            mock.patch.object(
                registrar,
                "_finish_chatgpt_registration",
                return_value={"access_token": "chatgpt-token", "session_token": "", "cookie": ""},
            ),
        ):
            result = registrar.register(2)

        registrar.close()
        register_user.assert_not_called()
        send_otp.assert_not_called()
        resend.assert_not_called()
        baseline.assert_called_once()
        self.assertEqual(result["password"], "")

    def test_reference_otp_validation_uses_same_profile_without_device_or_sentinel_headers(self) -> None:
        registrar = reference_register.ReferencePlatformRegistrar("")
        response = mock.Mock(status_code=200)
        calls = []

        def fake_request(_session, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return response, ""

        with (
            mock.patch.object(openai_register, "request_with_local_retry", side_effect=fake_request),
            mock.patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
        ):
            actual, error = registrar._request_otp_validation("123456", 3)

        registrar.close()
        self.assertIs(actual, response)
        self.assertEqual(error, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "post")
        self.assertEqual(calls[0]["url"], "https://auth.openai.com/api/accounts/email-otp/validate")
        self.assertEqual(calls[0]["json"], {"code": "123456"})
        headers = {key.lower(): value for key, value in calls[0]["headers"].items()}
        self.assertEqual(headers["user-agent"], registrar._browser_user_agent())
        self.assertNotIn("oai-device-id", headers)
        self.assertNotIn("openai-sentinel-token", headers)
        self.assertNotIn("traceparent", headers)

    def test_passwordless_login_failed_does_not_loop_resend_in_same_broken_session(self) -> None:
        registrar = reference_register.ReferencePlatformRegistrar("")
        registrar.signup_verification_mode = "passwordless_signup"
        rejected = mock.Mock(status_code=401, text='{"error":{"code":"login_failed"}}')
        rejected.json.return_value = {"error": {"code": "login_failed"}}
        mailbox = {"address": "user@example.test"}

        with (
            mock.patch.object(openai_register, "wait_for_code", return_value="111111") as wait_for_code,
            mock.patch.object(registrar, "_request_otp_validation", return_value=(rejected, "")) as validate,
            mock.patch.object(registrar, "_resend_signup_otp") as resend,
        ):
            with self.assertRaisesRegex(RuntimeError, "validate_otp_http_401"):
                registrar._validate_mailbox_otp(mailbox, 4)

        registrar.close()
        validate.assert_called_once_with("111111", 4)
        self.assertIs(wait_for_code.call_args.kwargs["mail_config"], registrar.mail_config)
        resend.assert_not_called()

    def test_authorize_landing_on_email_verification_skips_duplicate_email_submission(self) -> None:
        mailbox = {"address": "user@example.test", "provider": "test"}
        registrar = reference_register.ReferencePlatformRegistrar("")

        def land_on_verification(*_args, **_kwargs):
            registrar.chatgpt_authorize_landed_path = "/email-verification"

        with (
            mock.patch.object(openai_register, "create_mailbox", return_value=mailbox),
            mock.patch.object(openai_register.mail_provider, "mark_mailbox_result"),
            mock.patch.object(openai_register.mail_provider, "prepare_code_baseline"),
            mock.patch.object(registrar, "_chatgpt_authorize", side_effect=land_on_verification),
            mock.patch.object(registrar, "_authorize_signup") as signup,
            mock.patch.object(registrar, "_resend_signup_otp") as resend,
            mock.patch.object(registrar, "_validate_mailbox_otp"),
            mock.patch.object(registrar, "_create_account"),
            mock.patch.object(
                registrar,
                "_finish_chatgpt_registration",
                return_value={"access_token": "chatgpt-token", "session_token": "", "cookie": ""},
            ),
        ):
            registrar.register(5)

        registrar.close()
        signup.assert_not_called()
        resend.assert_not_called()

    def test_random_profile_is_used_for_sentinel_generation(self) -> None:
        registrar = reference_register.ReferencePlatformRegistrar("")
        with mock.patch.object(openai_register, "build_sentinel_token", return_value="token") as build:
            self.assertEqual(registrar._build_sentinel_token("authorize_continue"), "token")

        registrar.close()
        call = build.call_args
        self.assertEqual(call.args[1], registrar.device_id)
        self.assertEqual(call.args[2], "authorize_continue")
        self.assertEqual(call.kwargs["user_agent_override"], registrar._browser_user_agent())
        self.assertEqual(call.kwargs["sec_ch_ua_override"], registrar._browser_sec_ch_ua())

    def test_reference_log_context_does_not_replace_main_sink(self) -> None:
        main_logs: list[str] = []
        reference_logs: list[str] = []
        previous = openai_register.register_log_sink
        openai_register.register_log_sink = lambda text, _color="": main_logs.append(text)
        try:
            with openai_register.thread_log_sink(lambda text, _color="": reference_logs.append(text)):
                openai_register.log("reference-only")
            openai_register.log("main-only")
        finally:
            openai_register.register_log_sink = previous

        self.assertEqual(reference_logs, ["reference-only"])
        self.assertEqual(main_logs, ["main-only"])


if __name__ == "__main__":
    unittest.main()
