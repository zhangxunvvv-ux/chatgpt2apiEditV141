from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.protocol import openai_v1_chat_complete, openai_v1_image_generations, openai_v1_response
from services.storage.json_storage import JSONStorageBackend


class AccountPoolTests(unittest.TestCase):
    def test_default_and_gptfree_candidates_are_strictly_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            storage = JSONStorageBackend(root / "accounts.json", root / "keys.json")
            storage.save_accounts([
                {
                    "access_token": "default-token",
                    "source_type": "web",
                    "status": "正常",
                    "quota": 10,
                },
                {
                    "access_token": "gptfree-token",
                    "source_type": "gptfree",
                    "status": "正常",
                    "quota": 10,
                },
            ])
            service = AccountService(storage)

            self.assertEqual(
                service.get_text_access_token(source_type="default", refresh=False),
                "default-token",
            )
            self.assertEqual(
                service.get_text_access_token(source_type="gptfree", refresh=False),
                "gptfree-token",
            )
            self.assertEqual(service._list_ready_candidate_tokens(source_type="default"), ["default-token"])
            self.assertEqual(service._list_ready_candidate_tokens(source_type="gptfree"), ["gptfree-token"])

    def test_image_request_passes_selected_pool_to_scheduler(self) -> None:
        captured = {}

        def fake_stream(request):
            captured["request"] = request
            return iter(())

        with (
            patch.object(openai_v1_image_generations, "stream_image_outputs_with_pool", side_effect=fake_stream),
            patch.object(openai_v1_image_generations, "collect_image_outputs", return_value={"data": []}),
        ):
            openai_v1_image_generations.handle({
                "prompt": "test",
                "model": "gpt-image-2",
                "account_pool": "gptfree",
            })

        self.assertEqual(captured["request"].source_type, "gptfree")

    def test_chat_account_pool_routes_normal_model_through_gptfree(self) -> None:
        sentinel = iter([{"type": "sentinel"}])
        with patch.object(
            openai_v1_chat_complete,
            "stream_gptfree_chat_completion",
            return_value=sentinel,
        ) as stream_gptfree:
            result = openai_v1_chat_complete.handle({
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "account_pool": "gptfree",
            })

        self.assertIs(result, sentinel)
        stream_gptfree.assert_called_once()

    def test_responses_account_pool_routes_normal_model_through_gptfree(self) -> None:
        event = {"type": "response.completed", "response": {"output": []}}
        with patch.object(openai_v1_response.gptfree_response_service, "stream", return_value=iter([event])):
            result = list(openai_v1_response.response_events({
                "model": "auto",
                "input": "hello",
                "account_pool": "gptfree",
            }))

        self.assertEqual(result, [event])


if __name__ == "__main__":
    unittest.main()
