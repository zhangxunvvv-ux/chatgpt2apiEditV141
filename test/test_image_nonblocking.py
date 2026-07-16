from __future__ import annotations

import threading
import unittest
from unittest import mock

from services.protocol import conversation


class _FakeAccountService:
    def __init__(self) -> None:
        self.releases: list[str] = []
        self.results: list[tuple[str, bool, bool]] = []

    def get_available_access_token(self, **_kwargs) -> str:
        return "token-a"

    def get_account(self, _token: str) -> dict:
        return {"email": "image@example.com"}

    def release_image_slot(self, token: str) -> None:
        self.releases.append(token)

    def mark_image_result(self, token: str, success: bool, *, release_slot: bool = True, error: object = "") -> None:
        self.results.append((token, success, release_slot))


class _FakeBackend:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self.progress_callback = None

    def close(self) -> None:
        return None


class ImageNonBlockingTests(unittest.TestCase):
    def test_polling_releases_submission_slot_without_double_release(self) -> None:
        accounts = _FakeAccountService()

        def fake_stream(_backend, request, index, total):
            request.progress_callback({"step": "image_stream_resolve_start", "conversation_id": "conv-1"})
            self.assertEqual(accounts.releases, ["token-a"])
            yield conversation.ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"b64_json": "aW1hZ2U="}],
                conversation_id="conv-1",
            )

        request = conversation.ConversationRequest(model="gpt-image-2", prompt="cat")
        with (
            mock.patch.object(conversation, "account_service", accounts),
            mock.patch.object(conversation, "OpenAIBackendAPI", _FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream),
        ):
            outputs = conversation._generate_single_image(request, 1, 1)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(accounts.releases, ["token-a"])
        self.assertEqual(accounts.results, [("token-a", True, False)])

    def test_cooperative_cancel_releases_slot_without_counting_failure(self) -> None:
        accounts = _FakeAccountService()
        cancel_event = threading.Event()

        def fake_stream(_backend, request, _index, _total):
            cancel_event.set()
            request.progress_callback({"step": "submitting"})
            return iter(())

        request = conversation.ConversationRequest(
            model="gpt-image-2",
            prompt="cat",
            cancel_event=cancel_event,
        )
        with (
            mock.patch.object(conversation, "account_service", accounts),
            mock.patch.object(conversation, "OpenAIBackendAPI", _FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream),
        ):
            with self.assertRaises(conversation.ImageGenerationCancelled):
                conversation._generate_single_image(request, 1, 1)

        self.assertEqual(accounts.releases, ["token-a"])
        self.assertEqual(accounts.results, [])


if __name__ == "__main__":
    unittest.main()
