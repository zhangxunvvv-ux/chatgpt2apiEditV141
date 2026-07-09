#!/usr/bin/env python3
# Test script: directly call https://chatgpt.com/backend-api/codex/responses to generate one 2K image.
# Only edit ACCESS_TOKEN, then run: python codex_responses_image_test.py
# Fixed request parameters:
#   prompt: A highly detailed square 2K image of a quiet futuristic library at sunrise
#   responses model: gpt-5.5
#   image model: gpt-image-2
#   size: 2048x2048
#   quality: auto
#   output_format: png
#   output file: codex_4k.png

import base64
import json
import time
import urllib.request

ACCESS_TOKEN = ""


def parse_events(raw):
    ctype, text = raw.headers.get("content-type", ""), raw.read().decode("utf-8", "replace")
    if "application/json" in ctype:
        return [json.loads(text)]
    events, lines = [], []
    for line in text.splitlines() + [""]:
        if not line:
            if lines:
                data = "\n".join(lines).strip()
                if data and data != "[DONE]":
                    events.append(json.loads(data))
                lines = []
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
    return events


def find_images(value):
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        return [image for item in value.values() for image in find_images(item)]
    if isinstance(value, list):
        return [image for item in value for image in find_images(item)]
    return []


def main():
    start_time = time.time()
    body = {
        "model": "gpt-5.5",
        "instructions": "Use the image_generation tool to create exactly one image for the user's request. Return the generated image result.",
        "store": False,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "A highly detailed square 2K image of a quiet futuristic library at sunrise"}]}],
        "tools": [{
            "type": "image_generation",
            "model": "gpt-image-2",
            "action": "generate",
            "size": "3840x2160",
            "quality": "auto",
            "output_format": "png"
        }],
        "tool_choice": {"type": "image_generation"},
        "stream": True
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

    req = urllib.request.Request("https://chatgpt.com/backend-api/codex/responses", json.dumps(body).encode(), headers, method="POST")
    try:
        images = find_images(parse_events(urllib.request.urlopen(req, timeout=1200)))
    except urllib.error.HTTPError as error:
        raise SystemExit(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:1000]}")

    if not images:
        raise SystemExit("No image result found in response")

    with open("codex_4k.png", "wb") as file:
        file.write(base64.b64decode(images[0]))
    print("saved codex_4k.png")
    end_time = time.time()
    print(f"total time: {end_time - start_time:.2f} seconds")


if __name__ == "__main__":
    main()
