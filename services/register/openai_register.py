from __future__ import annotations

import json
import random
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.proxy_service import ClearanceBundle, proxy_settings
from services.register import mail_provider
from utils.pkce import generate_pkce as _generate_pkce
from utils.sentinel import (
    build_sentinel_headers_with_sdk as _build_sentinel_headers_with_sdk,
    build_sentinel_token as _build_sentinel_token_tuple,
)

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "api_use_register_proxy": False,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
chatgpt_base = "https://chatgpt.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None


class RegistrationStopped(RuntimeError):
    pass

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


mail_provider.provider_log_sink = lambda text: log(text, "yellow")


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _truthy(value: object, fallback: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _mail_config(register_proxy: str = "") -> dict:
    mail = config["mail"] if isinstance(config.get("mail"), dict) else {}
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), False)
    proxy = str(register_proxy or "").strip() if use_register_proxy else ""
    return {**mail, "api_use_register_proxy": use_register_proxy, "proxy": proxy}


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def create_mailbox(username: str | None = None, register_proxy: str = "") -> dict:
    return mail_provider.create_mailbox(_mail_config(register_proxy), username)


def wait_for_code(
    mailbox: dict,
    register_proxy: str = "",
    stop_event: threading.Event | None = None,
) -> str | None:
    return mail_provider.wait_for_code(_mail_config(register_proxy), mailbox, stop_event=stop_event)


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    sentinel_val, oai_sc_value = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
    )
    if oai_sc_value:
        for domain in (".auth.openai.com", "auth.openai.com"):
            try:
                session.cookies.set("oai-sc", oai_sc_value, domain=domain)
            except Exception:
                continue
    return sentinel_val


def build_sentinel_headers_with_sdk(session: requests.Session, device_id: str, flow: str):
    return _build_sentinel_headers_with_sdk(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        observer_wait_ms=5000,
    )


def create_session(proxy: str = "") -> Any:
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate="chrome",
        verify=False,
    )
    return requests.Session(**kwargs)


def _apply_clearance_to_session(session: requests.Session, bundle: ClearanceBundle | None) -> None:
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["User-Agent"] = bundle.user_agent
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
            session.cookies.set(name, value, domain=bundle.target_host or "auth.openai.com")
        except Exception:
            continue


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    merged = proxy_settings.build_headers(
        headers=headers,
        target_url=target_url,
        proxy=proxy,
        upstream=True,
    )
    normalized = {str(key): str(value) for key, value in merged.items()}
    if user_agent_override:
        ua_key = next((key for key in normalized if key.lower() == "user-agent"), "user-agent")
        normalized[ua_key] = user_agent_override
    return normalized


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def _otp_error_code(resp) -> str:
    data = _response_json(resp) if resp is not None else {}
    error = data.get("error") if isinstance(data, dict) else None
    return str(error.get("code") or "").strip() if isinstance(error, dict) else ""


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    if _otp_error_code(resp) in {"wrong_email_otp_code", "email_otp_invalid", "invalid_code"}:
        return resp, error
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def _extract_continue_url(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    direct = str(data.get("continue_url") or data.get("continueUrl") or "").strip()
    if direct:
        return direct
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    return str(payload.get("continue_url") or payload.get("continueUrl") or "").strip()


def _url_path(url: str) -> str:
    try:
        return urlparse(str(url or "").strip()).path.rstrip("/") or "/"
    except Exception:
        return ""


def request_platform_oauth_token(session: requests.Session, code: str, code_verifier: str) -> dict | None:
    headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": auth_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{auth_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": user_agent,
    }
    resp = session.post(
        f"{auth_base}/api/accounts/oauth/token",
        headers=headers,
        data={
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=False,
        timeout=60,
    )
    if resp.status_code != 200:
        print(resp.text)
        return None
    return _response_json(resp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "", stop_event: threading.Event | None = None) -> None:
        self.proxy = str(proxy or "").strip()
        self.stop_event = stop_event
        self.session = create_session(self.proxy)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.chatgpt_callback_url = ""
        self.authorize_sentinel_token = ""
        self.password_sentinel_token = ""
        self.signup_verification_mode = ""

    def close(self) -> None:
        self.session.close()

    def _ensure_active(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RegistrationStopped("registration_run_stopped")

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _otp_fetch_headers(self) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-US,en;q=0.9",
            # curl_cffi otherwise labels an empty POST as form-urlencoded;
            # the browser omits this header, but the API rejects curl's default.
            "content-type": "application/json",
            "origin": auth_base,
            "priority": "u=1, i",
            "referer": f"{auth_base}/email-verification",
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-arch": '"x86_64"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        }
        headers.update(_make_trace_headers())
        return headers

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> ClearanceBundle | None:
        self.clearance_failure_reason = ""
        profile = proxy_settings.get_profile(proxy=self.proxy, upstream=True) if hasattr(proxy_settings, "get_profile") else None
        if profile is not None and not profile.clearance_enabled:
            self.clearance_failure_reason = (
                "可尝试使用 FlareSolverr 清障方式，注意需要 Docker 部署 flaresolverr、privoxy、warp-proxy 等相关容器"
            )
            step(index, f"检测到 Cloudflare 拦截，{self.clearance_failure_reason}", "yellow")
            return None
        step(index, "检测到 Cloudflare 拦截，尝试刷新 clearance", "yellow")
        bundle = proxy_settings.refresh_clearance(
            target_url=target_url,
            proxy=self.proxy,
            force=True,
            upstream=True,
        )
        if bundle is not None:
            _apply_clearance_to_session(self.session, bundle)
            self.clearance_user_agent = bundle.user_agent or self.clearance_user_agent
            step(index, "Cloudflare clearance 刷新完成，重试当前请求", "yellow")
        else:
            self.clearance_failure_reason = "clearance 刷新未返回可用 Cookie，请检查 FlareSolverr URL、代理和出口 IP"
            step(index, f"Cloudflare clearance 刷新失败：{self.clearance_failure_reason}", "yellow")
        return bundle

    def _boot_chatgpt_session(self, index: int) -> None:
        step(index, "开始初始化 ChatGPT 会话")
        headers = _headers_with_clearance(self._navigate_headers(), chatgpt_base, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "get",
            chatgpt_base,
            headers=headers,
            allow_redirects=True,
            verify=False,
        )
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(chatgpt_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(), chatgpt_base, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(
                self.session,
                "get",
                chatgpt_base,
                headers=headers,
                allow_redirects=True,
                verify=False,
            )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_boot_http_{getattr(resp, 'status_code', 'unknown')}")
        cookies = self.session.cookies.get_dict()
        self.device_id = str(cookies.get("oai-did") or self.device_id)
        step(index, "ChatGPT 会话初始化完成")

    def _chatgpt_authorize(self, email: str, index: int) -> None:
        self._boot_chatgpt_session(index)
        csrf_url = f"{chatgpt_base}/api/auth/csrf"
        csrf_browser_headers = {
            "accept": "application/json",
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        }
        csrf_headers = _headers_with_clearance(
            csrf_browser_headers,
            csrf_url,
            self.proxy,
            self.clearance_user_agent,
        )
        csrf_resp, error = request_with_local_retry(
            self.session,
            "get",
            csrf_url,
            headers=csrf_headers,
            verify=False,
        )
        csrf_data = _response_json(csrf_resp) if csrf_resp is not None else {}
        csrf_token = str(csrf_data.get("csrfToken") or "").strip()
        if csrf_resp is None or csrf_resp.status_code != 200 or not csrf_token:
            raise RuntimeError(error or f"chatgpt_csrf_http_{getattr(csrf_resp, 'status_code', 'unknown')}")

        query = urlencode({
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "0111",
            "screen_hint": "login_or_signup",
        })
        signin_url = f"{chatgpt_base}/api/auth/signin/openai?{query}"
        signin_headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": chatgpt_base,
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        }
        signin_headers = _headers_with_clearance(signin_headers, signin_url, self.proxy, self.clearance_user_agent)
        signin_resp, error = request_with_local_retry(
            self.session,
            "post",
            signin_url,
            data={"callbackUrl": f"{chatgpt_base}/", "csrfToken": csrf_token, "json": "true"},
            headers=signin_headers,
            allow_redirects=False,
            verify=False,
        )
        signin_data = _response_json(signin_resp) if signin_resp is not None else {}
        authorize_url = str(signin_data.get("url") or "").strip()
        if signin_resp is None or signin_resp.status_code != 200 or not authorize_url:
            raise RuntimeError(error or f"chatgpt_signin_http_{getattr(signin_resp, 'status_code', 'unknown')}")

        def authorize():
            authorize_headers = _headers_with_clearance(
                self._navigate_headers(f"{chatgpt_base}/auth/login"),
                authorize_url,
                self.proxy,
                self.clearance_user_agent,
            )
            return request_with_local_retry(
                self.session,
                "get",
                authorize_url,
                headers=authorize_headers,
                allow_redirects=True,
                verify=False,
            )

        resp, error = authorize()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = authorize()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_authorize_http_{getattr(resp, 'status_code', 'unknown')}")
        parsed_authorize = urlparse(authorize_url)
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did", "") or "").strip()
        except Exception:
            cookie_device_id = ""
        query_device_id = str((parse_qs(parsed_authorize.query).get("device_id") or [""])[0]).strip()
        self.device_id = cookie_device_id or query_device_id or self.device_id
        for domain in (".auth.openai.com", "auth.openai.com"):
            try:
                self.session.cookies.set("oai-did", self.device_id, domain=domain)
            except Exception:
                continue
        step(index, f"ChatGPT authorize 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    @staticmethod
    def _oauth_code_from_response(resp) -> str:
        candidates: list[str] = []
        for item in [*(getattr(resp, "history", None) or []), resp]:
            candidates.append(str(getattr(item, "url", "") or ""))
            location = str((getattr(item, "headers", {}) or {}).get("location") or "")
            if location:
                candidates.append(location)
        for candidate in reversed(candidates):
            params = extract_oauth_callback_params_from_url(candidate)
            if params and params.get("code"):
                return str(params["code"])
        return ""

    def _platform_authorize(self, email: str, index: int, *, screen_hint: str = "signup") -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 注册流程显式声明 signup：throwaway 域名 OpenAI 会自动当新账号走注册，
            # 但 @outlook.com/@hotmail.com 这类真实消费邮箱会被 login_or_signup 路由到登录分支，
            # 后续 user/register 落在错误的 auth step 上报 invalid_auth_step。
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        self.platform_auth_code = self._oauth_code_from_response(resp)
        if not self.platform_auth_code:
            body = str(getattr(resp, "text", "") or "")
            match = re.search(r"[?&]code=([A-Za-z0-9._~+/\-]+)", body) or re.search(
                r'"code"\s*:\s*"([^"]+)"', body
            )
            if match:
                self.platform_auth_code = str(match.group(1) or "").strip()
        if screen_hint == "login" and not self.platform_auth_code:
            raise RuntimeError("platform_authorize_missing_code")
        # 仅打日志，不据此中断：authorize 落地页无法可靠区分注册/登录，
        # 真正的判定交给 user/register（失败会 dump 完整响应）。
        step(index, f"platform authorize 完成[{landed or '?'}] url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _finish_chatgpt_registration(self, index: int) -> dict[str, str]:
        callback_url = str(self.chatgpt_callback_url or "").strip()
        if not callback_url:
            raise RuntimeError("chatgpt_callback_url_missing")
        headers = _headers_with_clearance(
            self._navigate_headers(f"{auth_base}/about-you"),
            callback_url,
            self.proxy,
            self.clearance_user_agent,
        )
        resp, error = request_with_local_retry(
            self.session,
            "get",
            callback_url,
            headers=headers,
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"chatgpt_callback_http_{getattr(resp, 'status_code', 'unknown')}")

        session_url = f"{chatgpt_base}/api/auth/session"
        session_headers = {
            "accept": "application/json",
            "referer": f"{chatgpt_base}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        }
        session_headers = _headers_with_clearance(session_headers, session_url, self.proxy, self.clearance_user_agent)
        session_resp, error = request_with_local_retry(
            self.session,
            "get",
            session_url,
            headers=session_headers,
            verify=False,
        )
        data = _response_json(session_resp) if session_resp is not None else {}
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if session_resp is None or session_resp.status_code != 200 or not access_token:
            raise RuntimeError(error or f"chatgpt_session_http_{getattr(session_resp, 'status_code', 'unknown')}")
        cookies = self.session.cookies.get_dict()
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)
        step(index, f"ChatGPT session token 获取完成 token_len={len(access_token)} cookie_count={len(cookies)}")
        return {
            "access_token": access_token,
            "session_token": str(data.get("sessionToken") or data.get("session_token") or "").strip(),
            "cookie": cookie_header,
        }

    def _follow_authorize_continue(self, continue_url: str, referer: str, index: int) -> None:
        target_url = str(continue_url or "").strip()
        if not target_url:
            return
        if target_url.startswith("/"):
            target_url = f"{auth_base}{target_url}"
        step(index, "开始 authorize continue")
        headers = _headers_with_clearance(self._navigate_headers(referer), target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(referer), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            debug = _response_debug_detail(resp)
            raise RuntimeError(error or f"authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, {debug}")
        step(index, f"authorize continue 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _authorize_signup(self, email: str, index: int) -> tuple[str, str]:
        self._ensure_active()
        step(index, "提交 ChatGPT 注册邮箱")
        url = f"{auth_base}/api/accounts/authorize/continue"

        def submit():
            sentinel_token = build_sentinel_token(self.session, self.device_id, "authorize_continue")
            self.authorize_sentinel_token = sentinel_token
            headers = self._json_headers(f"{auth_base}/create-account")
            headers["openai-sentinel-token"] = sentinel_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"value": email, "kind": "email"}, "screen_hint": "signup"},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = submit()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = submit()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"authorize_signup_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )

        data = _response_json(resp)
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        page_type = str(page.get("type") or "").strip()
        continue_url = _extract_continue_url(data)
        page_mode = {
            "create_account_password": "password",
            "email_otp_verification": "otp",
        }.get(page_type)
        continue_mode = {
            "/create-account/password": "password",
            "/email-verification": "otp",
        }.get(_url_path(continue_url))
        if page_mode and continue_mode and page_mode != continue_mode:
            raise RuntimeError(
                f"authorize_signup_state_conflict: page_type={page_type}, continue_path={_url_path(continue_url)}"
            )
        mode = page_mode or continue_mode
        if mode not in {"password", "otp"}:
            raise RuntimeError(
                f"authorize_signup_unknown_state: page_type={page_type or '?'}, "
                f"continue_path={_url_path(continue_url) or '?'}"
            )

        verification_mode = str(payload.get("email_verification_mode") or "").strip().lower()
        self.signup_verification_mode = verification_mode
        if mode == "password":
            self._follow_authorize_continue(
                continue_url or f"{auth_base}/create-account/password",
                f"{auth_base}/create-account",
                index,
            )
        step(
            index,
            f"ChatGPT 注册邮箱状态 mode={mode}, verification_mode={verification_mode or 'none'}",
        )
        return mode, verification_mode

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        self.password_sentinel_token = build_sentinel_token(
            self.session,
            self.device_id,
            "username_password_create",
        )
        headers["openai-sentinel-token"] = self.password_sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            self.password_sentinel_token = build_sentinel_token(
                self.session,
                self.device_id,
                "username_password_create",
            )
            headers["openai-sentinel-token"] = self.password_sentinel_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        self._follow_authorize_continue(str(data.get("continue_url") or "").strip(), f"{auth_base}/create-account/password", index)
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int, mailbox: dict[str, Any] | None = None) -> None:
        self._ensure_active()
        step(index, "开始发送验证码")
        if mailbox is not None:
            try:
                mail_provider.prepare_code_baseline(_mail_config(self.proxy), mailbox)
                step(index, "发送验证码前邮箱基线已记录")
            except Exception as exc:
                step(index, f"邮箱基线记录失败，继续发送验证码: {str(exc)[:160]}", "yellow")
        url = f"{auth_base}/api/accounts/email-otp/send"
        if not self.password_sentinel_token:
            raise RuntimeError("send_otp_missing_password_sentinel")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = self.password_sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = self.password_sentinel_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _resend_signup_otp(self, index: int, mailbox: dict[str, Any]) -> None:
        self._ensure_active()
        try:
            mail_provider.prepare_code_baseline(_mail_config(self.proxy), mailbox)
            step(index, "重发验证码前邮箱基线已记录")
        except Exception as exc:
            step(index, f"邮箱基线记录失败，继续重发验证码: {str(exc)[:160]}", "yellow")

        step(index, "开始重发注册验证码")
        url = f"{auth_base}/api/accounts/email-otp/resend"
        try:
            cookie_count = len(self.session.cookies.get_dict())
        except Exception:
            cookie_count = 0
        step(
            index,
            f"OTP resend browser headers: cookie_count={cookie_count}, sentinel_header=false, device_header=false",
        )

        def submit():
            headers = self._otp_fetch_headers()
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                retry_attempts=1,
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = submit()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = submit()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"resend_otp_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )

        data = _response_json(resp)
        error_data = data.get("error") if isinstance(data, dict) else None
        if isinstance(error_data, dict):
            error_message = str(error_data.get("message") or error_data.get("code") or "").strip()
        else:
            error_message = str(error_data or "").strip()
        if not error_message and data.get("success") is False:
            error_message = str(data.get("message") or "unknown_error").strip()
        if error_message:
            raise RuntimeError(f"resend_otp_rejected: {error_message}")
        step(index, "重发注册验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, "开始校验验证码")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")

    def _validate_mailbox_otp(self, mailbox: dict[str, Any], index: int) -> None:
        max_attempts = 4
        last_detail = ""
        for attempt in range(1, max_attempts + 1):
            self._ensure_active()
            step(index, f"开始等待注册验证码（第 {attempt}/{max_attempts} 次）")
            code = wait_for_code(mailbox, register_proxy=self.proxy, stop_event=self.stop_event)
            self._ensure_active()
            if not code:
                if attempt >= max_attempts:
                    raise RuntimeError(last_detail or "等待注册验证码超时")
                step(index, f"第 {attempt}/{max_attempts} 次等待未收到验证码，继续等待", "yellow")
                continue
            mail_provider.mark_verification_code_received(mailbox)
            step(index, "收到注册验证码")
            step(index, "开始校验验证码")
            resp, error = validate_otp(self.session, self.device_id, code)
            if resp is not None and resp.status_code == 200:
                step(index, "验证码校验完成")
                return

            error_code = _otp_error_code(resp)
            body = ""
            try:
                body = str(resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            last_detail = error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}"
            retryable = error_code in {"wrong_email_otp_code", "email_otp_invalid", "invalid_code"}
            if not retryable or attempt >= max_attempts:
                raise RuntimeError(last_detail)

            mail_provider.mark_verification_code_rejected(mailbox, code)
            step(index, f"验证码被上游拒绝({error_code})，忽略该验证码并等待更新邮件", "yellow")
            time.sleep(1.5)

        raise RuntimeError(last_detail or "验证码校验失败")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")
        sentinel_headers = build_sentinel_headers_with_sdk(self.session, self.device_id, "oauth_create_account")
        if not sentinel_headers.so_token:
            raise RuntimeError(f"create_account_missing_sentinel_so_token: {sentinel_headers.log_summary()}")
        headers.update(sentinel_headers.as_headers())
        step(index, f"create_account sentinel: {json.dumps(sentinel_headers.log_summary(), ensure_ascii=False)}")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")
            sentinel_headers = build_sentinel_headers_with_sdk(self.session, self.device_id, "oauth_create_account")
            if not sentinel_headers.so_token:
                raise RuntimeError(f"create_account_missing_sentinel_so_token: {sentinel_headers.log_summary()}")
            headers.update(sentinel_headers.as_headers())
            step(index, f"create_account sentinel retry: {json.dumps(sentinel_headers.log_summary(), ensure_ascii=False)}")
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        self.chatgpt_callback_url = str(data.get("continue_url") or "").strip()
        callback_params = extract_oauth_callback_params_from_url(self.chatgpt_callback_url)
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        step(index, "创建账号资料完成")

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        self._ensure_active()
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(register_proxy=self.proxy)
        self._ensure_active()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password = ""
            first_name, last_name = _random_name()
            self._ensure_active()
            self._chatgpt_authorize(email, index)
            self._ensure_active()
            signup_mode, verification_mode = self._authorize_signup(email, index)
            if signup_mode == "password":
                password = _random_password()
                self._ensure_active()
                self._register_user(email, password, index)
                self._ensure_active()
                self._send_otp(index, mailbox)
            else:
                if verification_mode == "passwordless_login":
                    raise RuntimeError("signup_email_already_registered")
                if verification_mode != "passwordless_signup":
                    raise RuntimeError(
                        "signup_otp_mode_unconfirmed: "
                        f"email_verification_mode={verification_mode or 'unknown'}"
                    )
                self._resend_signup_otp(index, mailbox)
            self._validate_mailbox_otp(mailbox, index)
            self._ensure_active()
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            self._ensure_active()
            chatgpt_session = self._finish_chatgpt_registration(index)
            tokens: dict = {}
            try:
                self._platform_authorize(email, index, screen_hint="login")
                tokens = self._exchange_registered_tokens(index)
            except Exception as oauth_error:
                # The ChatGPT session is already usable. Keep the account when
                # the optional Platform OAuth refresh-token flow is intermittent.
                step(
                    index,
                    f"Platform OAuth refresh token 获取失败，保留 ChatGPT session 账号: {str(oauth_error)[:240]}",
                    "yellow",
                )
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            "access_token": str(chatgpt_session.get("access_token") or "").strip(),
            "platform_access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "session_token": str(chatgpt_session.get("session_token") or "").strip(),
            "cookie": str(chatgpt_session.get("cookie") or "").strip(),
            "source_type": "web",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int, stop_event: threading.Event | None = None, generation: int = 0) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"], stop_event=stop_event)
    try:
        step(index, f"任务启动 generation={generation}")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_account_items([result])
        refresh_result = account_service.refresh_accounts([access_token])
        if refresh_result.get("errors"):
            step(index, f"账号已保存，刷新状态暂未成功，稍后可重试: {refresh_result['errors']}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "generation": generation, "result": result}
    except RegistrationStopped:
        cost = time.time() - start
        step(index, f"任务因运行代次停止，本次耗时{cost:.1f}s", "yellow")
        return {"ok": False, "cancelled": True, "index": index, "generation": generation}
    except Exception as e:
        cost = time.time() - start
        if stop_event is not None and stop_event.is_set():
            step(index, f"任务因运行代次停止，本次耗时{cost:.1f}s", "yellow")
            return {"ok": False, "cancelled": True, "index": index, "generation": generation}
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "generation": generation, "error": str(e)}
    finally:
        registrar.close()
