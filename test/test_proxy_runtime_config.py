import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from services.config import (
    DEFAULT_PROXY_RUNTIME,
    ConfigStore,
    _normalize_proxy_runtime_settings,
)


class ProxyRuntimeConfigTests(unittest.TestCase):
    def _make_store(self, initial: dict[str, object] | None = None) -> tuple[tempfile.TemporaryDirectory[str], ConfigStore]:
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "config.json"
        data = {"auth-key": "test-auth"}
        if initial:
            data.update(initial)
        path.write_text(json.dumps(data), encoding="utf-8")
        return tmp_dir, ConfigStore(path)

    def test_defaults_are_safe_and_included_in_public_config(self) -> None:
        tmp_dir, store = self._make_store({"proxy": " http://legacy.example:8080 "})
        with tmp_dir:
            expected_default = copy.deepcopy(DEFAULT_PROXY_RUNTIME)
            runtime = store.get_proxy_runtime_settings()
            self.assertEqual(runtime, expected_default)
            self.assertEqual(store.get_proxy_settings(), "http://legacy.example:8080")

            expected_public = copy.deepcopy(expected_default)
            expected_public["clearance"]["has_cf_cookies"] = False
            expected_public["clearance"]["has_cf_clearance"] = False
            public_config = store.get()
            self.assertEqual(public_config["proxy"], " http://legacy.example:8080 ")
            self.assertEqual(public_config["proxy_runtime"], expected_public)
            self.assertNotIn("auth-key", public_config)

            runtime["enabled"] = True
            runtime["clearance"]["enabled"] = True
            self.assertEqual(DEFAULT_PROXY_RUNTIME, expected_default)
            self.assertEqual(store.get_proxy_runtime_settings(), expected_default)

    def test_normalize_proxy_runtime_sanitizes_invalid_values(self) -> None:
        normalized = _normalize_proxy_runtime_settings(
            {
                "enabled": "yes",
                "egress_mode": "tor",
                "proxy_url": "  http://proxy.example:8080  ",
                "resource_proxy_url": "  socks5://resource.example:1080  ",
                "skip_ssl_verify": "on",
                "reset_session_status_codes": ["403", 429, 99, 600, "bad", None, True],
                "clearance": {
                    "enabled": "0",
                    "mode": "manual",
                    "cf_cookies": "  a=b; c=d  ",
                    "cf_clearance": "  token  ",
                    "user_agent": "  Custom UA  ",
                    "browser": "  firefox  ",
                    "flaresolverr_url": "  http://flare.example/  ",
                    "timeout_sec": 0,
                    "refresh_interval": 59,
                    "warm_up_on_start": "true",
                },
            }
        )

        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["egress_mode"], "direct")
        self.assertEqual(normalized["proxy_url"], "http://proxy.example:8080")
        self.assertEqual(normalized["resource_proxy_url"], "socks5://resource.example:1080")
        self.assertTrue(normalized["skip_ssl_verify"])
        self.assertEqual(normalized["reset_session_status_codes"], [403, 429])

        clearance = normalized["clearance"]
        self.assertFalse(clearance["enabled"])
        self.assertEqual(clearance["mode"], "manual")
        self.assertEqual(clearance["cf_cookies"], "a=b; c=d")
        self.assertEqual(clearance["cf_clearance"], "token")
        self.assertEqual(clearance["user_agent"], "Custom UA")
        self.assertEqual(clearance["browser"], "firefox")
        self.assertEqual(clearance["flaresolverr_url"], "http://flare.example/")
        self.assertEqual(clearance["timeout_sec"], 1)
        self.assertEqual(clearance["refresh_interval"], 60)
        self.assertTrue(clearance["warm_up_on_start"])

    def test_normalize_proxy_runtime_uses_defaults_for_missing_or_empty_values(self) -> None:
        self.assertEqual(_normalize_proxy_runtime_settings(None), DEFAULT_PROXY_RUNTIME)
        self.assertEqual(
            _normalize_proxy_runtime_settings(
                {
                    "egress_mode": "single_proxy",
                    "reset_session_status_codes": ["bad", 99, 600],
                    "clearance": {
                        "enabled": True,
                        "mode": "invalid",
                        "user_agent": "",
                        "browser": "",
                        "timeout_sec": "bad",
                        "refresh_interval": "bad",
                    },
                }
            ),
            {
                **DEFAULT_PROXY_RUNTIME,
                "egress_mode": "single_proxy",
                "reset_session_status_codes": [403],
                "clearance": {
                    **DEFAULT_PROXY_RUNTIME["clearance"],
                    "enabled": True,
                    "mode": "none",
                },
            },
        )

    def test_malformed_proxy_runtime_values_fall_back_safely(self) -> None:
        normalized = _normalize_proxy_runtime_settings(
            {
                "enabled": "maybe",
                "skip_ssl_verify": "maybe",
                "reset_session_status_codes": [math.inf, "NaN", "403"],
                "clearance": {
                    "enabled": "maybe",
                    "timeout_sec": math.inf,
                    "refresh_interval": -math.inf,
                    "warm_up_on_start": "maybe",
                },
            }
        )

        self.assertFalse(normalized["enabled"])
        self.assertFalse(normalized["skip_ssl_verify"])
        self.assertEqual(normalized["reset_session_status_codes"], [403])
        clearance = normalized["clearance"]
        self.assertFalse(clearance["enabled"])
        self.assertEqual(clearance["timeout_sec"], DEFAULT_PROXY_RUNTIME["clearance"]["timeout_sec"])
        self.assertEqual(clearance["refresh_interval"], DEFAULT_PROXY_RUNTIME["clearance"]["refresh_interval"])
        self.assertFalse(clearance["warm_up_on_start"])

    def test_update_normalizes_and_persists_proxy_runtime(self) -> None:
        tmp_dir, store = self._make_store()
        with tmp_dir:
            public_config = store.update(
                {
                    "proxy_runtime": {
                        "enabled": "true",
                        "egress_mode": "single_proxy",
                        "proxy_url": "  http://proxy.example  ",
                        "reset_session_status_codes": [401, "403", "nope"],
                        "clearance": {
                            "enabled": "yes",
                            "mode": "flaresolverr",
                            "flaresolverr_url": " http://localhost:8191 ",
                            "timeout_sec": "30",
                            "refresh_interval": "120",
                        },
                    }
                }
            )

            expected = {
                **DEFAULT_PROXY_RUNTIME,
                "enabled": True,
                "egress_mode": "single_proxy",
                "proxy_url": "http://proxy.example",
                "reset_session_status_codes": [401, 403],
                "clearance": {
                    **DEFAULT_PROXY_RUNTIME["clearance"],
                    "enabled": True,
                    "mode": "flaresolverr",
                    "flaresolverr_url": "http://localhost:8191",
                    "timeout_sec": 30,
                    "refresh_interval": 120,
                },
            }
            expected_public = copy.deepcopy(expected)
            expected_public["clearance"]["cf_cookies"] = ""
            expected_public["clearance"]["cf_clearance"] = ""
            expected_public["clearance"]["has_cf_cookies"] = False
            expected_public["clearance"]["has_cf_clearance"] = False
            self.assertEqual(public_config["proxy_runtime"], expected_public)

            raw_saved = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(raw_saved["proxy_runtime"], expected)
            reloaded = ConfigStore(store.path)
            self.assertEqual(reloaded.get_proxy_runtime_settings(), expected)

    def test_public_proxy_runtime_redacts_and_preserves_existing_clearance_values(self) -> None:
        existing = copy.deepcopy(DEFAULT_PROXY_RUNTIME)
        existing["enabled"] = True
        existing["clearance"]["enabled"] = True
        existing["clearance"]["mode"] = "manual"
        existing["clearance"]["cf_cookies"] = "session=secret-cookie"
        existing["clearance"]["cf_clearance"] = "secret-clearance"
        tmp_dir, store = self._make_store({"proxy_runtime": existing})
        with tmp_dir:
            public_config = store.get()
            public_runtime = public_config["proxy_runtime"]
            public_clearance = public_runtime["clearance"]
            self.assertEqual(public_clearance["cf_cookies"], "")
            self.assertEqual(public_clearance["cf_clearance"], "")
            self.assertTrue(public_clearance["has_cf_cookies"])
            self.assertTrue(public_clearance["has_cf_clearance"])
            self.assertNotIn("secret-cookie", json.dumps(public_config))
            self.assertNotIn("secret-clearance", json.dumps(public_config))

            public_clearance["user_agent"] = "Updated UA"
            updated_public = store.update({"proxy_runtime": public_runtime})
            updated_raw = json.loads(store.path.read_text(encoding="utf-8"))["proxy_runtime"]
            self.assertEqual(updated_raw["clearance"]["cf_cookies"], "session=secret-cookie")
            self.assertEqual(updated_raw["clearance"]["cf_clearance"], "secret-clearance")
            self.assertEqual(updated_raw["clearance"]["user_agent"], "Updated UA")
            self.assertEqual(updated_public["proxy_runtime"]["clearance"]["cf_cookies"], "")
            self.assertEqual(updated_public["proxy_runtime"]["clearance"]["cf_clearance"], "")


if __name__ == "__main__":
    unittest.main()
