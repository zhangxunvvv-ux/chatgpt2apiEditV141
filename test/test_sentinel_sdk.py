import shutil
import unittest
from unittest.mock import call, patch

from utils import sentinel


class SentinelSdkTests(unittest.TestCase):
    def test_missing_req_body_is_retried_before_posting_requirements(self):
        final_result = {
            "version": "test-version",
            "token": "sentinel-token",
            "soToken": "session-observer-token",
        }

        with patch.object(
            sentinel,
            "_discover_sdk",
            return_value=("https://sentinel.openai.com/sentinel/test-version/sdk.js", "sdk-source", "test-version"),
        ), patch.object(
            sentinel,
            "_run_sdk",
            side_effect=[{}, {"capturedBody": '{"flow":"oauth_create_account"}'}, final_result],
        ) as run_sdk, patch.object(
            sentinel,
            "_post_sentinel_req",
            return_value=(200, {"token": "challenge", "so": {"required": True}}),
        ) as post_req, patch.object(sentinel.time, "sleep") as sleep:
            headers = sentinel.build_sentinel_headers_with_sdk(
                object(),
                "device-id",
                "oauth_create_account",
                observer_wait_ms=5000,
            )

        self.assertEqual(headers.sentinel_token, "sentinel-token")
        self.assertEqual(headers.so_token, "session-observer-token")
        self.assertTrue(headers.req_has_so)
        self.assertEqual(run_sdk.call_count, 3)
        sleep.assert_called_once_with(sentinel.SDK_REQ_CAPTURE_RETRY_DELAY_SECONDS)
        self.assertEqual(post_req.call_args.args[2], '{"flow":"oauth_create_account"}')
        self.assertEqual(run_sdk.call_args.kwargs["wait_ms"], 5000)

    def test_missing_req_body_fails_only_after_bounded_retries(self):
        with patch.object(
            sentinel,
            "_discover_sdk",
            return_value=("https://sentinel.openai.com/sentinel/test-version/sdk.js", "sdk-source", "test-version"),
        ), patch.object(sentinel, "_run_sdk", return_value={}) as run_sdk, patch.object(
            sentinel.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "sentinel_sdk_missing_req_body"):
                sentinel.build_sentinel_headers_with_sdk(object(), "device-id", "oauth_create_account")

        self.assertEqual(run_sdk.call_count, sentinel.SDK_REQ_CAPTURE_ATTEMPTS)
        self.assertEqual(
            sleep.call_args_list,
            [call(sentinel.SDK_REQ_CAPTURE_RETRY_DELAY_SECONDS)] * (sentinel.SDK_REQ_CAPTURE_ATTEMPTS - 1),
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the Sentinel SDK runner")
    def test_runner_waits_after_token_before_minting_session_observer_token(self):
        sdk_source = """
        window.SentinelSDK = {
          token: async function (flow) {
            await fetch('/backend-api/sentinel/req', {
              method: 'POST',
              body: JSON.stringify({ flow: flow })
            });
            setTimeout(function () { window.__observerReady = true; }, 5);
            return 'sentinel-token';
          },
          sessionObserverToken: async function () {
            return window.__observerReady ? 'so-token' : 'so-too-early';
          }
        };
        """

        result = sentinel._run_sdk(
            sdk_source=sdk_source,
            sdk_url="https://sentinel.openai.com/sentinel/test-version/sdk.js",
            flow="oauth_create_account",
            device_id="device-id",
            user_agent="test-agent",
            req_data={"token": "challenge", "so": {"required": True}},
            wait_ms=20,
        )

        self.assertEqual(result["version"], "test-version")
        self.assertEqual(result["token"], "sentinel-token")
        self.assertEqual(result["soToken"], "so-token")
        self.assertEqual(result["capturedBody"], '{"flow":"oauth_create_account"}')


if __name__ == "__main__":
    unittest.main()
