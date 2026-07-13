"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import random
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from curl_cffi.requests import Session


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。"""
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


# ── 默认 User-Agent 和 sec-ch-ua ──────────────────────────────
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"'
SENTINEL_ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"
SDK_ENTRYPOINTS = (
    "https://auth.openai.com/backend-api/sentinel/sdk.js",
    "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
    "https://chatgpt.com/backend-api/sentinel/sdk.js",
)
SDK_RUNNER_PATH = Path(__file__).with_name("sentinel_sdk_runner.js")
SDK_REQ_CAPTURE_ATTEMPTS = 3
SDK_REQ_CAPTURE_RETRY_DELAY_SECONDS = 0.2


@dataclass
class SentinelHeaders:
    sentinel_token: str
    so_token: str = ""
    sdk_version: str = ""
    sdk_url: str = ""
    req_url: str = ""
    req_has_so: bool = False

    def as_headers(self) -> dict[str, str]:
        headers = {"OpenAI-Sentinel-Token": self.sentinel_token}
        if self.so_token:
            headers["OpenAI-Sentinel-SO-Token"] = self.so_token
        return headers

    def log_summary(self) -> dict[str, object]:
        return {
            "sentinel_token_len": len(self.sentinel_token or ""),
            "so_token_len": len(self.so_token or ""),
            "so_token_generated": bool(self.so_token),
            "req_has_so": self.req_has_so,
            "sdk_version": self.sdk_version,
        }


def _sdk_headers(user_agent: str, sec_ch_ua: str, referer: str = "") -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _response_text(resp) -> str:
    try:
        return str(resp.text or "")
    except Exception:
        return ""


def _download_text(session: "Session", url: str, user_agent: str, sec_ch_ua: str, referer: str = "") -> str:
    try:
        resp = session.get(url, headers=_sdk_headers(user_agent, sec_ch_ua, referer), timeout=20, verify=False)
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(f"sdk_download_failed_{getattr(resp, 'status_code', 'unknown')}")
        text = _response_text(resp)
    except Exception:
        req = urllib.request.Request(url, headers=_sdk_headers(user_agent, sec_ch_ua, referer))
        with urllib.request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
    if not text or "<html" in text[:200].lower():
        raise RuntimeError("sdk_download_returned_html")
    return text


def _discover_sdk(session: "Session", user_agent: str, sec_ch_ua: str) -> tuple[str, str, str]:
    last_error = ""
    for entry_url in SDK_ENTRYPOINTS:
        try:
            entry_source = _download_text(session, entry_url, user_agent, sec_ch_ua)
            match = re.search(r"script\.src\s*=\s*['\"]([^'\"]+)['\"]", entry_source)
            sdk_url = urljoin(entry_url, match.group(1)) if match else entry_url
            sdk_source = _download_text(session, sdk_url, user_agent, sec_ch_ua, entry_url)
            parsed_sdk = urlparse(sdk_url)
            if parsed_sdk.netloc == "chatgpt.com" and parsed_sdk.path.startswith("/sentinel/"):
                sdk_url = f"https://sentinel.openai.com{parsed_sdk.path}"
            version_match = re.search(r"/sentinel/([^/]+)/sdk\.js", sdk_url)
            return sdk_url, sdk_source, (version_match.group(1) if version_match else "")
        except Exception as error:
            last_error = f"{entry_url}: {error}"
            continue
    raise RuntimeError(f"sentinel_sdk_unavailable: {last_error}")


def _run_sdk(
    *,
    sdk_source: str,
    sdk_url: str,
    flow: str,
    device_id: str,
    user_agent: str,
    req_data: dict | None = None,
    wait_ms: int = 0,
) -> dict:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node_not_found_for_sentinel_sdk")
    payload = {
        "sdkSource": sdk_source,
        "sdkUrl": sdk_url,
        "flow": flow,
        "deviceId": device_id,
        "userAgent": user_agent,
        "reqData": req_data,
        "waitMs": wait_ms,
    }
    proc = subprocess.run(
        [node, str(SDK_RUNNER_PATH)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=max(15, int(wait_ms / 1000) + 15),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sentinel_sdk_runner_failed: {proc.stderr.strip()[:500]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception as error:
        raise RuntimeError(f"sentinel_sdk_runner_bad_json: {error}") from error
    return data if isinstance(data, dict) else {}


def _token_has_error_payload(token_value: str) -> bool:
    if not token_value:
        return True
    if SENTINEL_ERROR_PREFIX in token_value:
        return True
    try:
        data = json.loads(token_value)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return any(SENTINEL_ERROR_PREFIX in str(data.get(key) or "") for key in ("p", "so", "t"))


def _sentinel_req_url(sdk_url: str) -> str:
    parsed = urlparse(sdk_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://sentinel.openai.com"
    return f"{origin}/backend-api/sentinel/req"


def _post_sentinel_req(
    session: "Session",
    req_url: str,
    req_body: str,
    *,
    user_agent: str,
    sec_ch_ua: str,
) -> tuple[int, dict]:
    parsed = urlparse(req_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": origin,
        "Referer": f"{origin}/backend-api/sentinel/frame.html",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        resp = session.post(req_url, data=req_body, headers=headers, timeout=30, verify=False)
        status = int(getattr(resp, "status_code", 0) or 0)
        data = resp.json() if getattr(resp, "text", "") else {}
        if status == 200 and isinstance(data, dict):
            return status, data
    except Exception:
        pass

    req = urllib.request.Request(req_url, data=req_body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status = int(getattr(response, "status", 0) or 0)
            text = response.read().decode("utf-8", errors="replace")
    except Exception as error:
        raise RuntimeError(f"sentinel_req_failed: {error}") from error
    try:
        data = json.loads(text) if text else {}
    except Exception as error:
        raise RuntimeError(f"sentinel_req_bad_json_{status}: {str(error)[:200]}") from error
    return status, data if isinstance(data, dict) else {}


def build_sentinel_headers_with_sdk(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    observer_wait_ms: int = 5000,
) -> SentinelHeaders:
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    sdk_url, sdk_source, sdk_version = _discover_sdk(session, ua, ch_ua)

    req_body = ""
    for attempt in range(SDK_REQ_CAPTURE_ATTEMPTS):
        capture = _run_sdk(
            sdk_source=sdk_source,
            sdk_url=sdk_url,
            flow=flow,
            device_id=device_id,
            user_agent=ua,
        )
        req_body = str(capture.get("capturedBody") or "").strip()
        if req_body:
            break
        if attempt + 1 < SDK_REQ_CAPTURE_ATTEMPTS:
            time.sleep(SDK_REQ_CAPTURE_RETRY_DELAY_SECONDS)
    if not req_body:
        raise RuntimeError("sentinel_sdk_missing_req_body")
    if SENTINEL_ERROR_PREFIX in req_body:
        raise RuntimeError("sentinel_sdk_generated_error_requirements_token")

    req_url = _sentinel_req_url(sdk_url)
    status, req_data = _post_sentinel_req(session, req_url, req_body, user_agent=ua, sec_ch_ua=ch_ua)
    if status != 200 or not str(req_data.get("token") or "").strip():
        raise RuntimeError(f"sentinel_req_failed_{status}")

    final = _run_sdk(
        sdk_source=sdk_source,
        sdk_url=sdk_url,
        flow=flow,
        device_id=device_id,
        user_agent=ua,
        req_data=req_data,
        wait_ms=max(0, int(observer_wait_ms)),
    )
    sentinel_token = str(final.get("token") or "").strip()
    so_token = str(final.get("soToken") or "").strip()
    if _token_has_error_payload(sentinel_token):
        raise RuntimeError("sentinel_sdk_generated_error_token")
    if so_token and _token_has_error_payload(so_token):
        raise RuntimeError("sentinel_sdk_generated_error_so_token")
    return SentinelHeaders(
        sentinel_token=sentinel_token,
        so_token=so_token,
        sdk_version=str(final.get("version") or sdk_version or ""),
        sdk_url=sdk_url,
        req_url=req_url,
        req_has_so=isinstance(req_data.get("so"), dict) and bool((req_data.get("so") or {}).get("required")),
    )


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(device_id, ua)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    sentinel_value = json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))
    # oai-sc cookie = "0" + sentinel token "c" value (the challenge token from the server)
    oai_sc_value = "0" + token
    return sentinel_value, oai_sc_value
