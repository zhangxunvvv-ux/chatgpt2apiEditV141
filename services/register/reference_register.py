from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.account_service import account_service
from services.register import mail_provider, openai_register

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


class ReferencePlatformRegistrar(openai_register.PlatformRegistrar):
    """Alternative URL-driven registration flow adapted from the shared project."""

    def __init__(
        self,
        proxy: str = "",
        stop_event: threading.Event | None = None,
        mail_config: dict | None = None,
    ) -> None:
        super().__init__(proxy, stop_event=stop_event, mail_config=mail_config)
        self._is_mobile = random.random() < 0.35
        major = random.randint(138, 146)
        build = random.randint(6000, 9999)
        patch = random.randint(50, 220)
        if self._is_mobile:
            android = random.choice((12, 13, 14, 15))
            model = random.choice(("Pixel 8", "Pixel 8 Pro", "SM-S928B", "CPH2487", "MI 13"))
            self._profile_user_agent = (
                f"Mozilla/5.0 (Linux; Android {android}; {model}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Mobile Safari/537.36"
            )
            self._profile_platform = '"Android"'
            self._profile_platform_version = f'"{android}.0.0"'
            self._profile_mobile = "?1"
        else:
            self._profile_user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36"
            )
            self._profile_platform = '"Windows"'
            self._profile_platform_version = '"15.0.0"'
            self._profile_mobile = "?0"
        self._profile_sec_ch_ua = (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not.A/Brand";v="24"'
        )
        self._profile_sec_ch_ua_full = (
            f'"Google Chrome";v="{major}.0.{build}.{patch}", '
            f'"Chromium";v="{major}.0.{build}.{patch}", "Not.A/Brand";v="24.0.0.0"'
        )

    def _browser_user_agent(self) -> str:
        return self._profile_user_agent

    def _browser_sec_ch_ua(self) -> str:
        return self._profile_sec_ch_ua

    def _profile_headers(self) -> dict[str, str]:
        return {
            "user-agent": self._profile_user_agent,
            "sec-ch-ua": self._profile_sec_ch_ua,
            "sec-ch-ua-full-version-list": self._profile_sec_ch_ua_full,
            "sec-ch-ua-mobile": self._profile_mobile,
            "sec-ch-ua-platform": self._profile_platform,
            "sec-ch-ua-platform-version": self._profile_platform_version,
        }

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = super()._navigate_headers(referer)
        headers.update(self._profile_headers())
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = super()._json_headers(referer)
        headers.update(self._profile_headers())
        return headers

    def _otp_fetch_headers(self) -> dict[str, str]:
        headers = super()._otp_fetch_headers()
        headers.update(self._profile_headers())
        return headers

    def _prepare_reference_code_baseline(self, index: int, mailbox: dict[str, Any], label: str) -> None:
        try:
            mail_provider.prepare_code_baseline(self.mail_config, mailbox)
            openai_register.step(index, f"新注册：{label}邮箱基线已记录")
        except Exception as exc:
            openai_register.step(index, f"新注册：邮箱基线记录失败，继续注册: {str(exc)[:160]}", "yellow")

    def _request_otp_validation(self, code: str, index: int):
        """Match the reference flow's OTP request without changing its device profile mid-session."""
        url = f"{openai_register.auth_base}/api/accounts/email-otp/validate"

        def submit():
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "origin": openai_register.auth_base,
                "referer": f"{openai_register.auth_base}/email-verification",
                "user-agent": self._browser_user_agent(),
            }
            headers = openai_register._headers_with_clearance(
                headers,
                url,
                self.proxy,
                self.clearance_user_agent,
            )
            return openai_register.request_with_local_retry(
                self.session,
                "post",
                url,
                json={"code": code},
                headers=headers,
                verify=False,
            )

        response, error = submit()
        if openai_register._is_cloudflare_challenge(response):
            bundle = self._refresh_cloudflare_clearance(openai_register.auth_base, index)
            if bundle is None:
                return response, openai_register._cloudflare_block_message(
                    response,
                    reason=self.clearance_failure_reason,
                )
            response, error = submit()
        return response, error

    def _send_email_otp_reference(self, index: int, mailbox: dict[str, Any]) -> None:
        self._ensure_active()
        self._prepare_reference_code_baseline(index, mailbox, "发送验证码前")

        url = f"{openai_register.auth_base}/api/accounts/email-otp/send"

        def submit():
            headers = {
                "accept": "application/json",
                "referer": f"{openai_register.auth_base}/create-account/password",
                **self._profile_headers(),
            }
            headers = openai_register._headers_with_clearance(
                headers,
                url,
                self.proxy,
                self.clearance_user_agent,
            )
            return openai_register.request_with_local_retry(
                self.session,
                "get",
                url,
                headers=headers,
                allow_redirects=True,
                verify=False,
            )

        response, error = submit()
        if openai_register._is_cloudflare_challenge(response):
            bundle = self._refresh_cloudflare_clearance(openai_register.auth_base, index)
            if bundle is None:
                raise RuntimeError(
                    openai_register._cloudflare_block_message(
                        response,
                        reason=self.clearance_failure_reason,
                    )
                )
            response, error = submit()
        if response is None or response.status_code not in (200, 302):
            raise RuntimeError(error or f"reference_send_otp_http_{getattr(response, 'status_code', 'unknown')}")
        openai_register.step(index, "新注册：发送验证码完成")

    def register(self, index: int) -> dict[str, str]:
        self._ensure_active()
        openai_register.step(index, "新注册：开始创建邮箱")
        mailbox = openai_register.create_mailbox(
            register_proxy=self.proxy,
            mail_config=self.mail_config,
        )
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        openai_register.step(index, f"新注册：邮箱创建完成[{label}]: {email}")

        try:
            self._ensure_active()
            # login_hint/authorize may send the passwordless OTP before
            # authorize/continue returns, so establish the mailbox boundary first.
            self._prepare_reference_code_baseline(index, mailbox, "注册开始前")
            openai_register.step(
                index,
                f"新注册：设备画像={'mobile' if self._is_mobile else 'desktop'}",
            )
            self._chatgpt_authorize(email, index, include_login_hint=True)
            if self.chatgpt_authorize_landed_path == "/email-verification":
                mode, verification_mode = "otp", "passwordless_signup"
                self.signup_verification_mode = verification_mode
                openai_register.step(index, "新注册：authorize 已进入验证码页，不重复提交邮箱")
            elif self.chatgpt_authorize_landed_path == "/create-account/password":
                mode, verification_mode = "password", ""
                openai_register.step(index, "新注册：authorize 已进入密码页，不重复提交邮箱")
            else:
                mode, verification_mode = self._authorize_signup(
                    email,
                    index,
                    screen_hint="login_or_signup",
                )
            password = ""
            if mode == "password":
                password = openai_register._random_password()
                self._register_user(email, password, index)
                self._send_email_otp_reference(index, mailbox)
            else:
                if verification_mode == "passwordless_login":
                    raise RuntimeError("signup_email_already_registered")
                if verification_mode != "passwordless_signup":
                    raise RuntimeError(
                        "reference_signup_otp_mode_unconfirmed: "
                        f"email_verification_mode={verification_mode or 'unknown'}"
                    )
                openai_register.step(index, "新注册：提交邮箱已触发验证码，直接等待首封邮件")

            self._validate_mailbox_otp(mailbox, index)
            first_name, last_name = openai_register._random_name()
            self._create_account(
                f"{first_name} {last_name}",
                openai_register._random_birthdate(),
                index,
            )
            chatgpt_session = self._finish_chatgpt_registration(index)
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise

        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            "access_token": str(chatgpt_session.get("access_token") or "").strip(),
            "platform_access_token": "",
            "refresh_token": "",
            "id_token": "",
            "session_token": str(chatgpt_session.get("session_token") or "").strip(),
            "cookie": str(chatgpt_session.get("cookie") or "").strip(),
            "source_type": "web",
            "registration_engine": "reference",
            **self._account_environment(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int, stop_event: threading.Event | None = None, generation: int = 0) -> dict:
    start = time.time()
    registrar = ReferencePlatformRegistrar(
        config["proxy"],
        stop_event=stop_event,
        mail_config=config["mail"],
    )
    with openai_register.thread_log_sink(_emit_log):
        try:
            openai_register.step(index, f"新注册任务启动 generation={generation}")
            result = registrar.register(index)
            cost = time.time() - start
            access_token = str(result["access_token"])
            account_service.add_account_items([result])
            refresh_result = account_service.refresh_accounts([access_token])
            if refresh_result.get("errors"):
                openai_register.step(
                    index,
                    f"新注册账号已保存，状态刷新稍后重试: {refresh_result['errors']}",
                    "yellow",
                )
            with stats_lock:
                stats["done"] += 1
                stats["success"] += 1
                avg = (time.time() - stats["start_time"]) / stats["success"]
            openai_register.log(
                f'{result["email"]} 新注册成功，本次耗时{cost:.1f}s，平均耗时{avg:.1f}s',
                "green",
            )
            return {"ok": True, "index": index, "generation": generation, "result": result}
        except openai_register.RegistrationStopped:
            cost = time.time() - start
            openai_register.step(index, f"新注册任务已停止，本次耗时{cost:.1f}s", "yellow")
            return {"ok": False, "cancelled": True, "index": index, "generation": generation}
        except Exception as error:
            cost = time.time() - start
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "cancelled": True, "index": index, "generation": generation}
            with stats_lock:
                stats["done"] += 1
                stats["fail"] += 1
            openai_register.log(f"新注册任务{index}失败，本次耗时{cost:.1f}s，原因: {error}", "red")
            return {
                "ok": False,
                "index": index,
                "generation": generation,
                "error": str(error),
            }
        finally:
            registrar.close()
