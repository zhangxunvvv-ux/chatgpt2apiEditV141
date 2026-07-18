from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import mail_provider, openai_register


REGISTER_FILE = DATA_DIR / "register.json"


def _immediate_backoff_reason(error: object) -> str:
    text = str(error or "").lower()
    if "http 429" in text or "http_429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    return ""


def _serialize_outlook_pool(credentials: list[dict]) -> str:
    return "\n".join(
        f'{c["email"]}----{c.get("password", "")}----{c["client_id"]}----{c["refresh_token"]}' for c in credentials
    )


def _merge_outlook_pool(old_text: str, new_text: str) -> str:
    """合并已存邮箱池与新导入文本，按邮箱去重，新导入的同名邮箱覆盖旧凭据。"""
    merged: dict[str, dict] = {}
    for credential in mail_provider.parse_outlook_credentials(old_text or ""):
        merged[credential["email"].strip().lower()] = credential
    for credential in mail_provider.parse_outlook_credentials(new_text or ""):
        merged[credential["email"].strip().lower()] = credential
    return _serialize_outlook_pool(list(merged.values()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {
        **openai_register.config,
        "mode": "total",
        "target_quota": 100,
        "target_available": 10,
        "check_interval": 5,
        "failure_backoff_seconds": 1200,
        "enabled": False,
        "stats": {
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "threads": openai_register.config["threads"],
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
            "current_quota": 0,
            "current_available": 0,
            "consecutive_failures": 0,
            "retry_at": None,
            "pause_reason": "",
            "scheduler_restarts": 0,
        },
    }


def _safe_bool(value: object, fallback: bool) -> bool:
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


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg.pop("failure_backoff_threshold", None)
    cfg["failure_backoff_seconds"] = max(1, int(cfg.get("failure_backoff_seconds") or 1200))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    default_mail = _default_config()["mail"] if isinstance(_default_config().get("mail"), dict) else {}
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    cfg["mail"] = {**default_mail, **mail}
    cfg["mail"]["api_use_register_proxy"] = _safe_bool(cfg["mail"].get("api_use_register_proxy"), False)
    cfg["mail"].pop("proxy", None)
    providers = cfg["mail"].get("providers")
    if isinstance(providers, list):
        for provider in providers:
            if isinstance(provider, dict):
                provider.pop("domain_stats", None)
                if provider.get("type") == "tempmail_lol":
                    provider["domain"] = mail_provider.parse_tempmail_domains(provider.get("domain"))
                    provider.pop("domain_cooldown_threshold", None)
                    provider.pop("domain_cooldown_seconds", None)
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    if not cfg["enabled"]:
        stats["running"] = 0
        stats["retry_at"] = None
        stats["pause_reason"] = ""
    cfg["stats"] = stats
    return cfg


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get(self) -> dict:
        with self._lock:
            snapshot = json.loads(json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False))
        self._redact_outlook_pools(snapshot)
        self._attach_tempmail_domain_stats(snapshot)
        return snapshot

    @staticmethod
    def _attach_tempmail_domain_stats(snapshot: dict) -> None:
        mail = snapshot.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        stats = mail_provider.tempmail_domain_stats_snapshot()
        for provider in mail["providers"]:
            if isinstance(provider, dict) and provider.get("type") == "tempmail_lol":
                provider["domain_stats"] = stats

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        """把 outlook_token 邮箱池里的密码/refresh_token 从对外输出中抹掉，仅保留脱敏预览与统计。

        mailboxes 改为只写导入框（输出为空），避免把密码与 refresh_token 通过 GET/SSE 反复广播。
        """
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _merge_outlook_pools(self, updates: dict) -> None:
        """对 outlook_token provider：把前端新导入的 mailboxes 与已存池按邮箱合并去重。

        前端 mailboxes 是只写导入框，留空表示不改动；填入的新行追加/覆盖已存凭据。
        按数组下标与已存的同类型 provider 对齐。
        """
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            old = old_providers[index] if index < len(old_providers) and isinstance(old_providers[index], dict) else {}
            old_text = str(old.get("mailboxes") or "") if old.get("type") == "outlook_token" else ""
            new_text = str(provider.get("mailboxes") or "")
            provider["mailboxes"] = _merge_outlook_pool(old_text, new_text) if (old_text or new_text) else ""
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)

    def _prune_unused_outlook_pools(self) -> int:
        mail = self._config.get("mail")
        if not isinstance(mail, dict):
            return 0
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return 0
        total_removed = 0
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)
        return total_removed

    def _apply_updates_locked(self, updates: dict) -> None:
        self._merge_outlook_pools(updates)
        self._config = _normalize({**self._config, **updates})
        self._drop_mail_proxy()
        openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._apply_updates_locked(updates)
            self._save()
            return self.get()

    def start(self, updates: dict | None = None) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            if updates:
                self._apply_updates_locked(updates)
            self._config["enabled"] = True
            self._drop_mail_proxy()
            self._logs = []
            try:
                metrics = self._pool_metrics()
            except Exception as error:
                previous = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
                metrics = {
                    "current_quota": int(previous.get("current_quota") or 0),
                    "current_available": int(previous.get("current_available") or 0),
                }
                self._append_log(
                    f"号池初始指标读取失败（{type(error).__name__}），调度器将在后台自动重试",
                    "yellow",
                )
            self._config["stats"] = {
                "job_id": uuid.uuid4().hex,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                **metrics,
                "consecutive_failures": 0,
                "retry_at": None,
                "pause_reason": "",
                "scheduler_restarts": 0,
                "started_at": _now(),
                "updated_at": _now(),
            }
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}", "yellow")
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                "elapsed_seconds": 0,
                "avg_seconds": 0,
                "success_rate": 0,
                "consecutive_failures": 0,
                "retry_at": None,
                "pause_reason": "",
                "scheduler_restarts": 0,
                **self._pool_metrics(),
                "updated_at": _now(),
            }
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            with self._lock:
                removed = self._prune_unused_outlook_pools()
                openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
                self._save()
                self._append_log(f"已清空 Outlook 邮箱池未使用邮箱，移除 {removed} 个", "yellow")
            return self.get()
        scope = "failed" if str(scope) == "failed" else "all"
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        with self._lock:
            self._append_log(
                f"已重置 Outlook 邮箱池状态（范围={'仅失败/占用' if scope == 'failed' else '全部'}），清除 {cleared} 条记录",
                "yellow",
            )
        return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if item.get("status") == "正常"]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _is_enabled(self) -> bool:
        with self._lock:
            return bool(self._config.get("enabled"))

    def _target_reached(self, cfg: dict, successful: int, in_flight: int = 0) -> bool:
        mode = str(cfg.get("mode") or "total")
        if mode == "total":
            # A failed attempt must not consume the requested successful-account count.
            return successful + in_flight >= int(cfg.get("total") or 1)
        metrics = self._pool_metrics()
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return False

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _wait_for_retry(self, seconds: int, reason: str) -> bool:
        delay = max(1, int(seconds))
        deadline = time.monotonic() + delay
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        self._bump(retry_at=retry_at, pause_reason=reason, running=0)
        while self._is_enabled():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._bump(retry_at=None, pause_reason="", consecutive_failures=0)
                self._append_log("失败冷却结束，注册调度自动恢复", "yellow")
                return True
            time.sleep(min(1.0, remaining))
        self._bump(retry_at=None, pause_reason="", running=0)
        return False

    def _run_loop(self) -> None:
        threads = int(self.get()["threads"])
        initial_stats = self.get().get("stats") or {}
        done = int(initial_stats.get("done") or 0)
        success = int(initial_stats.get("success") or 0)
        fail = int(initial_stats.get("fail") or 0)
        consecutive_failures = int(initial_stats.get("consecutive_failures") or 0)
        immediate_pause_reason = ""
        task_index = done
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self.get()
                while (
                    self._is_enabled()
                    and not immediate_pause_reason
                    and len(futures) < threads
                    and not self._target_reached(cfg, success, len(futures))
                ):
                    task_index += 1
                    futures.add(executor.submit(openai_register.worker, task_index))
                self._bump(
                    running=len(futures),
                    done=done,
                    success=success,
                    fail=fail,
                    consecutive_failures=consecutive_failures,
                )
                if not futures:
                    if not self._is_enabled():
                        break
                    if str(cfg.get("mode") or "total") == "total" and self._target_reached(cfg, success):
                        break
                    if immediate_pause_reason:
                        delay = max(1, int(cfg.get("failure_backoff_seconds") or 1200))
                        self._append_log(
                            f"检测到 HTTP 429/明确限流，暂停新注册 {delay} 秒；冷却后自动恢复",
                            "yellow",
                        )
                        if not self._wait_for_retry(delay, immediate_pause_reason):
                            break
                        consecutive_failures = 0
                        immediate_pause_reason = ""
                        continue
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        ok = bool(isinstance(result, dict) and result.get("ok"))
                        failure_error = str(result.get("error") or "") if isinstance(result, dict) else ""
                    except Exception as error:
                        ok = False
                        failure_error = str(error)
                        self._append_log(
                            f"注册工作线程异常（{type(error).__name__}），已计为失败并继续调度",
                            "red",
                        )
                    if ok:
                        success += 1
                        consecutive_failures = 0
                        immediate_pause_reason = ""
                    else:
                        fail += 1
                        consecutive_failures += 1
                        reason = _immediate_backoff_reason(failure_error)
                        if reason:
                            immediate_pause_reason = reason
                self._bump(
                    running=len(futures),
                    done=done,
                    success=success,
                    fail=fail,
                    consecutive_failures=consecutive_failures,
                )
        self._bump(
            running=0,
            done=done,
            success=success,
            fail=fail,
            consecutive_failures=consecutive_failures,
            retry_at=None,
            pause_reason="",
            finished_at=_now(),
        )
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")

    def _run(self) -> None:
        while self._is_enabled():
            try:
                self._run_loop()
                return
            except Exception as error:
                if not self._is_enabled():
                    break
                snapshot = self.get()
                stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), dict) else {}
                restarts = int(stats.get("scheduler_restarts") or 0) + 1
                delay = max(1, int(snapshot.get("failure_backoff_seconds") or 1200))
                self._bump(running=0, scheduler_restarts=restarts)
                self._append_log(
                    f"注册调度器异常（{type(error).__name__}），不会停止任务；{delay} 秒后自动重试",
                    "red",
                )
                if not self._wait_for_retry(delay, "scheduler_error"):
                    break
        self._bump(running=0, retry_at=None, pause_reason="")


register_service = RegisterService(REGISTER_FILE)
