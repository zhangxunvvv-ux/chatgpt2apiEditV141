from __future__ import annotations

import hashlib
import imaplib
import json
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, message_from_string, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from threading import Event, Lock
from typing import Any, Callable, TypeVar
from curl_cffi import requests


from services.config import DATA_DIR

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()

OUTLOOK_TOKEN_USED_FILE = DATA_DIR / "outlook_token_used.json"
_outlook_token_state_lock = Lock()
# in_use 超过该秒数视为陈旧（注册进程崩溃残留），可被重新领用
OUTLOOK_IN_USE_STALE_SECONDS = 3600
OUTLOOK_RECORDED_STATES = {"used", "in_use", "token_invalid", "failed"}
OUTLOOK_UNAVAILABLE_STATES = {"used", "token_invalid", "failed"}


def _load_ddg_aliases() -> set[str]:
    try:
        if DDG_ALIASES_FILE.exists():
            data = json.loads(DDG_ALIASES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(item).strip().lower() for item in data if str(item).strip()}
    except Exception:
        pass
    return set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    DDG_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DDG_ALIASES_FILE.write_text(json.dumps(sorted(aliases), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


def _load_outlook_token_state() -> dict[str, dict[str, Any]]:
    """读取邮箱池状态文件，返回 {email_lower: {state, reason, updated_at}}。

    兼容旧格式：纯字符串列表（历史的“已用邮箱”）会被解释为 used。
    """
    try:
        if not OUTLOOK_TOKEN_USED_FILE.exists():
            return {}
        data = json.loads(OUTLOOK_TOKEN_USED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    state: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            key = str(item).strip().lower()
            if key:
                state[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            email = str(key).strip().lower()
            if not email:
                continue
            if isinstance(value, dict):
                state[email] = {
                    "state": str(value.get("state") or "used").strip() or "used",
                    "reason": str(value.get("reason") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                }
            else:
                state[email] = {"state": str(value or "used").strip() or "used", "reason": "", "updated_at": ""}
    return state


def _save_outlook_token_state(state: dict[str, dict[str, Any]]) -> None:
    OUTLOOK_TOKEN_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: state[key] for key in sorted(state)}
    OUTLOOK_TOKEN_USED_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _outlook_entry_available(entry: dict[str, Any] | None) -> bool:
    """该邮箱当前是否可领用：未记录、或 in_use 已陈旧、或非终态时可用。"""
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if current == "in_use":
        updated_at = str(entry.get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds()
            return age >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def _set_outlook_token_state(address: str, state: str, reason: str = "") -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        store[target] = {"state": str(state), "reason": str(reason or ""), "updated_at": datetime.now(timezone.utc).isoformat()}
        _save_outlook_token_state(store)


def _release_outlook_token_state(address: str) -> None:
    """把 in_use 释放回未使用（仅当当前确实是 in_use 时）。"""
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        entry = store.get(target)
        if isinstance(entry, dict) and str(entry.get("state") or "") == "in_use":
            store.pop(target, None)
            _save_outlook_token_state(store)


def reset_outlook_token_pool_state(scope: str = "all") -> int:
    """重置邮箱池状态文件。

    scope=all 清空所有记录；scope=failed 仅清除 failed/token_invalid/in_use（保留 used）。
    返回被清除的条目数。
    """
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        if not store:
            return 0
        if str(scope) == "failed":
            remove = {key for key, value in store.items() if str(value.get("state") or "") in {"failed", "token_invalid", "in_use"}}
            for key in remove:
                store.pop(key, None)
            _save_outlook_token_state(store)
            return len(remove)
        count = len(store)
        _save_outlook_token_state({})
        return count


def prune_outlook_unused_credentials(credentials: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Return credentials with recorded state, plus the number pruned as unused."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
    kept: list[dict[str, str]] = []
    removed = 0
    for credential in credentials:
        key = str(credential.get("email") or "").strip().lower()
        entry = store.get(key) if key else None
        state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
        if state in OUTLOOK_RECORDED_STATES:
            kept.append(credential)
        else:
            removed += 1
    return kept, removed


def outlook_token_pool_stats(pool: list[dict[str, str]] | None = None) -> dict[str, int]:
    """统计邮箱池各状态数量。pool 为该 provider 当前导入的邮箱列表（用于算 unused）。"""
    store = _load_outlook_token_state()
    counts = {"unused": 0, "in_use": 0, "used": 0, "token_invalid": 0, "failed": 0}
    if pool:
        for credential in pool:
            entry = store.get(str(credential.get("email") or "").strip().lower())
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
            else:
                counts["unused"] += 1
    else:
        for entry in store.values():
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
    return counts


ResultT = TypeVar("ResultT")
domain_lock = Lock()
provider_lock = Lock()
domain_index = 0
provider_index = 0
cloudmail_token_lock = Lock()
cloudmail_token_cache: dict[str, tuple[str, float]] = {}
fixed_mailbox_lock = Lock()
fixed_mailbox_reservations: set[str] = set()
provider_log_sink: Callable[[str], None] | None = None


def _provider_log(message: str) -> None:
    if provider_log_sink is not None:
        try:
            provider_log_sink(message)
            return
        except Exception:
            pass
    print(message)


def _config(mail_config: dict) -> dict:
    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 30),
        "wait_interval": float(mail_config.get("wait_interval") or 2),
        "user_agent": str(mail_config.get("user_agent") or "Mozilla/5.0"),
        "proxy": str(mail_config.get("proxy") or "").strip(),
    }


def _random_mailbox_name() -> str:
    return f"{''.join(random.choices(string.ascii_lowercase, k=5))}{''.join(random.choices(string.digits, k=random.randint(1, 3)))}{''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))}"


def _random_subdomain_label() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 10)))


def _random_subdomain_suffix() -> str:
    chars = [random.choice(string.ascii_lowercase), random.choice(string.digits)]
    chars.extend(random.choices(string.ascii_lowercase + string.digits, k=3))
    random.shuffle(chars)
    return "".join(chars)


def _next_domain(domains: list[str]) -> str:
    global domain_index
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    if len(domains) == 1:
        return domains[0]
    with domain_lock:
        value = domains[domain_index % len(domains)]
        domain_index = (domain_index + 1) % len(domains)
        return value


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_dns_name(value: Any, field: str) -> str:
    name = str(value or "").strip().lower().lstrip("@").strip(".")
    if not name:
        raise RuntimeError(f"{field} 不能为空")
    try:
        ascii_name = name.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise RuntimeError(f"{field} 格式无效: {name}") from error
    labels = ascii_name.split(".")
    if len(ascii_name) > 253 or any(
        len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise RuntimeError(f"{field} 格式无效: {name}")
    return ascii_name


def _normalize_full_email(value: Any, field: str) -> str:
    candidate = str(value or "").strip()
    local_part, separator, raw_domain = candidate.rpartition("@")
    if not separator or not local_part or "@" in local_part:
        raise RuntimeError(f"{field} 格式无效")
    if (
        len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local_part)
    ):
        raise RuntimeError(f"{field} 格式无效")
    domain = _normalize_dns_name(raw_domain, f"{field}域名")
    return f"{local_part}@{domain}"


def _reserve_fixed_mailbox(key: str) -> bool:
    with fixed_mailbox_lock:
        if key in fixed_mailbox_reservations:
            return False
        fixed_mailbox_reservations.add(key)
        return True


def _release_fixed_mailbox(mailbox: dict[str, Any]) -> None:
    key = str(mailbox.pop("_fixed_mailbox_reservation", "") or "").strip()
    if not key:
        return
    with fixed_mailbox_lock:
        fixed_mailbox_reservations.discard(key)


def _create_session(conf: dict):
    proxy = str(conf.get("proxy") or "").strip()
    kwargs = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return requests.Session(**kwargs)


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(data: dict[str, Any]) -> tuple[str, str]:
    text_content = str(data.get("text_content") or data.get("text") or data.get("body") or data.get("content") or "")
    html_content = str(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html") or "")
    if text_content or html_content:
        return text_content, html_content
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            html.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                out.extend(_extract_text_candidates(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_text_candidates(item))
        return out
    return []


def _message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in ("to", "toEmail", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    return not target or not candidates or any(target in str(item).strip().lower() for item in candidates if str(item).strip())


def _extract_code(message: dict[str, Any]) -> str | None:
    content = f"{message.get('subject', '')}\n{message.get('text_content', '')}\n{message.get('html_content', '')}".strip()
    raw = message.get("raw")
    if raw:
        try:
            raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        except Exception:
            raw_text = str(raw)
        if raw_text not in content:
            content = f"{content}\n{raw_text}".strip()
    if not content:
        return None
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    contextual_patterns = (
        r"(?:verification|security|login|sign[ -]?in|one[ -]?time|chatgpt|otp|code|\u9a8c\u8bc1\u7801|\u6821\u9a8c\u7801)[^0-9]{0,100}((?:\d[\s-]?){6})",
        r"((?:\d[\s-]?){6})[^A-Za-z0-9]{0,80}(?:is\s+(?:your\s+)?(?:verification\s+)?code|\u9a8c\u8bc1\u7801)",
    )
    for pattern in contextual_patterns:
        match = re.search(pattern, content, re.I)
        if match:
            value = re.sub(r"\D", "", match.group(1))
            if len(value) == 6 and value != "177010":
                return value
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


def _message_tracking_ref(message: dict[str, Any]) -> str:
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"
    received_at = message.get("received_at")
    received_value = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    content = "\n".join(str(message.get(key) or "") for key in ("subject", "sender", "text_content", "html_content"))
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


def _message_before_code_boundary(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    boundary = mailbox.get("_code_not_before")
    received_at = message.get("received_at")
    if not isinstance(boundary, datetime) or not isinstance(received_at, datetime):
        return False
    if not received_at.tzinfo:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at < boundary


def _rejected_code_set(mailbox: dict[str, Any]) -> set[str]:
    values = mailbox.setdefault("_rejected_verification_codes", [])
    if not isinstance(values, list):
        values = []
        mailbox["_rejected_verification_codes"] = values
    return {str(value) for value in values if str(value)}


def mark_verification_code_rejected(mailbox: dict[str, Any], code: str) -> None:
    normalized = re.sub(r"\D", "", str(code or ""))[:6]
    if not normalized:
        return
    values = mailbox.setdefault("_rejected_verification_codes", [])
    if not isinstance(values, list):
        values = []
        mailbox["_rejected_verification_codes"] = values
    if normalized not in values:
        values.append(normalized)


def _verification_code_rejected(mailbox: dict[str, Any], code: str | None) -> bool:
    return bool(code and code in _rejected_code_set(mailbox))


class BaseMailProvider:
    name = "unknown"

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref
        self.stop_event: Event | None = None

    def _stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _poll_wait(self) -> bool:
        seconds = max(0.2, self.conf["wait_interval"])
        if self.stop_event is not None:
            return self.stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def wait_for(self, mailbox: dict[str, Any], on_message: Callable[[dict[str, Any]], ResultT | None]) -> ResultT | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline and not self._stopped():
            message = self.fetch_latest_message(mailbox)
            if message:
                result = on_message(message)
                if result is not None:
                    return result
            if self._poll_wait():
                break
        return None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        def extract_unseen_code(message: dict[str, Any]) -> str | None:
            if _message_before_code_boundary(mailbox, message):
                return None
            ref = _message_tracking_ref(message)
            if ref in seen_refs:
                return None
            code = _extract_code(message)
            if code and not _verification_code_rejected(mailbox, code):
                seen_value.append(ref)
                seen_refs.add(ref)
                return code
            return None

        return self.wait_for(mailbox, extract_unseen_code)

    def prepare_code_baseline(self, mailbox: dict[str, Any]) -> None:
        messages: list[dict[str, Any]] = []
        fetch_recent = getattr(self, "fetch_recent_messages", None)
        if callable(fetch_recent):
            recent = fetch_recent(mailbox)
            if isinstance(recent, list):
                messages.extend(message for message in recent if isinstance(message, dict))
        else:
            latest = self.fetch_latest_message(mailbox)
            if isinstance(latest, dict):
                messages.append(latest)

        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(value) for value in seen_value}
        for message in messages:
            ref = _message_tracking_ref(message)
            if ref not in seen_refs:
                seen_value.append(ref)
                seen_refs.add(ref)
            code = _extract_code(message)
            if code:
                mark_verification_code_rejected(mailbox, code)
        # Provider and host clocks can differ slightly; baseline refs/codes still
        # prevent pre-existing mail from being selected inside this skew window.
        mailbox["_code_not_before"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    def close(self) -> None:
        pass


class CloudflareTempMailProvider(BaseMailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_password = str(entry["admin_password"]).strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.subdomain_levels = _normalize_string_list(entry.get("subdomain_levels"))
        self.fixed_address = str(entry.get("fixed_address") or "").strip()
        suffix_value = entry.get("append_random_suffix", True)
        if isinstance(suffix_value, bool):
            self.append_random_suffix = suffix_value
        else:
            self.append_random_suffix = str(suffix_value).strip().lower() not in {"0", "false", "no", "off"}
        try:
            depth = int(entry.get("random_subdomain_depth") or 1)
        except (TypeError, ValueError):
            depth = 1
        self.random_subdomain_depth = max(1, min(5, depth))
        self._last_status_code: int | None = None
        self._last_raw_batch = 0
        self._last_matched_batch = 0
        self._last_response_shape = "unknown"
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        self._last_status_code = None
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"Content-Type": "application/json", "User-Agent": self.conf["user_agent"], **(headers or {})}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        self._last_status_code = int(resp.status_code)
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudflareTempMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _resolve_domain(self) -> str:
        base_domain = _normalize_dns_name(_next_domain(self.domain), "CloudflareTempMail 根域名")
        if self.subdomain_levels:
            levels = [
                _normalize_dns_name(value, f"CloudflareTempMail 第 {index} 级域名")
                for index, value in enumerate(self.subdomain_levels, start=1)
            ]
            if any("." in level for level in levels):
                raise RuntimeError("CloudflareTempMail 手动域名每一级只能填写一个标签，不能包含点号")
            if self.append_random_suffix:
                levels = [
                    _normalize_dns_name(
                        f"{level}{_random_subdomain_suffix()}",
                        f"CloudflareTempMail 第 {index} 级域名（含随机后缀）",
                    )
                    for index, level in enumerate(levels, start=1)
                ]
            return f"{'.'.join(reversed(levels))}.{base_domain}"
        if self.subdomain:
            custom = _normalize_dns_name(random.choice(self.subdomain), "CloudflareTempMail N 级域名")
            if custom == base_domain or custom.endswith(f".{base_domain}"):
                return custom
            return f"{custom}.{base_domain}"
        prefix = ".".join(_random_subdomain_label() for _ in range(self.random_subdomain_depth))
        return f"{prefix}.{base_domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if self.fixed_address:
            return self._get_or_create_fixed_mailbox()
        selected_domain = self._resolve_domain()
        data = self._request(
            "POST",
            "/admin/new_address",
            headers={"x-admin-auth": self.admin_password},
            payload={
                "enablePrefix": True,
                "name": username or _random_mailbox_name(),
                "domain": selected_domain,
            },
        )
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError("CloudflareTempMail 缺少 address 或 jwt")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "token": token,
        }

    def _get_or_create_fixed_mailbox(self) -> dict[str, Any]:
        address = _normalize_full_email(self.fixed_address, "CloudflareTempMail 指定完整邮箱")
        reservation_key = f"{self.api_base.lower()}|{address.lower()}"
        if not _reserve_fixed_mailbox(reservation_key):
            raise RuntimeError("CloudflareTempMail 指定完整邮箱正在被其他注册任务使用，请使用单线程测试")
        try:
            try:
                mailbox = self.get_existing_mailbox(address)
            except RuntimeError:
                if self._last_status_code not in (400, 404):
                    raise
                local_part, _, domain = address.rpartition("@")
                data = self._request(
                    "POST",
                    "/admin/new_address",
                    headers={"x-admin-auth": self.admin_password},
                    payload={
                        "enablePrefix": False,
                        "name": local_part,
                        "domain": domain,
                    },
                )
                returned_address = str(data.get("address") or "").strip()
                token = str(data.get("jwt") or "").strip()
                if returned_address.lower() != address.lower() or not token:
                    raise RuntimeError("CloudflareTempMail 无法按指定完整邮箱精确创建地址")
                mailbox = {
                    "provider": self.name,
                    "provider_ref": self.provider_ref,
                    "address": returned_address,
                    "token": token,
                }
            mailbox["_fixed_mailbox_reservation"] = reservation_key
            mailbox["fixed_address"] = True
            return mailbox
        except Exception as exc:
            if self._last_status_code == 400 and "address already exists" in str(exc).lower():
                try:
                    mailbox = self.get_existing_mailbox(address)
                except Exception:
                    mailbox = None
                if mailbox is not None:
                    mailbox["_fixed_mailbox_reservation"] = reservation_key
                    mailbox["fixed_address"] = True
                    return mailbox
            with fixed_mailbox_lock:
                fixed_mailbox_reservations.discard(reservation_key)
            raise

    def get_existing_mailbox(self, email: str) -> dict[str, Any]:
        """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
        query_data = self._request(
            "GET",
            "/admin/address",
            headers={"x-admin-auth": self.admin_password},
            params={"limit": 100, "offset": 0, "query": email},
        )
        results = query_data.get("results")
        if not isinstance(results, list):
            raise RuntimeError("CloudflareTempMail address lookup returned an invalid response")
        target = email.strip().lower()
        matched = next(
            (
                item
                for item in results
                if isinstance(item, dict)
                and str(item.get("name") or item.get("address") or "").strip().lower() == target
            ),
            None,
        )
        if matched is None:
            # Preserve the existing create-on-404 branch without treating API errors as absence.
            self._last_status_code = 404
            raise RuntimeError("CloudflareTempMail fixed address was not found")
        address_id = str(matched.get("id") or "").strip()
        if not address_id:
            raise RuntimeError("CloudflareTempMail existing address is missing its id")
        token_data = self._request(
            "GET",
            f"/admin/show_password/{address_id}",
            headers={"x-admin-auth": self.admin_password},
        )
        data = {"address": email, "jwt": token_data.get("jwt")}
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError(f"CloudflareTempMail 无法获取已有邮箱 {email} 的 JWT")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def _normalize_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or item.get("_id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(sender),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt")
                or item.get("created_at")
                or item.get("receivedAt")
                or item.get("date")
                or item.get("timestamp")
            ),
            "raw": item,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {mailbox['token']}"}
        data = self._request("GET", "/api/mails", headers=headers, params={"limit": 20, "offset": 0})
        raw: list[Any] = []
        response_shape = type(data).__name__
        if isinstance(data, list):
            raw = data
            response_shape = "root:list"
        elif isinstance(data, dict):
            for key in ("results", "emails", "messages", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    raw = value
                    response_shape = f"{key}:list"
                    break
                if isinstance(value, dict):
                    for nested_key in ("results", "emails", "messages", "items"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            raw = nested
                            response_shape = f"{key}.{nested_key}:list"
                            break
                    if raw:
                        break
        self._last_response_shape = response_shape
        self._last_raw_batch = len(raw)
        summaries = [item for item in raw if isinstance(item, dict) and _message_matches_email(item, str(mailbox.get("address") or ""))]
        self._last_matched_batch = len(summaries)
        return [self._normalize_message(mailbox, summary) for summary in summaries]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def prepare_code_baseline(self, mailbox: dict[str, Any]) -> None:
        # New Cloudflare inboxes do not need a pre-send read. It can race with
        # OTP delivery and classify the first valid code as an old message.
        mailbox["_code_not_before"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        deadline = time.monotonic() + self.conf["wait_timeout"]
        successful_queries = empty_inboxes = http_429 = http_5xx = other_errors = 0
        scanned = boundary_filtered = no_code = 0
        raw_messages_seen = matched_messages_seen = 0
        last_raw_batch = last_matched_batch = 0
        last_status: int | None = None

        def log_diagnostics(outcome: str) -> None:
            domain = str(mailbox.get("address") or "").partition("@")[2] or "unknown"
            if successful_queries and empty_inboxes == successful_queries and not raw_messages_seen:
                conclusion = "upstream_delivery_not_observed"
            elif raw_messages_seen and not matched_messages_seen:
                conclusion = "recipient_not_matched"
            elif matched_messages_seen and scanned and no_code:
                conclusion = "mail_present_but_code_not_recognized"
            elif matched_messages_seen and not scanned:
                conclusion = "mail_present_but_already_seen_or_filtered"
            elif http_429 or http_5xx or other_errors:
                conclusion = "provider_query_errors"
            else:
                conclusion = "code_recognized" if outcome == "命中" else "inconclusive"
            _provider_log(
                f"CloudflareTempMail 验证码轮询{outcome}: "
                f"provider_ref={self.provider_ref or self.name}, domain={domain}, "
                f"successful_queries={successful_queries}, empty_inboxes={empty_inboxes}, "
                f"http_429={http_429}, http_5xx={http_5xx}, other_errors={other_errors}, "
                f"last_status={last_status if last_status is not None else 'none'}, "
                f"response_shape={self._last_response_shape}, last_raw_batch={last_raw_batch}, "
                f"last_matched_batch={last_matched_batch}, raw_messages_seen={raw_messages_seen}, "
                f"matched_messages_seen={matched_messages_seen}, scanned={scanned}, no_code={no_code}, "
                f"boundary_filtered={boundary_filtered}, conclusion={conclusion}"
            )

        while time.monotonic() < deadline and not self._stopped():
            try:
                messages = self.fetch_recent_messages(mailbox)
                last_status = self._last_status_code
                successful_queries += 1
            except RuntimeError:
                last_status = self._last_status_code
                if last_status == 429:
                    http_429 += 1
                    log_diagnostics("HTTP 429")
                    raise
                if last_status is not None and 500 <= last_status < 600:
                    http_5xx += 1
                else:
                    other_errors += 1
                    if last_status is not None and 400 <= last_status < 500:
                        log_diagnostics("查询失败")
                        raise
                if self._poll_wait():
                    break
                continue

            last_raw_batch = self._last_raw_batch
            last_matched_batch = self._last_matched_batch
            raw_messages_seen += last_raw_batch
            matched_messages_seen += last_matched_batch
            if not last_raw_batch:
                empty_inboxes += 1
            for message in messages:
                if _message_before_code_boundary(mailbox, message) and not message.get("message_id"):
                    boundary_filtered += 1
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                scanned += 1
                code = _extract_code(message)
                if code and _verification_code_rejected(mailbox, code):
                    seen_value.append(ref)
                    seen_refs.add(ref)
                    continue
                if code:
                    seen_value.append(ref)
                    seen_refs.add(ref)
                    log_diagnostics("命中")
                    return code
                no_code += 1
            if self._poll_wait():
                break

        log_diagnostics("超时")
        return None

    def close(self) -> None:
        self.session.close()


class DDGMailProvider(BaseMailProvider):
    name = "ddg_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.ddg_token = str(entry["ddg_token"]).strip()
        self.cf_api_base = str(entry.get("api_base") or entry.get("cf_api_base") or "").rstrip("/")
        self.cf_inbox_jwt = str(entry.get("cf_inbox_jwt") or "").strip()
        self.cf_admin_password = str(entry.get("admin_password") or "").strip()
        self.cf_api_key = str(entry.get("cf_api_key") or "").strip()
        self.cf_auth_mode = str(entry.get("cf_auth_mode") or "none").strip().lower()
        self.cf_domain = entry.get("cf_domain") or []
        self.cf_create_path = str(entry.get("cf_create_path") or "/api/new_address").strip()
        self.cf_messages_path = str(entry.get("cf_messages_path") or "/api/mails").strip()
        self.session = _create_session(conf)

    def _cf_build_headers(self, content_type: bool = False) -> dict:
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.cf_api_key:
            if self.cf_auth_mode == "x-api-key":
                headers["X-API-Key"] = self.cf_api_key
            elif self.cf_auth_mode != "none":
                headers["Authorization"] = f"Bearer {self.cf_api_key}"
        return headers

    def _cf_request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict:
        merged_headers = {**self._cf_build_headers(True), **(headers or {}), "User-Agent": self.conf["user_agent"]}
        if self.cf_admin_password and method.upper() in ("POST",):
            merged_headers["x-admin-auth"] = self.cf_admin_password
        if self.cf_api_key and self.cf_auth_mode == "query-key":
            params = {**(params or {}), "key": self.cf_api_key}
        resp = self.session.request(method.upper(), f"{self.cf_api_base}{path}", headers=merged_headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DDGMail CF请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _ddg_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = self.session.request(method.upper(), f"https://quack.duckduckgo.com{path}", headers={"Authorization": f"Bearer {self.ddg_token}", "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"DDG API请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return resp.json()

    def _cf_list_payload(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "hydra:member", "data", "messages"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    return value["messages"]
        return []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        ddg_data = self._ddg_request("POST", "/api/email/addresses", payload={})
        ddg_address_part = str(ddg_data.get("address") or "").strip()
        if not ddg_address_part:
            raise RuntimeError("DDG API 返回无 address 字段")
        ddg_address = f"{ddg_address_part}@duck.com"

        if _is_ddg_alias_duplicate(ddg_address):
            raise RuntimeError(f"[{self.label}] DDG日上限已达，别名 {ddg_address} 已存在，自动切换邮箱提供商")

        _record_ddg_alias(ddg_address)

        if not self.cf_inbox_jwt:
            raise RuntimeError("DDGMail 需要 cf_inbox_jwt（DDG 转发目标的固定收件箱 JWT），请在邮箱配置中填写 CF Inbox JWT")

        return {"provider": self.name, "provider_ref": self.provider_ref, "address": ddg_address, "token": self.cf_inbox_jwt, "label": self.label}

    def _parse_raw_recipient(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        match = re.search(r"^To:\s*(.+?)$", raw_text, re.MULTILINE | re.IGNORECASE)
        if match:
            addr = match.group(1).strip()
            addr = re.sub(r"\s*<[^>]*>", "", addr)
            return addr.strip().lower()
        try:
            parsed = message_from_string(raw_text, policy=policy.default)
            return str(parsed.get("To") or "").strip().lower()
        except Exception:
            return ""

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        target_address = str(mailbox.get("address") or "").strip().lower()
        data = self._cf_request("GET", self.cf_messages_path, headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0})
        raw_list = self._cf_list_payload(data)
        messages = [item for item in raw_list if isinstance(item, dict)]
        if not messages:
            return None

        for item in messages:
            message_id = str(item.get("id") or item.get("msgid") or item.get("_id") or "")
            raw_text = str(item.get("raw") or "")
            raw_recipient = self._parse_raw_recipient(raw_text)
            if target_address and raw_recipient and target_address not in raw_recipient:
                continue
            text_content, html_content = _extract_content(item)
            subject = str(item.get("subject") or "")
            sender = item.get("from") or item.get("sender") or item.get("source") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
            if raw_text and (not subject or not sender or subject == sender == ""):
                try:
                    parsed = message_from_string(raw_text, policy=policy.default)
                    if not subject:
                        subject = str(parsed.get("Subject") or "")
                    if not sender:
                        sender = str(parsed.get("From") or "")
                except Exception:
                    pass
            return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": subject, "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

        return None

    def close(self) -> None:
        self.session.close()


class _NonRetryableCloudMailGenError(RuntimeError):
    pass


class CloudMailGenProvider(BaseMailProvider):
    name = "cloudmail_gen"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_email = str(entry.get("admin_email") or "").strip()
        self.admin_password = str(entry.get("admin_password") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.session = _create_session(conf)

    def _clear_token_cache(self) -> None:
        with cloudmail_token_lock:
            cloudmail_token_cache.pop(self._cache_key(), None)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        last_error = ""
        attempts = 3
        for attempt in range(attempts):
            try:
                resp = self.session.request(
                    method.upper(),
                    f"{self.api_base}{path}",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": self.conf["user_agent"],
                        **(headers or {}),
                    },
                    params=params,
                    json=payload,
                    timeout=self.conf["request_timeout"],
                    verify=False,
                )
                if resp.status_code in expected:
                    return {} if resp.status_code == 204 else resp.json()
                message = f"CloudMailGen 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}"
                if not self._is_retryable_status(int(resp.status_code)):
                    raise _NonRetryableCloudMailGenError(message)
                last_error = message
            except _NonRetryableCloudMailGenError as error:
                raise RuntimeError(str(error)) from error
            except Exception as error:
                last_error = f"CloudMailGen 请求异常: {method} {path}, error={error}"
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(last_error or f"CloudMailGen 请求失败: {method} {path}")

    def _cache_key(self) -> str:
        return f"{self.api_base}|{self.admin_email}"

    @staticmethod
    def _is_success_payload(data: Any) -> bool:
        return isinstance(data, dict) and data.get("code") == 200

    def _fetch_email_list(self, token: str, address: str) -> dict:
        data = self._request(
            "POST",
            "/api/public/emailList",
            headers={"Authorization": token},
            payload={"toEmail": address, "size": 20, "timeSort": "desc"},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        return data

    def _get_token(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMailGen 缺少 admin_email 或 admin_password")
        cache_key = self._cache_key()
        now = time.time()
        with cloudmail_token_lock:
            cached = cloudmail_token_cache.get(cache_key)
            if cached and now < cached[1] - 300:
                return cached[0]
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
        )
        token = ""
        if isinstance(data, dict) and data.get("code") == 200:
            token = str((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError(f"CloudMailGen genToken 返回异常: {data}")
        with cloudmail_token_lock:
            cloudmail_token_cache[cache_key] = (token, now + 24 * 3600)
        return token

    def _resolve_address(self, username: str | None = None) -> str:
        domain = _next_domain(self.domain)
        if self.subdomain:
            domain = f"{random.choice(self.subdomain)}.{domain}"
        if username:
            local_part = username
        elif self.email_prefix:
            local_part = f"{self.email_prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
        else:
            local_part = _random_mailbox_name()
        return f"{local_part}@{domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.domain:
            raise RuntimeError("CloudMailGen 需要至少配置一个 domain")
        address = self._resolve_address(username)
        token = self._get_token()
        self._request(
            "POST",
            "/api/public/addUser",
            headers={"Authorization": token},
            payload={"list": [{"email": address}]},
        )
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("CloudMailGen 缺少 address")
        token = self._get_token()
        data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            self._clear_token_cache()
            token = self._get_token()
            data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        items = data.get("data") or []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or item.get("emailId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("sender") or item.get("sendEmail") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt") or item.get("created_at") or item.get("createTime") or item.get("receivedAt") or item.get("date") or item.get("timestamp")
            ),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def close(self) -> None:
        self.session.close()


TEMPMAIL_LOL_API_BASE = "https://api.tempmail.lol/v2"
_TEMPMAIL_KEY_SPLIT_RE = re.compile(r"[\s,，;；]+")
TEMPMAIL_DOMAIN_STATS_FILE = DATA_DIR / "tempmail_domain_stats.json"
_tempmail_domain_stats_lock = Lock()


def _tempmail_root_domain(address_or_domain: str) -> str:
    source = str(address_or_domain or "").strip().lower()
    domain = source.partition("@")[2] or source
    labels = [part for part in domain.strip(".").split(".") if part]
    if len(labels) < 2:
        return domain.strip(".")
    compound_suffixes = {"co.uk", "com.cn", "net.cn", "org.cn", "com.au", "co.jp"}
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in compound_suffixes and len(labels) >= 3 else suffix


def _load_tempmail_domain_stats() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(TEMPMAIL_DOMAIN_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): dict(value) for key, value in data.items() if isinstance(value, dict)}


def _save_tempmail_domain_stats(stats: dict[str, dict[str, Any]]) -> None:
    TEMPMAIL_DOMAIN_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = TEMPMAIL_DOMAIN_STATS_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_file.replace(TEMPMAIL_DOMAIN_STATS_FILE)


def _record_tempmail_domain_result(
    domain: str,
    *,
    received: bool,
) -> None:
    root = _tempmail_root_domain(domain)
    if not root:
        return
    now = datetime.now(timezone.utc)
    with _tempmail_domain_stats_lock:
        stats = _load_tempmail_domain_stats()
        entry = stats.setdefault(root, {})
        entry["received"] = int(entry.get("received") or 0)
        entry["timeouts"] = int(entry.get("timeouts") or 0)
        entry["consecutive_timeouts"] = int(entry.get("consecutive_timeouts") or 0)
        if received:
            entry["received"] += 1
            entry["consecutive_timeouts"] = 0
        else:
            entry["timeouts"] += 1
            entry["consecutive_timeouts"] += 1
        # Domain history is passive telemetry only; it never blocks an address.
        entry.pop("cooldown_until", None)
        entry.pop("skipped", None)
        entry["updated_at"] = now.isoformat()
        _save_tempmail_domain_stats(stats)


def tempmail_domain_stats_snapshot() -> list[dict[str, Any]]:
    with _tempmail_domain_stats_lock:
        stats = _load_tempmail_domain_stats()
    result: list[dict[str, Any]] = []
    for domain, entry in stats.items():
        received = int(entry.get("received") or 0)
        timeouts = int(entry.get("timeouts") or 0)
        result.append(
            {
                "domain": domain,
                "received": received,
                "timeouts": timeouts,
                "success_rate": round(received * 100 / max(1, received + timeouts), 1),
                "consecutive_timeouts": int(entry.get("consecutive_timeouts") or 0),
            }
        )
    return sorted(result, key=lambda item: (float(item["success_rate"]), str(item["domain"])))


def mark_verification_code_received(mailbox: dict[str, Any]) -> None:
    if str(mailbox.get("provider") or "") != "tempmail_lol" or mailbox.get("_domain_delivery_recorded"):
        return
    _record_tempmail_domain_result(
        str(mailbox.get("address") or ""),
        received=True,
    )
    mailbox["_domain_delivery_recorded"] = True


def _mark_tempmail_verification_timeout(mailbox: dict[str, Any]) -> None:
    if str(mailbox.get("provider") or "") != "tempmail_lol" or mailbox.get("_domain_delivery_recorded"):
        return
    _record_tempmail_domain_result(
        str(mailbox.get("address") or ""),
        received=False,
    )
    mailbox["_domain_delivery_recorded"] = True


def _parse_tempmail_keys(raw: Any) -> list[str]:
    """Parse one or more TempMail.lol keys; an empty key means anonymous access."""
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        for piece in _TEMPMAIL_KEY_SPLIT_RE.split(str(value or "")):
            key = piece.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys or [""]


class _TempMailKeyPool:
    """Thread-safe round-robin key selector without registration throttling."""

    def __init__(self, keys: list[str]):
        self.keys = list(keys)
        self._lock = Lock()
        self._cursor = 0

    def next_key(self, *, exclude: set[str] | None = None) -> str | None:
        excluded = exclude or set()
        with self._lock:
            for offset in range(len(self.keys)):
                index = (self._cursor + offset) % len(self.keys)
                key = self.keys[index]
                if key in excluded:
                    continue
                self._cursor = (index + 1) % len(self.keys)
                return key
        return None


_tempmail_pool_lock = Lock()
_tempmail_pools: dict[str, tuple[tuple[str, ...], _TempMailKeyPool]] = {}
_tempmail_token_key_lock = Lock()
_tempmail_token_keys: dict[str, str] = {}
_tempmail_domain_cursor_lock = Lock()
_tempmail_domain_cursors: dict[str, tuple[tuple[str, ...], int]] = {}


def parse_tempmail_domains(raw: Any) -> list[str]:
    """Normalize optional TempMail.lol domains from a list or multiline text."""
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    domains: list[str] = []
    seen: set[str] = set()
    for value in values:
        for piece in _TEMPMAIL_KEY_SPLIT_RE.split(str(value or "")):
            domain = piece.strip().lower().lstrip("@").rstrip(".")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
    return domains


def _next_tempmail_domain(provider_ref: str, domains: list[str]) -> str:
    """Round-robin domains across short-lived provider instances and threads."""
    signature = tuple(domains)
    if not signature:
        return ""
    scope = provider_ref or "tempmail_lol#default"
    with _tempmail_domain_cursor_lock:
        current = _tempmail_domain_cursors.get(scope)
        index = current[1] if current is not None and current[0] == signature else 0
        domain = signature[index % len(signature)]
        _tempmail_domain_cursors[scope] = (signature, (index + 1) % len(signature))
        return domain


def _get_tempmail_pool(provider_ref: str, keys: list[str]) -> _TempMailKeyPool:
    pool_key = provider_ref or "tempmail_lol#default"
    signature = tuple(keys)
    with _tempmail_pool_lock:
        current = _tempmail_pools.get(pool_key)
        if current is None or current[0] != signature:
            pool = _TempMailKeyPool(keys)
            _tempmail_pools[pool_key] = (signature, pool)
            return pool
        return current[1]


def _remember_tempmail_token_key(token: str, key: str) -> None:
    if not token:
        return
    with _tempmail_token_key_lock:
        _tempmail_token_keys.pop(token, None)
        _tempmail_token_keys[token] = key
        if len(_tempmail_token_keys) > 1024:
            for old_token in list(_tempmail_token_keys)[:512]:
                _tempmail_token_keys.pop(old_token, None)


def _lookup_tempmail_token_key(token: str) -> str | None:
    with _tempmail_token_key_lock:
        return _tempmail_token_keys.get(token)


class _TempMailLolRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TempMailLolProvider(BaseMailProvider):
    name = "tempmail_lol"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        raw_keys = entry.get("api_key") or entry.get("tempmail_api_key") or ""
        self.api_keys = _parse_tempmail_keys(raw_keys)
        self.domains = parse_tempmail_domains(entry.get("domain"))
        self.key_pool = _get_tempmail_pool(self.provider_ref, self.api_keys)
        self._last_status_code: int | None = None
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, *, api_key: str | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
        selected_key = self.api_keys[0] if api_key is None else api_key
        headers = {"Authorization": f"Bearer {selected_key}"} if selected_key else {}
        try:
            resp = self.session.request(
                method.upper(),
                f"{TEMPMAIL_LOL_API_BASE}{path}",
                headers=headers,
                params=params,
                json=payload,
                timeout=self.conf["request_timeout"],
                verify=False,
            )
        except Exception as error:
            raise _TempMailLolRequestError(f"TempMail.lol 请求失败: {method} {path}, {error}") from error
        self._last_status_code = int(resp.status_code)
        if resp.status_code not in expected:
            raise _TempMailLolRequestError(
                f"TempMail.lol 请求失败: {method} {path}, HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as error:
            raise _TempMailLolRequestError(
                f"TempMail.lol {method} {path} 响应解析失败: {error}",
                status_code=resp.status_code,
            ) from error
        if not isinstance(data, dict):
            raise _TempMailLolRequestError(
                f"TempMail.lol {method} {path} 返回结构不是对象",
                status_code=resp.status_code,
            )
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        selected_domain = _next_tempmail_domain(self.provider_ref, self.domains)
        api_key = self.key_pool.next_key()
        if api_key is None:
            raise RuntimeError("TempMail.lol 没有可用 API Key")
        payload: dict[str, Any] = {"prefix": username or _random_mailbox_name()}
        if selected_domain:
            payload["domain"] = selected_domain
        try:
            data = self._request("POST", "/inbox/create", api_key=api_key, payload=payload, expected=(200, 201))
        except _TempMailLolRequestError as error:
            status = f"HTTP {error.status_code}" if error.status_code is not None else "network"
            raise RuntimeError(f"TempMail.lol 创建邮箱失败 ({status}): {error}") from error

        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol 响应缺少 address/token")
        _remember_tempmail_token_key(token, api_key)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "token": token,
        }

    @staticmethod
    def _inbox_items(data: dict[str, Any]) -> list[dict[str, Any]]:
        items = data.get("emails") or data.get("messages") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _message_from_item(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        text_content, html_content = _extract_content(item)
        received_at = _parse_received_at(
            item.get("created_at") or item.get("createdAt") or item.get("date") or item.get("received_at") or item.get("timestamp")
        )
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or item.get("message_id") or item.get("token") or item.get("date") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("from_address") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": received_at,
            "raw": item,
        }

    def _mailbox_api_key(self, mailbox: dict[str, Any]) -> str:
        remembered = _lookup_tempmail_token_key(str(mailbox.get("token") or ""))
        return self.api_keys[0] if remembered is None else remembered

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request(
            "GET",
            "/inbox",
            api_key=self._mailbox_api_key(mailbox),
            params={"token": mailbox["token"]},
        )
        messages = self._inbox_items(data)
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("created_at") or value.get("createdAt") or value.get("date") or value.get("received_at") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or value.get("token") or "")))
        return self._message_from_item(mailbox, item)

    def prepare_code_baseline(self, mailbox: dict[str, Any]) -> None:
        # A newly created TempMail.lol inbox has no legitimate pre-existing OTP.
        # Avoid the extra pre-send read used by reusable providers: the API can
        # expose a just-delivered OTP during that read and incorrectly reject it.
        mailbox["_code_not_before"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        primary_key = self._mailbox_api_key(mailbox)
        api_key = primary_key
        consecutive_errors = 0
        deadline = time.monotonic() + self.conf["wait_timeout"]
        successful_queries = 0
        empty_inboxes = 0
        http_429 = 0
        http_5xx = 0
        other_errors = 0
        scanned = 0
        boundary_filtered = 0
        no_code = 0
        rejected_codes = 0
        key_switches = 0
        last_batch = 0
        raw_messages_seen = 0
        last_status: int | None = None

        def log_diagnostics(outcome: str) -> None:
            domain = _tempmail_root_domain(str(mailbox.get("address") or "")) or "unknown"
            if successful_queries and empty_inboxes == successful_queries and not raw_messages_seen:
                conclusion = "upstream_delivery_not_observed"
            elif raw_messages_seen and not scanned:
                conclusion = "mail_present_but_already_seen_or_filtered"
            elif scanned and no_code:
                conclusion = "mail_present_but_code_not_recognized"
            elif http_429 or http_5xx or other_errors:
                conclusion = "provider_query_errors"
            else:
                conclusion = "code_recognized" if outcome == "命中" else "inconclusive"
            _provider_log(
                f"TempMail.lol 验证码轮询{outcome}: domain={domain}, "
                f"successful_queries={successful_queries}, empty_inboxes={empty_inboxes}, "
                f"http_429={http_429}, http_5xx={http_5xx}, other_errors={other_errors}, "
                f"last_status={last_status if last_status is not None else 'none'}, last_batch={last_batch}, "
                f"raw_messages_seen={raw_messages_seen}, delivery_observed={str(bool(raw_messages_seen)).lower()}, "
                f"scanned={scanned}, no_code={no_code}, rejected_codes={rejected_codes}, "
                f"boundary_filtered={boundary_filtered}, key_switches={key_switches}, conclusion={conclusion}"
            )

        while time.monotonic() < deadline and not self._stopped():
            try:
                data = self._request(
                    "GET",
                    "/inbox",
                    api_key=api_key,
                    params={"token": mailbox["token"]},
                )
                last_status = self._last_status_code
                successful_queries += 1
                consecutive_errors = 0
            except _TempMailLolRequestError as error:
                last_status = error.status_code
                consecutive_errors += 1
                if error.status_code == 429:
                    http_429 += 1
                    log_diagnostics("HTTP 429")
                    raise RuntimeError("TempMail.lol 验证码查询失败: HTTP 429") from error
                if error.status_code is not None and 500 <= error.status_code < 600:
                    http_5xx += 1
                else:
                    other_errors += 1
                if consecutive_errors == 3:
                    fallback = self.key_pool.next_key(exclude={primary_key})
                    if fallback is not None:
                        api_key = fallback
                        key_switches += 1
                if self._poll_wait():
                    break
                continue
            except Exception:
                other_errors += 1
                consecutive_errors += 1
                if self._poll_wait():
                    break
                continue

            messages = [self._message_from_item(mailbox, item) for item in self._inbox_items(data)]
            last_batch = len(messages)
            raw_messages_seen += last_batch
            if not messages:
                empty_inboxes += 1
            messages.sort(
                key=lambda message: (
                    (message.get("received_at") or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
                    str(message.get("message_id") or ""),
                ),
                reverse=True,
            )
            for message in messages:
                if _message_before_code_boundary(mailbox, message) and not message.get("message_id"):
                    boundary_filtered += 1
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                scanned += 1
                code = _extract_code(message)
                if code and _verification_code_rejected(mailbox, code):
                    rejected_codes += 1
                    seen_refs.add(ref)
                    seen_value.append(ref)
                    continue
                if code:
                    seen_refs.add(ref)
                    seen_value.append(ref)
                    log_diagnostics("命中")
                    return code
                no_code += 1
            if self._poll_wait():
                break
        log_diagnostics("超时")
        return None

    def close(self) -> None:
        self.session.close()


class DuckMailProvider(BaseMailProvider):
    name = "duckmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", use_api_key: bool = False, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.api_key if use_api_key else token}"} if use_api_key or token else {}
        resp = self.session.request(method.upper(), f"https://api.duckmail.sbs{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DuckMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("hydra:member") or data.get("member") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username or _random_mailbox_name()}@{self.default_domain}"
        payload = {"address": address, "password": password}
        account = self._request("POST", "/accounts", use_api_key=True, payload=payload)
        token_data = self._request("POST", "/token", use_api_key=True, payload=payload)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": str(token_data.get("token") or ""), "password": password, "account_id": str(account.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"page": 1})
        items = self._items(data)
        if not items:
            return None
        item = items[0]
        message_id = str(item.get("id") or item.get("@id") or "").replace("/messages/", "")
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""))
        sender = item.get("from") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("name") or ""
        html_content = item.get("html") or ""
        if isinstance(html_content, list):
            html_content = "".join(str(value) for value in html_content)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": str(item.get("text") or item.get("text_content") or ""), "html_content": str(html_content), "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")), "raw": item}

    def close(self) -> None:
        self.session.close()


class GptMailProvider(BaseMailProvider):
    name = "gptmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "").strip()
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key})

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        query = dict(params or {})
        resp = self.session.request(method.upper(), f"https://mail.chatgpt.org.uk{path}", params=query, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code != 200:
            raise RuntimeError(f"GPTMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in {"prefix": username, "domain": self.default_domain}.items() if value}
        data = self._request("POST" if payload else "GET", "/api/generate-email", payload=payload or None)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": str(data["email"])}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/emails", params={"email": mailbox["address"]})
        emails = data if isinstance(data, list) else data.get("emails") or []
        if not emails:
            return None
        item = max(emails, key=lambda value: (float(value.get("timestamp") or 0), str(value.get("id") or "")))
        if item.get("id"):
            item = self._request("GET", f"/api/email/{item['id']}")
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from_address") or ""), "text_content": str(item.get("content") or ""), "html_content": str(item.get("html_content") or ""), "received_at": _parse_received_at(item.get("timestamp") or item.get("created_at")), "raw": item}

    def close(self) -> None:
        self.session.close()


class MoEmailProvider(BaseMailProvider):
    name = "moemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.expiry_time = int(entry.get("expiry_time") or 0)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"MoEmail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"MoEmail {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/api/emails/generate", payload={"name": username or _random_mailbox_name(), "expiryTime": self.expiry_time, "domain": _next_domain(self.domain)}, expected=(200, 201))
        address = str(data.get("email") or "").strip()
        email_id = str(data.get("id") or data.get("email_id") or "").strip()
        if not address or not email_id:
            raise RuntimeError("MoEmail 缺少 email 或 id")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "email_id": email_id}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        email_id = str(mailbox.get("email_id") or "").strip()
        if not email_id:
            raise RuntimeError("MoEmail 缺少 email_id")
        data = self._request("GET", f"/api/emails/{email_id}")
        items = data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        _, item = max(enumerate(messages), key=lambda pair: (((_parse_received_at(pair[1].get("createdAt") or pair[1].get("created_at") or pair[1].get("receivedAt") or pair[1].get("date") or pair[1].get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()), pair[0]))
        message_id = str(item.get("id") or item.get("message_id") or item.get("_id") or "").strip()
        detail = self._request("GET", f"/api/emails/{email_id}/{message_id}") if message_id else {"message": item}
        message = detail.get("message") if isinstance(detail.get("message"), dict) else detail
        text_content, html_content = _extract_content(message)
        sender = message.get("from") or message.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(message.get("subject") or item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(message.get("createdAt") or message.get("created_at") or message.get("receivedAt") or message.get("date") or message.get("timestamp") or item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": detail}

    def close(self) -> None:
        self.session.close()


class InbucketMailProvider(BaseMailProvider):
    name = "inbucket"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.random_subdomain = bool(entry.get("random_subdomain", True))
        self.session = _create_session(conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"Inbucket 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        content_type = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def _resolve_domain(self) -> str:
        if self.domain:
            return _next_domain(self.domain)
        raise RuntimeError("Inbucket 需要至少配置一个 domain")

    def _mailbox_name(self, address: str) -> str:
        local_part, _, _ = str(address or "").partition("@")
        return local_part.strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = username or _random_mailbox_name()
        base_domain = self._resolve_domain()
        domain = f"{_random_subdomain_label()}.{base_domain}" if self.random_subdomain else base_domain
        address = f"{local_part}@{domain}"
        mailbox_name = self._mailbox_name(address)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "base_domain": base_domain,
            "mailbox_name": mailbox_name,
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        mailbox_name = str(mailbox.get("mailbox_name") or self._mailbox_name(str(mailbox.get("address") or ""))).strip()
        if not mailbox_name:
            raise RuntimeError("Inbucket 缺少 mailbox_name")
        data = self._request("GET", f"/api/v1/mailbox/{mailbox_name}")
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if not items:
            return None
        items.sort(
            key=lambda value: (
                (_parse_received_at(value.get("date")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
                str(value.get("id") or ""),
            ),
            reverse=True,
        )
        address = str(mailbox.get("address") or "").strip()
        for item in items:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            detail = self._request("GET", f"/api/v1/mailbox/{mailbox_name}/{message_id}")
            if not isinstance(detail, dict):
                continue
            header = detail.get("header") if isinstance(detail.get("header"), dict) else {}
            body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
            normalized = {
                "provider": self.name,
                "mailbox": mailbox_name,
                "message_id": message_id,
                "subject": str(detail.get("subject") or item.get("subject") or ""),
                "sender": str(detail.get("from") or item.get("from") or ""),
                "text_content": str(body.get("text") or ""),
                "html_content": str(body.get("html") or ""),
                "received_at": _parse_received_at(detail.get("date") or item.get("date")),
                "to": header.get("To") if isinstance(header, dict) else None,
                "raw": detail,
            }
            if _message_matches_email(normalized, address):
                return normalized
        return None

    def close(self) -> None:
        self.session.close()


class YydsMailProvider(BaseMailProvider):
    name = "yyds_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.subdomain = str(entry.get("subdomain") or "").strip()
        self.wildcard = bool(entry.get("wildcard"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"YYDSMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"YYDSMail 请求失败: {data.get('errorCode') or data.get('error')}")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("items") or data.get("messages") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {"localPart": username or _random_mailbox_name()}
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        if self.subdomain:
            payload["subdomain"] = self.subdomain
        data = self._request("POST", "/accounts/wildcard" if self.wildcard else "/accounts", payload=payload)
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or data.get("temp_token") or data.get("tempToken") or data.get("access_token") or "").strip()
        if not address or not token:
            raise RuntimeError("YYDSMail 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token, "account_id": str(data.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("createdAt") or value.get("created_at") or value.get("receivedAt") or value.get("date") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or "")))
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"


class OutlookTokenError(RuntimeError):
    """refresh_token 换取 access_token 失败（凭据失效/权限不对），与“读邮件失败”区分。"""


def _clean_outlook_value(value: str) -> str:
    return str(value or "").replace("﻿", "").replace(" ", " ").strip()


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    """解析邮箱池文本，每行格式：email----password----client_id----refresh_token。"""
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = _clean_outlook_value(raw_line)
        if not line or "----" not in line:
            continue
        parts = [_clean_outlook_value(part) for part in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    return credentials


def _normalize_outlook_pool(value: Any) -> list[dict[str, str]]:
    """邮箱池既支持纯文本（每行一条），也支持已解析的对象列表。"""
    if isinstance(value, str):
        return parse_outlook_credentials(value)
    if isinstance(value, list):
        items: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                items.extend(parse_outlook_credentials(item))
            elif isinstance(item, dict):
                email = _clean_outlook_value(item.get("email") or item.get("address") or "")
                client_id = _clean_outlook_value(item.get("client_id") or "")
                refresh_token = _clean_outlook_value(item.get("refresh_token") or "")
                if "@" in email and client_id and refresh_token:
                    items.append({"email": email, "password": _clean_outlook_value(item.get("password") or ""), "client_id": client_id, "refresh_token": refresh_token})
        return items
    return []


class OutlookTokenProvider(BaseMailProvider):
    """使用 refresh_token 读取 Outlook/Hotmail 邮箱验证码。

    邮箱池在应用配置里维护（mailboxes 字段，每行 email----password----client_id----refresh_token），
    create_mailbox() 从池中取下一个未使用的邮箱，wait_for_code() 用 refresh_token 换取 access_token
    后通过 Graph/IMAP 读取最新邮件。
    """

    name = "outlook_token"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = _normalize_outlook_pool(entry.get("mailboxes") or entry.get("pool"))
        self.mode = str(entry.get("mode") or "graph").strip().lower() or "graph"
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "graph"
        self.imap_host = str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip() or OUTLOOK_DEFAULT_IMAP_HOST
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _exchange_refresh_token(self, client_id: str, refresh_token: str, scope: str) -> str:
        resp = self.session.post(
            OUTLOOK_TOKEN_URL,
            data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scope},
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.conf["user_agent"]},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error_description") or data.get("error") or resp.text[:300]
            raise OutlookTokenError(f"OutlookToken 刷新失败: HTTP {resp.status_code}, {detail}")
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise OutlookTokenError("OutlookToken 刷新响应缺少 access_token")
        return access_token

    def _access_token(self, mailbox: dict[str, Any], client_id: str, refresh_token: str, scope: str) -> str:
        """缓存 access_token 复用：避免 wait_for_code 轮询时每次都换 token 触发限流。"""
        cache = mailbox.get("_outlook_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_outlook_token_cache"] = cache
        cached = cache.get(scope)
        if isinstance(cached, tuple) and len(cached) == 2 and time.monotonic() < cached[1]:
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, refresh_token, scope)
        cache[scope] = (token, time.monotonic() + 600)
        return token

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError("OutlookToken 邮箱池为空，请在邮箱配置中导入 email----password----client_id----refresh_token")
        with _outlook_token_state_lock:
            store = _load_outlook_token_state()
            credential = next((item for item in self.pool if _outlook_entry_available(store.get(item["email"].strip().lower()))), None)
            if credential is None:
                raise RuntimeError(f"[{self.label}] OutlookToken 邮箱池暂无可用邮箱（共 {len(self.pool)} 个，已用尽或全部占用/失效），请导入新邮箱或重置池状态")
            store[credential["email"].strip().lower()] = {"state": "in_use", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()}
            _save_outlook_token_state(store)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": credential["email"],
            "label": self.label,
            "client_id": credential["client_id"],
            "refresh_token": credential["refresh_token"],
        }

    def _read_graph(self, access_token: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            OUTLOOK_GRAPH_MESSAGES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            params={"$top": self.message_limit, "$orderby": "receivedDateTime desc", "$select": "subject,receivedDateTime,from,body,bodyPreview"},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else resp.text[:300]
            raise RuntimeError(f"OutlookToken Graph 失败: HTTP {resp.status_code}, {detail}")
        items = data.get("value") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _graph_sender(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        if isinstance(sender, dict):
            address = sender.get("emailAddress") or {}
            if isinstance(address, dict):
                return str(address.get("address") or address.get("name") or "")
        return ""

    def _normalize_graph_item(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        content_type = str(body.get("contentType") or "").lower()
        content = str(body.get("content") or "")
        text_content = content if content_type != "html" else str(item.get("bodyPreview") or "")
        html_content = content if content_type == "html" else ""
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": self._graph_sender(item),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedDateTime")),
            "raw": item,
        }

    def _graph_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件（Graph 已按 receivedDateTime desc 排序，最新在前）。"""
        return [self._normalize_graph_item(mailbox, item) for item in self._read_graph(access_token)]

    def _imap_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件，最新在前。"""
        auth_string = f"user={mailbox['address']}\x01auth=Bearer {access_token}\x01\x01"
        imap = imaplib.IMAP4_SSL(self.imap_host)
        try:
            imap.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("OutlookToken IMAP select INBOX 失败")
            status, data = imap.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-self.message_limit :]
            messages: list[dict[str, Any]] = []
            for uid in reversed(uids):  # 最新在前
                status, fetched = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw_payload = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
                if raw_payload:
                    messages.append(self._parse_imap_message(mailbox, raw_payload))
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _parse_imap_message(self, mailbox: dict[str, Any], raw: bytes) -> dict[str, Any]:
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = _parse_received_at(parsedate_to_datetime(str(message.get("Date") or "")))
        except Exception:
            received = None
        plain: list[str] = []
        html: list[str] = []
        for part in (message.walk() if message.is_multipart() else [message]):
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if not payload:
                continue
            if part.get_content_type() == "text/html":
                html.append(str(payload))
            else:
                plain.append(str(payload))

        def _decode(value: str | None) -> str:
            if not value:
                return ""
            try:
                return str(make_header(decode_header(value)))
            except Exception:
                return value

        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": _decode(str(message.get("Message-ID") or "")),
            "subject": _decode(str(message.get("Subject") or "")),
            "sender": _decode(str(message.get("From") or "")),
            "text_content": "\n".join(plain).strip(),
            "html_content": "\n".join(html).strip(),
            "received_at": received,
            "raw": None,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        """拉取最近 N 封邮件（最新在前），供 wait_for_code 逐封扫描验证码。"""
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("OutlookToken mailbox 缺少 client_id 或 refresh_token")
        errors: list[str] = []
        if self.mode in {"graph", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_GRAPH_SCOPE)
                return self._graph_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "graph":
                    raise
                errors.append(f"graph: {error}")
        if self.mode in {"imap", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_IMAP_SCOPE)
                return self._imap_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "imap":
                    raise
                errors.append(f"imap: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        """轮询时遍历最近 N 封邮件，逐封提取验证码，避免最新一封是广告/安全提醒时错过验证码。"""
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline and not self._stopped():
            for message in self.fetch_recent_messages(mailbox):
                if _message_before_code_boundary(mailbox, message):
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                code = _extract_code(message)
                if code and not _verification_code_rejected(mailbox, code):
                    seen_value.append(ref)
                    return code
                seen_refs.add(ref)
            if self._poll_wait():
                break
        return None


def _entries(mail_config: dict) -> list[dict]:
    result: list[dict] = []
    counters: dict[str, int] = {}
    for item in mail_config["providers"]:
        idx = len(result) + 1
        t = item.get("type", "")
        cnt = counters.get(t, 0) + 1
        counters[t] = cnt
        label = f"DDG-{cnt}" if t == "ddg_mail" else f"{t}#{idx}"
        result.append({**item, "provider_ref": f"{item['type']}#{idx}", "label": label})
    return result


def _enabled_entries(mail_config: dict) -> list[dict]:
    items = [item for item in _entries(mail_config) if item.get("enable")]
    if not items:
        raise RuntimeError("mail.providers 没有启用的 provider")
    return items


def _next_entry(mail_config: dict) -> dict:
    global provider_index
    items = _enabled_entries(mail_config)
    if len(items) == 1:
        return dict(items[0])
    with provider_lock:
        value = dict(items[provider_index % len(items)])
        provider_index = (provider_index + 1) % len(items)
        return value


def _create_provider(mail_config: dict, provider: str = "", provider_ref: str = "") -> BaseMailProvider:
    entry = next((dict(item) for item in _entries(mail_config) if provider_ref and item["provider_ref"] == provider_ref), None)
    entry = entry or next((dict(item) for item in _enabled_entries(mail_config) if provider and item["type"] == provider), None) or _next_entry(mail_config)
    conf = _config(mail_config)
    if entry["type"] == "cloudmail_gen":
        return CloudMailGenProvider(entry, conf)
    if entry["type"] == "cloudflare_temp_email":
        return CloudflareTempMailProvider(entry, conf)
    if entry["type"] == "ddg_mail":
        return DDGMailProvider(entry, conf)
    if entry["type"] == "tempmail_lol":
        return TempMailLolProvider(entry, conf)
    if entry["type"] == "duckmail":
        return DuckMailProvider(entry, conf)
    if entry["type"] == "gptmail":
        return GptMailProvider(entry, conf)
    if entry["type"] == "moemail":
        return MoEmailProvider(entry, conf)
    if entry["type"] == "inbucket":
        return InbucketMailProvider(entry, conf)
    if entry["type"] == "yyds_mail":
        return YydsMailProvider(entry, conf)
    if entry["type"] == "outlook_token":
        return OutlookTokenProvider(entry, conf)
    raise RuntimeError(f"不支持的 mail.provider: {entry['type']}")


def create_mailbox(mail_config: dict, username: str | None = None) -> dict:
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            mailbox = provider.create_mailbox(username)
            mailbox["_code_not_before"] = datetime.now(timezone.utc)
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法创建邮箱")


def wait_for_code(mail_config: dict, mailbox: dict, stop_event: Event | None = None) -> str | None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    provider.stop_event = stop_event
    try:
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def prepare_code_baseline(mail_config: dict, mailbox: dict) -> None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        provider.prepare_code_baseline(mailbox)
    finally:
        provider.close()


def mark_mailbox_result(mailbox: dict, *, success: bool, error: Exception | str | None = None) -> None:
    """注册流程结束后更新邮箱池状态。

    仅对 outlook_token 邮箱生效：成功标记 used；失败时若是 token 失效标记 token_invalid，
    其余失败标记 failed（保留邮箱占用以便排查，可通过重置释放）。
    """
    _release_fixed_mailbox(mailbox)
    reason = str(error or "").strip()
    if not success and "等待注册验证码超时" in reason:
        _mark_tempmail_verification_timeout(mailbox)
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return
    if success:
        _set_outlook_token_state(address, "used")
        return
    if isinstance(error, OutlookTokenError) or "OutlookToken 刷新失败" in reason or "access_token" in reason:
        _set_outlook_token_state(address, "token_invalid", reason[:300])
    else:
        _set_outlook_token_state(address, "failed", reason[:300])


def release_mailbox(mailbox: dict) -> None:
    """把 outlook_token 邮箱从 in_use 释放回未使用（用于流程主动放弃且未消费验证码时）。"""
    _release_fixed_mailbox(mailbox)
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    _release_outlook_token_state(str(mailbox.get("address") or ""))


def get_existing_mailbox(mail_config: dict, email: str) -> dict:
    """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            if hasattr(provider, "get_existing_mailbox"):
                mailbox = provider.get_existing_mailbox(email)
                return mailbox
            else:
                raise RuntimeError(f"邮箱提供商 {provider.name} 不支持查询已有邮箱")
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法查询已有邮箱")
