from __future__ import annotations

from io import BytesIO
from typing import Any, Iterator

from PIL import Image

from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    count_text_tokens,
    encode_images,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from utils.image_tokens import count_image_inputs_tokens, count_image_output_items_tokens, image_usage


MASK_GUIDE_COLOR = (16, 185, 129)


def _mask_alpha(mask_data: bytes, size: tuple[int, int]) -> Image.Image:
    mask_img = Image.open(BytesIO(mask_data))
    if mask_img.mode == "RGBA":
        alpha = mask_img.split()[3]
    elif mask_img.mode == "L":
        alpha = mask_img
    else:
        alpha = mask_img.convert("L")
    return alpha.resize(size, Image.LANCZOS)


def _composite_mask(
    images: list[tuple[bytes, str, str]],
    masks: list[tuple[bytes, str, str]],
) -> list[tuple[bytes, str, str]]:
    """将 mask 的 alpha 通道合成到图片中，标识需要编辑的区域。
    
    mask 的透明区域（低 alpha）= 需要编辑的区域，
    mask 的不透明区域（高 alpha）= 保留的区域。
    如果无 mask 则返回原图。
    """
    if not masks:
        return images
    result: list[tuple[bytes, str, str]] = []
    for i, (data, filename, mime_type) in enumerate(images):
        mask_data = masks[i][0] if i < len(masks) else masks[-1][0]
        img = Image.open(BytesIO(data)).convert("RGBA")
        alpha = _mask_alpha(mask_data, img.size)
        img.putalpha(alpha)
        buf = BytesIO()
        img.save(buf, format="PNG")
        result.append((buf.getvalue(), filename, "image/png"))
    return result


def _build_mask_guides(
    images: list[tuple[bytes, str, str]],
    masks: list[tuple[bytes, str, str]],
) -> list[tuple[bytes, str, str]]:
    """Create visible guides because ChatGPT Web may not reliably infer alpha masks."""
    if not masks:
        return []
    guides: list[tuple[bytes, str, str]] = []
    for i, (data, filename, _mime_type) in enumerate(images):
        mask_data = masks[i][0] if i < len(masks) else masks[-1][0]
        source = Image.open(BytesIO(data)).convert("RGBA")
        preserve_alpha = _mask_alpha(mask_data, source.size)
        if preserve_alpha.getextrema()[0] >= 255:
            continue

        edit_alpha = preserve_alpha.point(lambda value: round((255 - value) * 0.58))
        background = Image.new("RGBA", source.size, (255, 255, 255, 255))
        guide = Image.alpha_composite(background, source)
        overlay = Image.new("RGBA", source.size, (*MASK_GUIDE_COLOR, 0))
        overlay.putalpha(edit_alpha)
        guide = Image.alpha_composite(guide, overlay)

        buf = BytesIO()
        guide.convert("RGB").save(buf, format="PNG")
        guides.append((buf.getvalue(), f"mask-guide-{filename}", "image/png"))
    return guides


def _append_mask_instructions(prompt: str, guide_count: int) -> str:
    if guide_count <= 0:
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        "[蒙版编辑协议]\n"
        f"附件末尾的 {guide_count} 张绿色半透明图片是蒙版定位图，不是额外参考图。\n"
        "绿色覆盖区域就是必须修改的区域；对应原参考图的同一区域也已通过透明 Alpha 标记。\n"
        "只修改绿色/透明蒙版区域，未覆盖区域尽量保持原样，最终结果中不要保留绿色蒙版或标记。"
    )


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    images = body.get("images") or []
    masks = body.get("mask") or []
    mask_guides = _build_mask_guides(images, masks)
    images = _composite_mask(images, masks) + mask_guides
    prompt = _append_mask_instructions(prompt, len(mask_guides))
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    cancel_event = body.get("cancel_event")
    encoded_images = encode_images(images)
    if not encoded_images:
        raise ImageGenerationError("image is required")
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        images=encoded_images,
        message_as_error=True,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    ))
    if body.get("stream"):
        return stream_image_chunks(outputs)
    result = collect_image_outputs(outputs)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result
