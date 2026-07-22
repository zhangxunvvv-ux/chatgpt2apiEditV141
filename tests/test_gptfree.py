from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_der_private_key

from services.account_service import AccountService
from services.gptfree_identity_service import (
    DEFAULT_CAPABILITIES,
    GptFreeIdentityError,
    GptFreeIdentityService,
    build_agent_assertion,
    decode_jwt_claims,
    generate_ed25519_keypair,
    validate_access_token,
)
from services.gptfree_response_service import GptFreeResponseService, _upstream_body
from services.storage.json_storage import JSONStorageBackend
from utils.helper import is_image_chat_request, split_image_model


def _b64url(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_access_token(*, exp: int | None = None) -> str:
    payload = {
        "sub": "user-test",
        "exp": exp or int(time.time()) + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-test",
            "chatgpt_user_id": "user-test",
            "chatgpt_plan_type": "free",
        },
        "https://api.openai.com/profile": {"email": "test@example.com"},
    }
    return f"{_b64url({'alg': 'none'})}.{_b64url(payload)}.signature"


class GptFreeIdentityTests(unittest.TestCase):
    def test_keypair_uses_ssh_ed25519_public_key(self) -> None:
        private_key, public_key = generate_ed25519_keypair()
        self.assertTrue(private_key)
        algorithm, encoded = public_key.split(" ", 1)
        self.assertEqual(algorithm, "ssh-ed25519")
        blob = base64.b64decode(encoded)
        algorithm_len = int.from_bytes(blob[:4], "big")
        self.assertEqual(blob[4:4 + algorithm_len], b"ssh-ed25519")
        offset = 4 + algorithm_len
        public_len = int.from_bytes(blob[offset:offset + 4], "big")
        self.assertEqual(public_len, 32)
        self.assertEqual(len(blob[offset + 4:]), 32)

    def test_jwt_validation_rejects_expired_tokens(self) -> None:
        with self.assertRaisesRegex(GptFreeIdentityError, "expired"):
            validate_access_token(fake_access_token(exp=int(time.time()) - 1))
        self.assertEqual(decode_jwt_claims(fake_access_token())["sub"], "user-test")

    def test_runtime_task_and_assertion_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            service = GptFreeIdentityService(root / "identities.json", root / "master.key")
            calls: list[tuple[str, dict, dict]] = []

            def fake_post(url, payload, headers, **_kwargs):
                calls.append((url, payload, headers))
                if url.endswith("/v1/agent/register"):
                    return {"agent_runtime_id": "runtime-test"}
                return {"task_id": "task-test"}

            service._post_json = fake_post  # type: ignore[method-assign]
            identity = service.register_runtime(fake_access_token())
            self.assertEqual(identity["agent_runtime_id"], "runtime-test")
            self.assertTrue(identity["private_key_saved"])
            self.assertNotIn("agent_private_key", identity)

            register_payload = calls[0][1]
            self.assertEqual(register_payload["capabilities"], DEFAULT_CAPABILITIES)
            self.assertEqual(register_payload["abom"]["agent_harness_id"], "codex-cli")
            self.assertIsNone(register_payload["ttl"])
            self.assertIn("agent_public_key", register_payload)
            self.assertTrue(calls[0][2].get("Authorization", "").startswith("Bearer "))
            self.assertNotIn("AgentAssertion", calls[0][2].get("Authorization", ""))

            record = service.get_identity(identity["identity_id"])
            private_key_b64 = record["agent_private_key"]
            private_key = load_der_private_key(base64.b64decode(private_key_b64), password=None)
            self.assertIsInstance(private_key, Ed25519PrivateKey)

            task_id = service.register_task(identity["identity_id"])
            self.assertEqual(task_id, "task-test")
            task_payload = calls[1][1]
            task_message = f"runtime-test:{task_payload['timestamp']}".encode("utf-8")
            private_key.public_key().verify(base64.b64decode(task_payload["signature"]), task_message)

            headers = service.authorization(identity["identity_id"])
            self.assertTrue(headers["Authorization"].startswith("AgentAssertion "))
            self.assertEqual(headers["ChatGPT-Account-ID"], "acct-test")
            self.assertNotIn(fake_access_token(), headers["Authorization"])

            assertion = build_agent_assertion(
                "runtime-test",
                "task-test",
                private_key_b64,
                timestamp="2026-07-22T00:00:00Z",
            )
            assertion_raw = assertion + "=" * ((4 - len(assertion) % 4) % 4)
            assertion_data = json.loads(base64.urlsafe_b64decode(assertion_raw))
            private_key.public_key().verify(
                base64.b64decode(assertion_data["signature"]),
                b"runtime-test:task-test:2026-07-22T00:00:00Z",
            )

            stored_text = (root / "identities.json").read_text(encoding="utf-8")
            self.assertNotIn(private_key_b64, stored_text)
            self.assertNotIn(fake_access_token(), stored_text)
            self.assertNotIn("agent_private_key", service.public_identity(identity["identity_id"]))


class GptFreeRoutingTests(unittest.TestCase):
    def test_text_pool_filters_source_type_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            storage = JSONStorageBackend(root / "accounts.json", root / "keys.json")
            storage.save_accounts([
                {"access_token": "web-token", "source_type": "web", "status": "正常"},
                {"access_token": "gptfree-token", "source_type": "gptfree", "status": "正常"},
            ])
            service = AccountService(storage)
            self.assertEqual(
                service.get_text_access_token(source_type="gptfree", refresh=False),
                "gptfree-token",
            )

    def test_gptfree_model_routes_text_and_image_explicitly(self) -> None:
        self.assertEqual(split_image_model("gptfree"), (None, "gpt-image-2"))
        self.assertFalse(is_image_chat_request({"model": "gptfree"}))
        self.assertTrue(is_image_chat_request({"model": "gptfree", "modalities": ["image"]}))
        self.assertFalse(is_image_chat_request({"model": "gptfree/gpt-5.6-sol", "modalities": ["image"]}))
        payload = _upstream_body({"model": "gptfree", "input": "hello", "stream": False})
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])

    def test_responses_use_agent_assertion_without_web_token(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def iter_lines():
                event = {"type": "response.output_text.delta", "delta": "hello"}
                yield f"data: {json.dumps(event)}".encode("utf-8")
                yield b"data: [DONE]"

        class FakeSession:
            def post(self, _url, **kwargs):
                captured.update(kwargs)
                return FakeResponse()

            @staticmethod
            def close():
                return None

        account = {
            "source_type": "gptfree",
            "gptfree_identity_id": "identity-test",
            "account_id": "acct-test",
        }
        service = GptFreeResponseService()
        with (
            patch("services.gptfree_response_service.account_service.get_text_access_token", return_value="web-secret"),
            patch("services.gptfree_response_service.account_service.get_account", return_value=account),
            patch("services.gptfree_response_service.account_service.mark_text_used"),
            patch(
                "services.gptfree_response_service.gptfree_identity_service.authorization",
                return_value={"Authorization": "AgentAssertion assertion-test", "ChatGPT-Account-ID": "acct-test"},
            ),
            patch("services.gptfree_response_service.requests.Session", return_value=FakeSession()),
            patch("services.gptfree_response_service.proxy_settings.build_session_kwargs", return_value={}),
        ):
            events = list(service.stream({"model": "gptfree", "input": "hello"}))

        self.assertEqual(events[0]["delta"], "hello")
        headers = captured["headers"]
        self.assertEqual(headers["Authorization"], "AgentAssertion assertion-test")
        self.assertNotIn("web-secret", json.dumps(headers))


if __name__ == "__main__":
    unittest.main()
