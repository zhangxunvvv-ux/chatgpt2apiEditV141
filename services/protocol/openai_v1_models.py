from __future__ import annotations

import copy
import time
from threading import Lock
from typing import Any

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL


MODEL_CACHE_TTL_SECONDS = 5 * 60
MAX_AUTH_MODEL_ATTEMPTS = 3
LATEST_CHAT_MODELS = ("gpt-5-6-sol", "gpt-5-6-Luna")
GPTFREE_MODELS = (
    "gptfree",
    "gptfree/gpt-5.6-sol",
    "gptfree/gpt-5.6-luna",
    "gptfree/gpt-5.6-terra",
)

_model_cache: dict[str, Any] | None = None
_model_cache_expires_at = 0.0
_model_cache_lock = Lock()


def _model_entry(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "chatgpt2api",
        "permission": [],
        "root": model_id,
        "parent": None,
    }


def _fetch_upstream_models() -> dict[str, Any]:
    attempted_tokens: set[str] = set()
    for _attempt in range(MAX_AUTH_MODEL_ATTEMPTS):
        try:
            access_token = account_service.get_text_access_token(
                set(attempted_tokens),
                source_type="default",
            )
        except Exception:
            continue
        if not access_token or access_token in attempted_tokens:
            break
        attempted_tokens.add(access_token)
        backend = OpenAIBackendAPI(access_token=access_token)
        try:
            result = backend.list_models()
            if isinstance(result.get("data"), list):
                return result
        except Exception:
            pass
        finally:
            backend.close()

    backend = OpenAIBackendAPI()
    try:
        return backend.list_models()
    finally:
        backend.close()


def _get_upstream_models() -> dict[str, Any]:
    global _model_cache, _model_cache_expires_at

    now = time.monotonic()
    with _model_cache_lock:
        if _model_cache is not None and now < _model_cache_expires_at:
            return copy.deepcopy(_model_cache)
        result = _fetch_upstream_models()
        _model_cache = copy.deepcopy(result)
        _model_cache_expires_at = time.monotonic() + MODEL_CACHE_TTL_SECONDS
        return copy.deepcopy(result)


def _clear_model_cache() -> None:
    global _model_cache, _model_cache_expires_at

    with _model_cache_lock:
        _model_cache = None
        _model_cache_expires_at = 0.0


def list_models() -> dict[str, Any]:
    result = _get_upstream_models()
    data = result.get("data")
    if not isinstance(data, list):
        return result

    existing = {
        str(item.get("id") or "").strip(): item
        for item in data
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    preferred_models = (*LATEST_CHAT_MODELS, *GPTFREE_MODELS)
    latest = [existing.get(model) or _model_entry(model) for model in preferred_models]
    remaining = [
        item
        for item in data
        if not isinstance(item, dict)
           or str(item.get("id") or "").strip() not in preferred_models
    ]
    auto_index = next(
        (
            index + 1
            for index, item in enumerate(remaining)
            if isinstance(item, dict) and str(item.get("id") or "").strip() == "auto"
        ),
        0,
    )
    data[:] = remaining[:auto_index] + latest + remaining[auto_index:]

    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        dynamic_models.add("gpt-image-2")
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append(_model_entry(model))
    return result
