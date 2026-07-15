from __future__ import annotations

import base64
import unittest
from io import BytesIO
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import api.ai as ai_module
from services.protocol.openai_v1_image_edit import _composite_mask


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
DATA_IMAGE_URL = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}"


class ImagesEditsApiTests(unittest.TestCase):
    def setUp(self):
        self.handle_calls = []

        def fake_handle(payload):
            self.handle_calls.append(payload)
            return {"created": 1, "data": [{"b64_json": base64.b64encode(b"out").decode("ascii")}]}

        self.handler_patcher = mock.patch.object(ai_module.openai_v1_image_edit, "handle", fake_handle)
        self.handler_patcher.start()
        self.addCleanup(self.handler_patcher.stop)
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_edit_accepts_json_image_url(self):
        """测试图片编辑接口支持官方 JSON image_url 引用。"""
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "edit",
                "images": [{"image_url": DATA_IMAGE_URL}],
                "n": 1,
                "response_format": "b64_json",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.handle_calls), 1)
        payload = self.handle_calls[0]
        self.assertEqual(payload["prompt"], "edit")
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["images"], [(PNG_BYTES, "image_url.png", "image/png")])

    def test_edit_rejects_file_id_reference(self):
        """测试图片编辑接口对暂不支持的 file_id 返回明确错误。"""
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "edit",
                "images": [{"file_id": "file-abc123"}],
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("file_id image references are not supported", response.text)
        self.assertEqual(self.handle_calls, [])

    def test_mask_transparent_pixels_mark_the_edit_region(self):
        source_buffer = BytesIO()
        Image.new("RGBA", (2, 1), (20, 40, 60, 255)).save(source_buffer, format="PNG")
        mask = Image.new("L", (2, 1), 255)
        mask.putpixel((0, 0), 0)
        mask_buffer = BytesIO()
        mask.save(mask_buffer, format="PNG")

        result = _composite_mask(
            [(source_buffer.getvalue(), "source.png", "image/png")],
            [(mask_buffer.getvalue(), "mask.png", "image/png")],
        )
        composited = Image.open(BytesIO(result[0][0])).convert("RGBA")

        self.assertEqual(composited.getpixel((0, 0))[3], 0)
        self.assertEqual(composited.getpixel((1, 0))[3], 255)


if __name__ == "__main__":
    unittest.main()
