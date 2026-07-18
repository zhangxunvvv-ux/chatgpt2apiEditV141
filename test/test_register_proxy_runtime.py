import unittest
from unittest import mock
from unittest.mock import patch

from services.proxy_service import ClearanceBundle
from services.register import openai_register


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://auth.openai.com/test", json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.json_data = json_data

    def json(self):
        return self.json_data or {}


class FakeCookieJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain=None):
        self.items.append({"name": name, "value": value, "domain": domain})


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.closed = False

    def close(self):
        self.closed = True


class FakeProxySettings:
    def __init__(self, bundle=None):
        self.bundle = bundle
        self.refreshed = False
        self.session_kwargs_calls = []
        self.build_headers_calls = []
        self.refresh_calls = []

    def build_session_kwargs(self, **kwargs):
        self.session_kwargs_calls.append(kwargs)
        return dict(kwargs, proxy="http://runtime.example:8118")

    def build_headers(self, headers=None, target_url="", proxy="", upstream=True, **kwargs):
        self.build_headers_calls.append({"target_url": target_url, "proxy": proxy, "upstream": upstream})
        merged = dict(headers or {})
        if self.refreshed and self.bundle and self.bundle.cookies:
            merged["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.bundle.cookies.items())
        return merged

    def refresh_clearance(self, target_url="", proxy="", force=False, upstream=True, **kwargs):
        self.refresh_calls.append({"target_url": target_url, "proxy": proxy, "force": force, "upstream": upstream})
        self.refreshed = self.bundle is not None
        return self.bundle


class RegisterProxyRuntimeTests(unittest.TestCase):
    def test_create_session_uses_proxy_settings_without_breaking_existing_proxy_argument(self):
        fake_proxy = FakeProxySettings()
        created = []

        def fake_session_factory(**kwargs):
            session = FakeSession(**kwargs)
            created.append(session)
            return session

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register.requests,
            "Session",
            side_effect=fake_session_factory,
        ):
            session = openai_register.create_session("http://legacy-register.example:8080")

        self.assertIs(session, created[0])
        self.assertEqual(fake_proxy.session_kwargs_calls[0]["proxy"], "http://legacy-register.example:8080")
        self.assertTrue(fake_proxy.session_kwargs_calls[0]["upstream"])
        self.assertEqual(fake_proxy.session_kwargs_calls[0]["impersonate"], "chrome")
        self.assertFalse(fake_proxy.session_kwargs_calls[0]["verify"])
        self.assertEqual(session.kwargs["proxy"], "http://runtime.example:8118")

    def test_cloudflare_without_clearance_keeps_clear_register_error(self):
        fake_proxy = FakeProxySettings(bundle=None)
        cf_response = FakeResponse(
            status_code=403,
            text="<html><title>Just a moment...</title></html>",
            headers={"server": "cloudflare", "content-type": "text/html"},
            url="https://auth.openai.com/api/accounts/authorize",
        )

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", return_value=(cf_response, "")):
            registrar = openai_register.PlatformRegistrar(proxy="http://legacy-register.example:8080")
            with self.assertRaisesRegex(RuntimeError, "Cloudflare") as ctx:
                registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        self.assertIn("status=403", str(ctx.exception))
        self.assertIn("Just a moment", str(ctx.exception))

    def test_openai_html_behind_cloudflare_is_not_treated_as_challenge(self):
        response = FakeResponse(
            status_code=200,
            text="""
            <!DOCTYPE html><html lang=\"en-US\"><head>
            <title>Create a password - OpenAI</title>
            </head><body>OpenAI account page</body></html>
            """,
            headers={"server": "cloudflare", "content-type": "text/html; charset=utf-8"},
            url="https://auth.openai.com/create-account/password",
        )

        self.assertFalse(openai_register._is_cloudflare_challenge(response))

    def test_chatgpt_authorize_keeps_login_hint_for_legacy_password_flow(self):
        fake_proxy = FakeProxySettings()
        session = FakeSession()
        request_calls = []
        responses = [
            FakeResponse(status_code=200, json_data={"csrfToken": "csrf-token"}),
            FakeResponse(
                status_code=200,
                json_data={
                    "url": "https://auth.openai.com/api/accounts/authorize?client_id=chatgpt&device_id=browser-device"
                },
            ),
            FakeResponse(status_code=200, url="https://auth.openai.com/create-account"),
        ]

        def fake_request(_session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, **kwargs})
            return responses.pop(0), ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register, "create_session", return_value=session
        ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request):
            registrar = openai_register.PlatformRegistrar(proxy="")
            registrar._boot_chatgpt_session = mock.Mock()
            registrar._chatgpt_authorize("user@example.com", 1)

        signin = request_calls[1]
        self.assertTrue(signin["url"].startswith("https://chatgpt.com/api/auth/signin/openai?"))
        self.assertIn("screen_hint=login_or_signup", signin["url"])
        self.assertIn("login_hint=user%40example.com", signin["url"])

    def test_sentinel_token_keeps_oai_sc_cookie_for_following_registration_steps(self):
        session = FakeSession()
        with patch.object(
            openai_register,
            "_build_sentinel_token_tuple",
            return_value=("sentinel-token", "oai-sc-token"),
        ):
            token = openai_register.build_sentinel_token(session, "device-id", "authorize_continue")

        self.assertEqual(token, "sentinel-token")
        self.assertIn(
            {"name": "oai-sc", "value": "oai-sc-token", "domain": ".auth.openai.com"},
            session.cookies.items,
        )
        self.assertIn(
            {"name": "oai-sc", "value": "oai-sc-token", "domain": "auth.openai.com"},
            session.cookies.items,
        )

    def test_register_uses_legacy_password_then_existing_otp_pipeline(self):
        fake_proxy = FakeProxySettings()
        mailbox = {"address": "user@example.com", "label": "test", "provider": "test"}
        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register, "create_session", return_value=FakeSession()
        ), patch.object(openai_register, "create_mailbox", return_value=mailbox), patch.object(
            openai_register.mail_provider, "mark_mailbox_result"
        ) as mark_result:
            registrar = openai_register.PlatformRegistrar(proxy="")
            registrar._chatgpt_authorize = mock.Mock()
            registrar._register_user = mock.Mock()
            registrar._send_otp = mock.Mock()
            registrar._validate_mailbox_otp = mock.Mock()
            registrar._create_account = mock.Mock()
            registrar._finish_chatgpt_registration = mock.Mock(
                return_value={"access_token": "chatgpt-token", "session_token": "", "cookie": ""}
            )
            registrar._platform_authorize = mock.Mock(side_effect=RuntimeError("optional oauth unavailable"))
            flow = mock.Mock()
            for method_name in (
                "_chatgpt_authorize",
                "_register_user",
                "_send_otp",
                "_validate_mailbox_otp",
                "_create_account",
                "_finish_chatgpt_registration",
            ):
                flow.attach_mock(getattr(registrar, method_name), method_name)

            result = registrar.register(1)

        self.assertEqual(
            [call[0] for call in flow.mock_calls],
            [
                "_chatgpt_authorize",
                "_register_user",
                "_send_otp",
                "_validate_mailbox_otp",
                "_create_account",
                "_finish_chatgpt_registration",
            ],
        )
        registrar._register_user.assert_called_once()
        registrar._send_otp.assert_called_once_with(1, mailbox)
        registrar._validate_mailbox_otp.assert_called_once_with(mailbox, 1)
        self.assertTrue(result["password"])
        self.assertEqual(result["access_token"], "chatgpt-token")
        mark_result.assert_called_once_with(mailbox, success=True)

    def test_cloudflare_challenge_refreshes_clearance_and_retries_once_with_matching_headers(self):
        bundle = ClearanceBundle(
            target_host="auth.openai.com",
            proxy_url="http://runtime.example:8118",
            cookies={"cf_clearance": "flare-token"},
            user_agent="Flare UA",
        )
        fake_proxy = FakeProxySettings(bundle=bundle)
        responses = [
            FakeResponse(
                status_code=403,
                text="<html><title>Just a moment...</title></html>",
                headers={"server": "cloudflare", "content-type": "text/html"},
                url="https://auth.openai.com/api/accounts/authorize",
            ),
            FakeResponse(status_code=200, text="{}", headers={"content-type": "application/json"}),
        ]
        request_calls = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            return responses.pop(0), ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request):
            registrar = openai_register.PlatformRegistrar(proxy="http://legacy-register.example:8080")
            registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(request_calls), 2)
        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        retry_headers = {key.lower(): value for key, value in request_calls[1]["headers"].items()}
        self.assertEqual(retry_headers["user-agent"], "Flare UA")
        self.assertEqual(retry_headers["cookie"], "cf_clearance=flare-token")
        self.assertEqual(fake_proxy.refresh_calls[0]["target_url"], openai_register.auth_base)
        self.assertEqual(fake_proxy.refresh_calls[0]["proxy"], "http://legacy-register.example:8080")
        self.assertTrue(fake_proxy.refresh_calls[0]["force"])

    def test_refresh_failure_reports_cloudflare_detail_without_infinite_retry(self):
        fake_proxy = FakeProxySettings(bundle=None)
        cf_response = FakeResponse(
            status_code=403,
            text="<html><title>Just a moment...</title><body>challenge body</body></html>",
            headers={"server": "cloudflare", "content-type": "text/html"},
            url="https://auth.openai.com/api/accounts/authorize",
        )
        request_calls = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url})
            return cf_response, ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "request_with_local_retry", side_effect=fake_request):
            registrar = openai_register.PlatformRegistrar(proxy="")
            with self.assertRaisesRegex(RuntimeError, "Cloudflare") as ctx:
                registrar._platform_authorize("user@example.com", 1)

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(len(fake_proxy.refresh_calls), 1)
        message = str(ctx.exception)
        self.assertIn("status=403", message)
        self.assertIn("challenge body", message)

    def test_create_account_sends_sentinel_and_so_headers(self):
        fake_proxy = FakeProxySettings()
        request_calls = []

        class FakeSentinelHeaders:
            so_token = "so-token"

            def as_headers(self):
                return {"OpenAI-Sentinel-Token": "sentinel-token", "OpenAI-Sentinel-SO-Token": self.so_token}

            def log_summary(self):
                return {"sentinel_token_len": 14, "so_token_len": 8, "sdk_version": "test-sdk"}

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            return FakeResponse(status_code=200, json_data={"continue_url": "https://platform.openai.com/auth/callback?code=abc&state=xyz"}), ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "build_sentinel_headers_with_sdk", return_value=FakeSentinelHeaders()), patch.object(
            openai_register,
            "request_with_local_retry",
            side_effect=fake_request,
        ):
            registrar = openai_register.PlatformRegistrar(proxy="")
            registrar._create_account("Test User", "2000-01-01", 1)

        self.assertEqual(len(request_calls), 1)
        headers = request_calls[0]["headers"]
        self.assertEqual(headers["OpenAI-Sentinel-Token"], "sentinel-token")
        self.assertEqual(headers["OpenAI-Sentinel-SO-Token"], "so-token")
        self.assertEqual(registrar.platform_auth_code, "abc")

    def test_register_user_follows_continue_url(self):
        fake_proxy = FakeProxySettings()
        request_calls = []

        def fake_request(session, method, url, retry_attempts=3, **kwargs):
            request_calls.append({"method": method, "url": url, "headers": dict(kwargs.get("headers") or {})})
            if method.lower() == "post":
                return FakeResponse(status_code=200, json_data={"continue_url": "https://auth.openai.com/continue?state=abc"}), ""
            return FakeResponse(status_code=200, url="https://auth.openai.com/about-you"), ""

        with patch.object(openai_register, "proxy_settings", fake_proxy), patch.object(
            openai_register,
            "create_session",
            return_value=FakeSession(),
        ), patch.object(openai_register, "build_sentinel_token", return_value="sentinel-token"), patch.object(
            openai_register,
            "request_with_local_retry",
            side_effect=fake_request,
        ):
            registrar = openai_register.PlatformRegistrar(proxy="")
            registrar._register_user("user@example.com", "Password1!", 1)

        self.assertEqual([call["method"].lower() for call in request_calls], ["post", "get"])
        self.assertEqual(request_calls[1]["url"], "https://auth.openai.com/continue?state=abc")


if __name__ == "__main__":
    unittest.main()
