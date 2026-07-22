from __future__ import annotations

import threading
import time

from services.account_service import account_service
from services.gptfree_identity_service import gptfree_identity_service
from services.register import openai_register, reference_register


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
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None


def _emit_log(text: str, color: str = "") -> None:
    if register_log_sink:
        register_log_sink(text, color)


def worker(index: int, stop_event: threading.Event | None = None, generation: int = 0) -> dict:
    started = time.time()
    registrar = reference_register.ReferencePlatformRegistrar(
        config["proxy"],
        stop_event=stop_event,
        mail_config=config["mail"],
    )
    with openai_register.thread_log_sink(_emit_log):
        try:
            openai_register.step(index, f"gptFree task started generation={generation}")
            result = registrar.register(index)
            registrar._ensure_active()
            openai_register.step(index, "gptFree: validating access token and registering Agent Runtime")
            identity = gptfree_identity_service.register_runtime(
                str(result.get("access_token") or ""),
                account_context=result,
            )
            result.update({
                "source_type": "gptfree",
                "registration_engine": "gptfree",
                "gptfree_identity_id": str(identity["identity_id"]),
                "agent_runtime_id": str(identity["agent_runtime_id"]),
            })
            account_service.add_account_items([result])
            access_token = str(result["access_token"])
            refresh_result = account_service.refresh_accounts([access_token])
            if refresh_result.get("errors"):
                openai_register.step(index, "gptFree account saved; pool metadata refresh will retry later", "yellow")

            elapsed = time.time() - started
            with stats_lock:
                stats["done"] += 1
                stats["success"] += 1
                average = (time.time() - stats["start_time"]) / max(1, stats["success"])
            openai_register.log(
                f"{result['email']} gptFree registration succeeded in {elapsed:.1f}s "
                f"(average {average:.1f}s, runtime_id_len={len(str(identity['agent_runtime_id']))}, private_key_saved=true)",
                "green",
            )
            return {"ok": True, "index": index, "generation": generation, "result": result}
        except openai_register.RegistrationStopped:
            elapsed = time.time() - started
            openai_register.step(index, f"gptFree task stopped after {elapsed:.1f}s", "yellow")
            return {"ok": False, "cancelled": True, "index": index, "generation": generation}
        except Exception as error:
            elapsed = time.time() - started
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "cancelled": True, "index": index, "generation": generation}
            with stats_lock:
                stats["done"] += 1
                stats["fail"] += 1
            openai_register.log(
                f"gptFree task {index} failed after {elapsed:.1f}s: {error}",
                "red",
            )
            return {
                "ok": False,
                "index": index,
                "generation": generation,
                "error": str(error),
            }
        finally:
            registrar.close()
