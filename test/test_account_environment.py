from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.openai_backend_api import OpenAIBackendAPI
from services.storage.json_storage import JSONStorageBackend


class _CookieJar:
    def __init__(self) -> None:
        self.values: list[tuple[str, str, str]] = []

    def set(self, name: str, value: str, domain: str = "") -> None:
        self.values.append((name, value, domain))

    def get_dict(self) -> dict[str, str]:
        return {name: value for name, value, _domain in self.values}

    def update(self, _values) -> None:
        return None


class _Session:
    def __init__(self, response=None) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = _CookieJar()
        self.response = response
        self.last_get: dict | None = None

    def get(self, url: str, **kwargs):
        self.last_get = {"url": url, **kwargs}
        return self.response

    def close(self) -> None:
        return None


class _Response:
    status_code = 200
    text = '{"accessToken":"next-token"}'

    @staticmethod
    def json() -> dict[str, str]:
        return {"accessToken": "next-token"}


def _fingerprint() -> dict[str, str]:
    return {
        "user-agent": "Registered Browser/1.0",
        "impersonate": "chrome",
        "oai-device-id": "registered-device",
        "oai-session-id": "registered-session",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Registered";v="1"',
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": '"1.0"',
        "sec-ch-ua-full-version-list": '"Registered";v="1.0"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-model": '"Phone"',
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua-platform-version": '"15.0.0"',
    }


class AccountEnvironmentTests(unittest.TestCase):
    def test_first_legacy_fingerprint_wins_for_later_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "legacy-token"}])
            first = {**_fingerprint(), "oai-device-id": "first-device"}
            second = {**_fingerprint(), "oai-device-id": "second-device"}

            self.assertEqual(service.ensure_account_fp("legacy-token", first)["oai-device-id"], "first-device")
            self.assertEqual(service.ensure_account_fp("legacy-token", second)["oai-device-id"], "first-device")
            self.assertEqual(service.get_account("legacy-token")["fp"]["oai-device-id"], "first-device")

    def test_backend_reuses_saved_fingerprint_proxy_and_cookies(self) -> None:
        account = {
            "access_token": "token",
            "proxy": "http://registered-proxy:8080",
            "cookie": "session=registered-cookie; device=registered-device",
            "fp": _fingerprint(),
        }
        session = _Session()
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value=account),
            mock.patch(
                "services.openai_backend_api.account_service.ensure_account_fp",
                side_effect=lambda _token, fp: fp,
            ) as ensure_fp,
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}) as build_kwargs,
            mock.patch("services.openai_backend_api.requests.Session", return_value=session),
        ):
            backend = OpenAIBackendAPI("token")

        ensure_fp.assert_called_once()
        self.assertEqual(build_kwargs.call_args.kwargs["account"], account)
        self.assertEqual(build_kwargs.call_args.kwargs["impersonate"], "chrome")
        self.assertEqual(backend.device_id, "registered-device")
        self.assertEqual(backend.session_id, "registered-session")
        self.assertEqual(session.headers["User-Agent"], "Registered Browser/1.0")
        self.assertEqual(session.headers["Sec-Ch-Ua-Platform"], '"Android"')
        self.assertIn(("session", "registered-cookie", ".chatgpt.com"), session.cookies.values)
        backend.close()

    def test_legacy_account_fingerprint_is_persisted_once(self) -> None:
        account = {"access_token": "legacy-token", "cookie": ""}
        session = _Session()
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value=account),
            mock.patch(
                "services.openai_backend_api.account_service.ensure_account_fp",
                side_effect=lambda _token, fp: fp,
            ) as ensure_fp,
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}),
            mock.patch("services.openai_backend_api.requests.Session", return_value=session),
        ):
            backend = OpenAIBackendAPI("legacy-token")

        persisted_fp = ensure_fp.call_args.args[1]
        self.assertEqual(persisted_fp["oai-device-id"], backend.device_id)
        self.assertEqual(persisted_fp["oai-session-id"], backend.session_id)
        self.assertTrue(persisted_fp["user-agent"])
        backend.close()

    def test_chatgpt_session_refresh_uses_account_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            account = {
                "access_token": "token",
                "proxy": "http://registered-proxy:8080",
                "cookie": "session=registered-cookie",
                "fp": _fingerprint(),
            }
            session = _Session(_Response())
            with (
                mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={}) as build_kwargs,
                mock.patch("curl_cffi.requests.Session", return_value=session),
            ):
                result = service._request_chatgpt_session_refresh(account)

        self.assertEqual(result["access_token"], "next-token")
        self.assertEqual(build_kwargs.call_args.kwargs["account"], account)
        self.assertEqual(build_kwargs.call_args.kwargs["impersonate"], "chrome")
        headers = session.last_get["headers"]
        self.assertEqual(headers["User-Agent"], "Registered Browser/1.0")
        self.assertEqual(headers["OAI-Device-Id"], "registered-device")
        self.assertEqual(headers["OAI-Session-Id"], "registered-session")


if __name__ == "__main__":
    unittest.main()
