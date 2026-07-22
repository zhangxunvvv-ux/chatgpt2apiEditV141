from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_der_private_key,
)
from curl_cffi import requests
from nacl.public import SealedBox
from nacl.signing import SigningKey

from services.config import DATA_DIR
from services.proxy_service import proxy_settings


AUTH_API_BASE = "https://auth.openai.com/api/accounts"
RUNTIME_REGISTER_URL = f"{AUTH_API_BASE}/v1/agent/register"
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"
DEFAULT_CAPABILITIES = ["responsesapi"]


class GptFreeIdentityError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_b64(value: str) -> bytes:
    normalized = str(value or "").strip()
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    try:
        return base64.b64decode(normalized, validate=True)
    except Exception:
        return base64.urlsafe_b64decode(normalized)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decode_jwt_claims(access_token: str) -> dict[str, Any]:
    parts = str(access_token or "").split(".")
    if len(parts) != 3:
        raise GptFreeIdentityError("gptfree_access_token_invalid_jwt")
    try:
        raw = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except Exception as exc:
        raise GptFreeIdentityError("gptfree_access_token_invalid_payload") from exc
    if not isinstance(value, dict):
        raise GptFreeIdentityError("gptfree_access_token_invalid_claims")
    return value


def validate_access_token(access_token: str, *, now: int | None = None) -> dict[str, Any]:
    claims = decode_jwt_claims(access_token)
    current = int(time.time()) if now is None else int(now)
    try:
        expires_at = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not expires_at:
        raise GptFreeIdentityError("gptfree_access_token_missing_exp")
    if expires_at <= current + 30:
        raise GptFreeIdentityError("gptfree_access_token_expired")

    auth = claims.get("https://api.openai.com/auth")
    profile = claims.get("https://api.openai.com/profile")
    auth = auth if isinstance(auth, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    account_id = str(auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id") or "").strip()
    user_id = str(auth.get("chatgpt_user_id") or claims.get("chatgpt_user_id") or claims.get("sub") or "").strip()
    if not account_id:
        raise GptFreeIdentityError("gptfree_access_token_missing_account_id")
    if not user_id:
        raise GptFreeIdentityError("gptfree_access_token_missing_user_id")
    return {
        "account_id": account_id,
        "chatgpt_user_id": user_id,
        "email": str(profile.get("email") or claims.get("email") or "").strip(),
        "plan_type": str(auth.get("chatgpt_plan_type") or "free").strip() or "free",
        "expires_at": str(expires_at),
        "chatgpt_account_is_fedramp": bool(
            auth.get("chatgpt_account_is_fedramp") or claims.get("chatgpt_account_is_fedramp")
        ),
    }


def generate_ed25519_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_der = private_key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    algorithm = b"ssh-ed25519"
    blob = (
        len(algorithm).to_bytes(4, "big")
        + algorithm
        + len(public_raw).to_bytes(4, "big")
        + public_raw
    )
    return base64.b64encode(private_der).decode("ascii"), f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"


def _private_key(private_key_b64: str) -> Ed25519PrivateKey:
    try:
        key = load_der_private_key(base64.b64decode(private_key_b64), password=None)
    except Exception as exc:
        raise GptFreeIdentityError("gptfree_private_key_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise GptFreeIdentityError("gptfree_private_key_not_ed25519")
    return key


def sign_message(private_key_b64: str, message: str) -> str:
    return base64.b64encode(_private_key(private_key_b64).sign(message.encode("utf-8"))).decode("ascii")


def build_agent_assertion(
    agent_runtime_id: str,
    task_id: str,
    private_key_b64: str,
    *,
    timestamp: str | None = None,
) -> str:
    created_at = timestamp or utc_timestamp()
    signature = sign_message(private_key_b64, f"{agent_runtime_id}:{task_id}:{created_at}")
    payload = {
        "agent_runtime_id": agent_runtime_id,
        "task_id": task_id,
        "timestamp": created_at,
        "signature": signature,
    }
    return _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def decrypt_task_id(private_key_b64: str, encrypted_task_id: str) -> str:
    private = _private_key(private_key_b64)
    raw_seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    curve_private = SigningKey(raw_seed).to_curve25519_private_key()
    try:
        decrypted = SealedBox(curve_private).decrypt(_decode_b64(encrypted_task_id))
    except Exception as exc:
        raise GptFreeIdentityError("gptfree_task_id_decrypt_failed") from exc
    value = decrypted.decode("utf-8").strip()
    if not value:
        raise GptFreeIdentityError("gptfree_task_id_empty")
    return value


class GptFreeIdentityService:
    def __init__(
        self,
        identities_file: Path | None = None,
        master_key_file: Path | None = None,
    ) -> None:
        self.identities_file = identities_file or (DATA_DIR / "gptfree_identities.json")
        self.master_key_file = master_key_file or (DATA_DIR / ".gptfree-master.key")
        self._lock = threading.RLock()
        self._task_locks: dict[str, threading.Lock] = {}
        self._tasks: dict[str, str] = {}
        self._fernet = Fernet(self._load_or_create_master_key())
        self._identities = self._load()

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load_or_create_master_key(self) -> bytes:
        self.master_key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = self.master_key_file.read_bytes().strip()
            Fernet(key)
            return key
        except FileNotFoundError:
            key = Fernet.generate_key()
            self.master_key_file.write_bytes(key + b"\n")
            self._restrict_permissions(self.master_key_file)
            return key
        except Exception as exc:
            raise GptFreeIdentityError("gptfree_master_key_invalid") from exc

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.identities_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            raise GptFreeIdentityError("gptfree_identity_store_invalid") from exc
        items = raw.get("identities") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return {}
        return {str(key): dict(value) for key, value in items.items() if isinstance(value, dict)}

    def _save_locked(self) -> None:
        self.identities_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "identities": self._identities}
        temporary = self.identities_file.with_suffix(self.identities_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._restrict_permissions(temporary)
        os.replace(temporary, self.identities_file)
        self._restrict_permissions(self.identities_file)

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise GptFreeIdentityError("gptfree_private_key_decrypt_failed") from exc

    @staticmethod
    def _safe_response_json(response: Any) -> dict[str, Any]:
        try:
            value = response.json()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _safe_error_body(response: Any) -> str:
        value = GptFreeIdentityService._safe_response_json(response)
        if value:
            error = value.get("error")
            if isinstance(error, dict):
                return str(error.get("code") or error.get("message") or "upstream_error")[:160]
            return str(value.get("code") or value.get("message") or "upstream_error")[:160]
        return "upstream_error"

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        account_context: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        kwargs = proxy_settings.build_session_kwargs(account=account_context or {}, impersonate="chrome", verify=True)
        session = requests.Session(**kwargs)
        try:
            for attempt in range(3):
                response = session.post(url, json=payload, headers=headers, timeout=timeout)
                if 200 <= response.status_code < 300:
                    data = self._safe_response_json(response)
                    if not data:
                        raise GptFreeIdentityError("gptfree_upstream_invalid_json")
                    return data
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt >= 2:
                    raise GptFreeIdentityError(
                        f"gptfree_http_{response.status_code}:{self._safe_error_body(response)}"
                    )
                retry_after = str(response.headers.get("Retry-After") or "").strip()
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else float(2 ** attempt)
                time.sleep(max(0.5, min(delay, 15.0)))
            raise GptFreeIdentityError("gptfree_upstream_retry_exhausted")
        finally:
            session.close()

    def register_runtime(
        self,
        access_token: str,
        *,
        account_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        claims = validate_access_token(access_token)
        private_key_b64, public_key_ssh = generate_ed25519_keypair()
        payload = {
            "abom": {
                "agent_version": AGENT_VERSION,
                "agent_harness_id": AGENT_HARNESS_ID,
                "running_location": RUNNING_LOCATION,
            },
            "agent_public_key": public_key_ssh,
            "capabilities": list(DEFAULT_CAPABILITIES),
            "ttl": None,
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if claims["chatgpt_account_is_fedramp"]:
            headers["X-OpenAI-Fedramp"] = "true"
        data = self._post_json(
            RUNTIME_REGISTER_URL,
            payload,
            headers,
            account_context=account_context,
        )
        runtime_id = str(data.get("agent_runtime_id") or "").strip()
        if not runtime_id:
            raise GptFreeIdentityError("gptfree_runtime_id_missing")

        identity_id = uuid.uuid4().hex
        now = utc_timestamp()
        record = {
            "identity_id": identity_id,
            "agent_runtime_id": runtime_id,
            "agent_private_key_encrypted": self._encrypt(private_key_b64),
            "account_id": claims["account_id"],
            "chatgpt_user_id": claims["chatgpt_user_id"],
            "email": claims["email"],
            "plan_type": claims["plan_type"],
            "chatgpt_account_is_fedramp": claims["chatgpt_account_is_fedramp"],
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._identities[identity_id] = record
            self._save_locked()
        return self.public_identity(identity_id)

    def public_identity(self, identity_id: str) -> dict[str, Any]:
        with self._lock:
            record = dict(self._identities.get(str(identity_id or "")) or {})
        if not record:
            raise GptFreeIdentityError("gptfree_identity_not_found")
        record.pop("agent_private_key_encrypted", None)
        record["private_key_saved"] = True
        record["task_registered"] = str(identity_id or "") in self._tasks
        return record

    def get_identity(self, identity_id: str) -> dict[str, Any]:
        with self._lock:
            record = dict(self._identities.get(str(identity_id or "")) or {})
        if not record:
            raise GptFreeIdentityError("gptfree_identity_not_found")
        encrypted = str(record.pop("agent_private_key_encrypted", "") or "")
        if not encrypted:
            raise GptFreeIdentityError("gptfree_private_key_missing")
        record["agent_private_key"] = self._decrypt(encrypted)
        return record

    def register_task(
        self,
        identity_id: str,
        *,
        force: bool = False,
        account_context: dict[str, Any] | None = None,
    ) -> str:
        identity_id = str(identity_id or "").strip()
        if not identity_id:
            raise GptFreeIdentityError("gptfree_identity_id_missing")
        with self._lock:
            if not force and self._tasks.get(identity_id):
                return self._tasks[identity_id]
            task_lock = self._task_locks.setdefault(identity_id, threading.Lock())
        with task_lock:
            with self._lock:
                if not force and self._tasks.get(identity_id):
                    return self._tasks[identity_id]
            record = self.get_identity(identity_id)
            runtime_id = str(record["agent_runtime_id"])
            timestamp = utc_timestamp()
            data = self._post_json(
                f"{AUTH_API_BASE}/v1/agent/{runtime_id}/task/register",
                {
                    "timestamp": timestamp,
                    "signature": sign_message(
                        str(record["agent_private_key"]),
                        f"{runtime_id}:{timestamp}",
                    ),
                },
                {"Accept": "application/json", "Content-Type": "application/json"},
                account_context=account_context,
            )
            task_id = str(data.get("task_id") or data.get("taskId") or "").strip()
            encrypted_task_id = data.get("encrypted_task_id") or data.get("encryptedTaskId")
            if not task_id and encrypted_task_id:
                task_id = decrypt_task_id(
                    str(record["agent_private_key"]),
                    str(encrypted_task_id),
                )
            if not task_id:
                raise GptFreeIdentityError("gptfree_task_id_missing")
            with self._lock:
                self._tasks[identity_id] = task_id
            return task_id

    def authorization(
        self,
        identity_id: str,
        *,
        force_task: bool = False,
        account_context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        record = self.get_identity(identity_id)
        task_id = self.register_task(identity_id, force=force_task, account_context=account_context)
        assertion = build_agent_assertion(
            str(record["agent_runtime_id"]),
            task_id,
            str(record["agent_private_key"]),
        )
        headers = {
            "Authorization": f"AgentAssertion {assertion}",
            "ChatGPT-Account-ID": str(record["account_id"]),
        }
        if record.get("chatgpt_account_is_fedramp"):
            headers["X-OpenAI-Fedramp"] = "true"
        return headers

    def invalidate_task(self, identity_id: str) -> None:
        with self._lock:
            self._tasks.pop(str(identity_id or ""), None)


class _LazyGptFreeIdentityService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._service: GptFreeIdentityService | None = None

    def _get(self) -> GptFreeIdentityService:
        if self._service is not None:
            return self._service
        with self._lock:
            if self._service is None:
                self._service = GptFreeIdentityService()
            return self._service

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


gptfree_identity_service = _LazyGptFreeIdentityService()
