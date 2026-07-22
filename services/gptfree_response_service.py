from __future__ import annotations

import json
from typing import Any, Iterator

from curl_cffi import requests

from services.account_service import account_service
from services.gptfree_identity_service import GptFreeIdentityError, gptfree_identity_service
from services.proxy_service import proxy_settings
from utils.helper import gptfree_upstream_model, iter_sse_payloads


GPTFREE_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_INSTRUCTIONS = (
    "You are a helpful assistant. Answer the user directly and accurately. "
    "Do not claim to have used tools that are not present in the request."
)


class GptFreeResponseError(RuntimeError):
    pass


def is_gptfree_request_model(model: object) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized == "gptfree" or normalized.startswith("gptfree/")


def _safe_error(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return "upstream_error"
    if not isinstance(payload, dict):
        return "upstream_error"
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("message") or "upstream_error")[:200]
    return str(payload.get("code") or payload.get("message") or "upstream_error")[:200]


def _upstream_body(body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["model"] = gptfree_upstream_model(payload.get("model"))
    payload["stream"] = True
    payload.setdefault("store", False)
    payload.setdefault("instructions", DEFAULT_INSTRUCTIONS)
    if not isinstance(payload.get("reasoning"), dict):
        effort = str(payload.get("reasoning_effort") or payload.get("thinking_effort") or "").strip().lower()
        if effort:
            payload["reasoning"] = {"effort": "xhigh" if effort == "extended" else effort}
    for key in ("n", "prompt", "modalities", "reasoning_effort", "thinking_effort", "account_pool"):
        payload.pop(key, None)
    return payload


class GptFreeResponseService:
    def stream(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        attempted_tokens: set[str] = set()
        last_error: Exception | None = None
        for _attempt in range(3):
            token = account_service.get_text_access_token(
                attempted_tokens,
                source_type="gptfree",
                refresh=False,
            )
            if not token:
                break
            attempted_tokens.add(token)
            account = account_service.get_account(token) or {}
            identity_id = str(account.get("gptfree_identity_id") or "").strip()
            if not identity_id:
                last_error = GptFreeResponseError("gptfree_account_missing_identity")
                continue
            emitted = False
            try:
                for event in self._stream_account(body, token, account, identity_id):
                    emitted = True
                    yield event
                account_service.mark_text_used(token)
                return
            except (GptFreeIdentityError, GptFreeResponseError) as exc:
                if emitted:
                    raise
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise GptFreeResponseError("no available gptfree account")

    def _stream_account(
        self,
        body: dict[str, Any],
        token: str,
        account: dict[str, Any],
        identity_id: str,
    ) -> Iterator[dict[str, Any]]:
        del token  # Agent Identity requests intentionally do not transmit the Web access token.
        for auth_attempt in range(2):
            auth_headers = gptfree_identity_service.authorization(
                identity_id,
                force_task=auth_attempt > 0,
                account_context=account,
            )
            session = requests.Session(
                **proxy_settings.build_session_kwargs(account=account, impersonate="chrome", verify=True)
            )
            try:
                response = session.post(
                    GPTFREE_RESPONSES_URL,
                    json=_upstream_body(body),
                    headers={
                        **auth_headers,
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                        "Originator": "codex_cli_rs",
                        "User-Agent": "chatgpt2api-gptfree/1.0",
                    },
                    timeout=300,
                    stream=True,
                )
                if response.status_code in {401, 403} and auth_attempt == 0:
                    gptfree_identity_service.invalidate_task(identity_id)
                    continue
                if not (200 <= response.status_code < 300):
                    raise GptFreeResponseError(
                        f"gptfree_responses_http_{response.status_code}:{_safe_error(response)}"
                    )
                for payload in iter_sse_payloads(response):
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event
                return
            finally:
                session.close()
        raise GptFreeResponseError("gptfree_agent_assertion_rejected")


gptfree_response_service = GptFreeResponseService()
