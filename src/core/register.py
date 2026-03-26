"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import re
import json
import time
import logging
import secrets
import string
from typing import Optional, Dict, Any, Tuple, Callable, List
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .openai.oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


@dataclass
class OTPValidationResult:
    """验证码校验结果"""
    success: bool
    page_type: str = ""
    continue_url: str = ""
    method: str = ""
    response_data: Dict[str, Any] = None
    candidate_urls: List[str] = None
    error_message: str = ""


@dataclass
class OAuthContinuationResult:
    """登录降级后恢复 OAuth 的结果"""
    success: bool
    workspace_id: str = ""
    callback_url: str = ""
    error_message: str = ""


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._post_create_account_start_urls: List[str] = []  # create_account 后可尝试的跳转起点
        self._last_create_account_error: Dict[str, Any] = {}  # 记录 create_account 的失败细节

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"成功创建邮箱: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已生成: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _reset_auth_session(self, clear_oauth: bool = False) -> bool:
        """重置认证会话，避免旧流程状态干扰新 OAuth 链路"""
        try:
            try:
                self.http_client.close()
            except Exception:
                pass

            self.http_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
            self.session = self.http_client.session

            if clear_oauth:
                self.oauth_start = None

            self._log("认证会话已重置")
            return True
        except Exception as e:
            self._log(f"重置认证会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        if not self.oauth_start:
            return None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                did = self.session.cookies.get("oai-did")

                if did:
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self._reset_auth_session(clear_oauth=False)

        return None

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            sen_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'

            response = self.http_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )

            if response.status_code == 200:
                sen_token = response.json().get("token")
                self._log(f"Sentinel token 获取成功")
                return sen_token
            else:
                self._log(f"Sentinel 检查失败: {response.status_code}", "warning")
                return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        screen_hint: str = "signup"
    ) -> SignupFormResult:
        """
        提交注册表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            safe_screen_hint = (screen_hint or "signup").strip()
            signup_body = json.dumps({
                "username": {"value": self.email, "kind": "email"},
                "screen_hint": safe_screen_hint,
            })

            referer = "https://auth.openai.com/create-account"
            if safe_screen_hint == "login":
                referer = "https://auth.openai.com/log-in"

            headers = {
                "referer": referer,
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = f'{{"p": "", "t": "", "c": "{sen_token}", "id": "{did}", "flow": "authorize_continue"}}'
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=signup_body,
            )

            self._log(f"提交注册表单 screen_hint: {safe_screen_hint}")
            self._log(f"提交注册表单状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            # 解析响应判断账号状态
            try:
                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                # 判断是否为已注册账号
                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]

                if is_existing:
                    self._log(f"检测到已注册账号，将自动切换到登录流程")
                    self._is_existing_account = True

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                # 无法解析，默认成功
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"提交注册表单失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _verify_password_for_login(self, password: str) -> bool:
        """登录流程校验密码（/accounts/password/verify）"""
        try:
            verify_body = json.dumps({"password": password})

            response = self.session.post(
                OPENAI_API_ENDPOINTS["verify_password"],
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=verify_body,
            )

            self._log(f"密码校验状态: {response.status_code}")
            if response.status_code != 200:
                self._log(f"密码校验失败: {response.text[:200]}", "warning")
                return False
            return True
        except Exception as e:
            self._log(f"密码校验异常: {e}", "warning")
            return False

    def _send_passwordless_login_otp(self) -> bool:
        """登录流程走 passwordless/send-otp 触发验证码"""
        try:
            self._otp_sent_at = time.time()

            response = self.session.post(
                OPENAI_API_ENDPOINTS["passwordless_send_otp"],
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json",
                },
            )

            self._log(f"Passwordless OTP 发送状态: {response.status_code}")
            try:
                self._log_response_snapshot("Passwordless OTP", response.json())
            except Exception:
                self._log_response_snapshot("Passwordless OTP", response.text)

            if response.status_code != 200:
                self._log(f"Passwordless OTP 发送失败: {response.text[:200]}", "warning")
                return False

            return True
        except Exception as e:
            self._log(f"发送 Passwordless OTP 失败: {e}", "warning")
            return False

    def _register_password(self) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")

                    # 检测邮箱已注册的情况
                    if "already" in error_msg.lower() or "exists" in error_msg.lower() or error_code == "user_exists":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        # 标记此邮箱为已注册状态
                        self._mark_email_as_registered()
                except Exception:
                    pass

                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _log_response_snapshot(self, step_name: str, body: Any):
        """打印响应体中的关键信息，便于定位 OpenAI 页面流转变化"""
        try:
            if isinstance(body, dict):
                keys = sorted(list(body.keys()))
                self._log(f"{step_name} 响应 keys: {keys}")

                page = body.get("page") or {}
                if isinstance(page, dict):
                    page_type = str(page.get("type") or "").strip()
                    if page_type:
                        self._log(f"{step_name} page.type: {page_type}")

                method = str(body.get("method") or "").strip()
                if method:
                    self._log(f"{step_name} method: {method}")

                continue_url = str(body.get("continue_url") or "").strip()
                if continue_url:
                    self._log(f"{step_name} continue_url: {continue_url[:120]}...")
                return

            text = str(body or "").strip()
            if text:
                self._log(f"{step_name} 响应片段: {text[:200]}")
        except Exception as e:
            self._log(f"{step_name} 响应摘要失败: {e}", "warning")

    def _send_verification_code(
        self,
        referer: str = "https://auth.openai.com/create-account/password",
        context: str = ""
    ) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()
            label = f"{context}验证码" if context else "验证码"

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": referer,
                    "accept": "application/json",
                },
            )

            self._log(f"{label}发送状态: {response.status_code}")
            if response.status_code != 200:
                self._log(f"{label}发送失败: {response.text[:200]}", "warning")
            return response.status_code == 200

        except Exception as e:
            label = f"{context}验证码" if context else "验证码"
            self._log(f"发送{label}失败: {e}", "error")
            return False

    def _get_verification_code(self, timeout: int = 120) -> Optional[str]:
        """获取验证码"""
        try:
            self._log(f"正在等待邮箱 {self.email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=timeout,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )

            if code:
                self._log(f"成功获取验证码: {code}")
                return code
            else:
                self._log("等待验证码超时", "error")
                return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(
        self,
        code: str,
        context: str = "",
        referer: str = "https://auth.openai.com/email-verification"
    ) -> OTPValidationResult:
        """验证验证码"""
        try:
            code_body = f'{{"code":"{code}"}}'
            label = f"{context}验证码" if context else "验证码"

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": referer,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"{label}校验状态: {response.status_code}")

            response_data: Optional[Dict[str, Any]] = None
            page_type = ""
            continue_url = ""
            method = ""

            try:
                response_data = response.json()
                self._log_response_snapshot(label, response_data)
                page = response_data.get("page") or {}
                if isinstance(page, dict):
                    page_type = str(page.get("type") or "").strip()
                continue_url = str(response_data.get("continue_url") or "").strip()
                method = str(response_data.get("method") or "").strip()
            except Exception:
                self._log_response_snapshot(label, response.text)

            candidate_urls = self._collect_candidate_urls_from_response(response)
            if candidate_urls:
                self._log(
                    f"{label}候选继续地址({len(candidate_urls)}): "
                    f"{candidate_urls[0][:120]}..."
                )

            if response.status_code != 200:
                return OTPValidationResult(
                    success=False,
                    page_type=page_type,
                    continue_url=continue_url,
                    method=method,
                    response_data=response_data,
                    candidate_urls=candidate_urls,
                    error_message=response.text[:200],
                )

            return OTPValidationResult(
                success=True,
                page_type=page_type,
                continue_url=continue_url,
                method=method,
                response_data=response_data,
                candidate_urls=candidate_urls,
            )

        except Exception as e:
            self._log(f"验证{label}失败: {e}", "error")
            return OTPValidationResult(success=False, error_message=str(e))

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            self._last_create_account_error = {}
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")

            if response.status_code != 200:
                error_code = ""
                error_message = ""
                error_type = ""
                try:
                    error_json = response.json()
                    error = error_json.get("error") or {}
                    if isinstance(error, dict):
                        error_code = str(error.get("code") or "").strip()
                        error_message = str(error.get("message") or "").strip()
                        error_type = str(error.get("type") or "").strip()
                except Exception:
                    pass

                self._last_create_account_error = {
                    "status_code": response.status_code,
                    "error_code": error_code,
                    "error_message": error_message,
                    "error_type": error_type,
                    "response_text": response.text[:500],
                }
                self._log(f"账户创建失败: {response.text[:200]}", "warning")
                return False

            try:
                self._log_response_snapshot("create_account", response.json())
            except Exception:
                self._log_response_snapshot("create_account", response.text)

            # 收集 create_account 后可用的跳转起点，供新流程无 workspace 时兜底
            self._post_create_account_start_urls = self._collect_candidate_urls_from_response(response)
            if self._post_create_account_start_urls:
                self._log(
                    f"create_account 跳转起点候选({len(self._post_create_account_start_urls)}): "
                    f"{self._post_create_account_start_urls[0][:100]}..."
                )
            else:
                self._log("create_account 响应中未提取到跳转起点", "warning")

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _extract_candidate_urls_from_text(self, text: str) -> List[str]:
        """从 HTML/文本中提取潜在继续地址"""
        import urllib.parse

        def add_url(candidates: List[str], raw: Optional[str]):
            v = str(raw or "").strip()
            if not v:
                return
            if v.startswith("http://") or v.startswith("https://"):
                candidates.append(v)
                return
            if v.startswith("/"):
                candidates.append(urllib.parse.urljoin("https://auth.openai.com", v))

        if not text:
            return []

        candidates: List[str] = []
        normalized_text = str(text).replace("\\/", "/")

        for m in re.findall(r"https?://[^\s\"'<>]+", normalized_text):
            add_url(candidates, m)

        for m in re.findall(r"""(?:href|action)=["']([^"'<>]+)["']""", normalized_text, flags=re.IGNORECASE):
            add_url(candidates, m)

        for m in re.findall(r"""(/api/accounts/login\?[^"'<>\s]+)""", normalized_text):
            add_url(candidates, m)

        for m in re.findall(r"""(/[^"'<>\s]*?(?:login_challenge|code|state)=[^"'<>\s]+)""", normalized_text):
            add_url(candidates, m)

        seen = set()
        ordered: List[str] = []
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered

    def _candidate_url_sort_key(self, url: str) -> Tuple[int, int]:
        """对继续地址做优先级排序"""
        raw = str(url or "").strip().lower()
        if "code=" in raw and "state=" in raw:
            return (0, len(raw))
        if "login_challenge=" in raw or "/api/accounts/login" in raw:
            return (1, len(raw))
        if "continue_url" in raw or "/oauth/" in raw or "/api/oauth/" in raw:
            return (2, len(raw))
        if "auth.openai.com" in raw:
            return (3, len(raw))
        return (4, len(raw))

    def _collect_candidate_urls_from_response(self, response) -> List[str]:
        """从响应体与会话 Cookie 中提取可尝试的继续地址"""
        import base64
        import json as json_module
        import urllib.parse

        def add_url(candidates: List[str], raw: Optional[str]):
            v = str(raw or "").strip()
            if not v:
                return
            if v.startswith("http://") or v.startswith("https://"):
                candidates.append(v)
                return
            if v.startswith("/"):
                candidates.append(urllib.parse.urljoin("https://auth.openai.com", v))

        def walk_collect(obj: Any, out: List[str]):
            if isinstance(obj, dict):
                for vv in obj.values():
                    walk_collect(vv, out)
                return
            if isinstance(obj, list):
                for vv in obj:
                    walk_collect(vv, out)
                return
            if isinstance(obj, str):
                s = obj.strip()
                if not s:
                    return
                add_url(out, s)
                for m in re.findall(r"https?://[^\s\"'<>]+", s):
                    add_url(out, m)

        candidates: List[str] = []

        # 1) 响应头 Location
        add_url(candidates, response.headers.get("Location"))

        # 2) 响应 JSON / 文本中的 URL
        try:
            body = response.json()
            walk_collect(body, candidates)
        except Exception:
            pass

        for url in self._extract_candidate_urls_from_text(response.text or ""):
            add_url(candidates, url)

        # 3) 常见会话 Cookie 中可能存在跳转信息
        for cookie_name in ["hydra_redirect", "login_session", "unified_session_manifest", "oai-client-auth-session"]:
            raw_cookie = self.session.cookies.get(cookie_name)
            if not raw_cookie:
                continue

            decoded_cookie = urllib.parse.unquote(raw_cookie)
            add_url(candidates, decoded_cookie)

            payload = decoded_cookie.split(".")[0]
            if not payload:
                continue
            try:
                pad = "=" * ((4 - (len(payload) % 4)) % 4)
                payload_obj = json_module.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
                walk_collect(payload_obj, candidates)
            except Exception:
                pass

        # 4) 去重并优先 auth.openai.com 域
        normalized = []
        seen = set()
        for u in candidates:
            v = str(u or "").strip()
            if not v or v in seen:
                continue
            seen.add(v)
            normalized.append(v)

        normalized.sort(key=self._candidate_url_sort_key)
        return normalized

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            import base64
            import json as json_module
            import urllib.parse

            # 打印当前会话里的 Cookie 名称，帮助定位到底收到了什么
            cookie_descriptions = []
            try:
                for c in self.session.cookies:
                    domain = getattr(c, "domain", "")
                    path = getattr(c, "path", "")
                    cookie_descriptions.append(f"{c.name}@{domain}{path}")
            except Exception:
                # 部分实现不支持直接迭代 Cookie 对象，降级只打印 name
                try:
                    cookie_descriptions = [str(k) for k in self.session.cookies.keys()]
                except Exception:
                    cookie_descriptions = []

            if cookie_descriptions:
                preview = ", ".join(cookie_descriptions[:20])
                if len(cookie_descriptions) > 20:
                    preview += f" ... (+{len(cookie_descriptions) - 20})"
                self._log(f"当前会话 Cookie({len(cookie_descriptions)}): {preview}")
            else:
                self._log("当前会话 Cookie 列表为空", "warning")

            # 先取 session，再兼容 info，方便定位为什么没有 workspace
            cookie_candidates = ["oai-client-auth-session", "oai-client-auth-info"]

            for cookie_name in cookie_candidates:
                raw_cookie = self.session.cookies.get(cookie_name)
                if not raw_cookie:
                    self._log(f"未找到 Cookie: {cookie_name}", "warning")
                    continue

                self._log(f"命中 Cookie: {cookie_name}, 长度: {len(raw_cookie)}")

                # oai-client-auth-info 常为 URL 编码；session 一般是 base64url 段
                cookie_value = urllib.parse.unquote(raw_cookie)
                if cookie_value != raw_cookie:
                    self._log(f"{cookie_name} 为 URL 编码，已自动解码")

                try:
                    segments = cookie_value.split(".")
                    payload = segments[0] if segments else ""
                    if not payload:
                        self._log(f"{cookie_name} 为空 payload", "warning")
                        continue

                    # 解码第一个 segment（现网格式为 base64url JSON）
                    pad = "=" * ((4 - (len(payload) % 4)) % 4)
                    decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
                    auth_json = json_module.loads(decoded.decode("utf-8"))

                    keys = sorted(list(auth_json.keys()))
                    self._log(f"{cookie_name} payload keys: {keys}")

                    workspaces = auth_json.get("workspaces") or []
                    if not workspaces:
                        self._log(f"{cookie_name} 里没有 workspaces 字段", "warning")
                        continue

                    workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                    if not workspace_id:
                        self._log(f"{cookie_name} workspaces[0] 缺少 id", "warning")
                        continue

                    self._log(f"Workspace ID: {workspace_id}")
                    return workspace_id
                except Exception as e:
                    self._log(f"解析 Cookie 失败 ({cookie_name}): {e}", "warning")
                    continue

            self._log("授权 Cookie 里没有 workspace 信息", "error")
            return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=select_body,
            )

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 8
            visited = set()

            for i in range(max_redirects):
                if not current_url:
                    break
                if current_url in visited:
                    self._log(f"重定向链出现重复 URL，停止: {current_url[:100]}...", "warning")
                    break
                visited.add(current_url)

                if "code=" in current_url and "state=" in current_url:
                    self._log(f"找到回调 URL: {current_url[:100]}...")
                    return current_url

                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    if response.status_code == 200:
                        inline_urls = self._extract_candidate_urls_from_text(response.text or "")
                        inline_urls = [u for u in inline_urls if u not in visited and u != current_url]
                        inline_urls.sort(key=self._candidate_url_sort_key)
                        if inline_urls:
                            self._log(
                                f"当前页提取到候选继续地址({len(inline_urls)}): "
                                f"{inline_urls[0][:100]}..."
                            )
                            current_url = inline_urls[0]
                            continue
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _follow_candidate_urls(self, start_urls: List[str], log_prefix: str = "") -> Optional[str]:
        """按优先级尝试多个继续地址，直到命中 OAuth callback"""
        seen = set()
        ordered_urls: List[str] = []
        for url in start_urls:
            value = str(url or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            ordered_urls.append(value)

        ordered_urls.sort(key=self._candidate_url_sort_key)
        for idx, start_url in enumerate(ordered_urls, 1):
            self._log(
                f"{log_prefix}尝试回调起点 {idx}/{len(ordered_urls)}: "
                f"{start_url[:100]}..."
            )
            callback_url = self._follow_redirects(start_url)
            if callback_url:
                return callback_url
        return None

    def _oauth_login_reauth_with_otp(
        self,
        step_prefix: str = "13.",
        use_fresh_session: bool = True
    ) -> OAuthContinuationResult:
        """
        create_account 后若拿不到 workspace，走 fresh-session 登录补偿：
        新 OAuth -> login -> passwordless/send-otp -> validate -> workspace/select -> callback
        """
        original_is_existing = self._is_existing_account
        try:
            if use_fresh_session:
                self._log(f"{step_prefix}1 [补偿] 重置认证会话并准备新的 OAuth 登录流程...")
                if not self._reset_auth_session(clear_oauth=True):
                    return OAuthContinuationResult(
                        success=False,
                        error_message="重置认证会话失败"
                    )
            else:
                self._log(f"{step_prefix}1 [补偿] 复用当前认证会话...")

            self._log(f"{step_prefix}2 [补偿] 初始化 OAuth 登录流程...")
            if not self._start_oauth():
                return OAuthContinuationResult(
                    success=False,
                    error_message="重新初始化 OAuth 失败"
                )

            self._log(f"{step_prefix}3 [补偿] 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                return OAuthContinuationResult(
                    success=False,
                    error_message="获取 Device ID 失败"
                )

            self._log(f"{step_prefix}4 [补偿] 检查 Sentinel...")
            sen_token = self._check_sentinel(did)
            if sen_token:
                self._log("[补偿] Sentinel 检查通过")
            else:
                self._log("[补偿] Sentinel 检查失败或未启用", "warning")

            self._log(f"{step_prefix}5 [补偿] 提交登录账号...")
            login_result = self._submit_signup_form(did, sen_token, screen_hint="login")
            if not login_result.success:
                self._log(f"[补偿] 提交登录账号失败: {login_result.error_message}", "warning")
                return OAuthContinuationResult(
                    success=False,
                    error_message=f"提交登录账号失败: {login_result.error_message}"
                )

            page_type = (login_result.page_type or "").strip()
            page_type_lower = page_type.lower()
            self._log(f"{step_prefix}6 [补偿] 账号步骤返回 page_type: {page_type}")
            if login_result.response_data is not None:
                self._log_response_snapshot("登录账号步骤", login_result.response_data)

            is_login_password_step = (
                page_type_lower in ("login_password", OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"])
                or (page_type_lower.startswith("login") and "password" in page_type_lower)
            )
            is_otp_step = (
                page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
                or ("otp" in page_type_lower and "verification" in page_type_lower)
            )

            if is_login_password_step:
                self._log(f"{step_prefix}7 [补偿] 发送 Passwordless OTP...")
                if not self._send_passwordless_login_otp():
                    return OAuthContinuationResult(
                        success=False,
                        error_message="发送 Passwordless OTP 失败"
                    )
            elif is_otp_step:
                self._log(f"{step_prefix}7 [补偿] 已进入 OTP 页面，继续等待验证码...")
                self._otp_sent_at = time.time()
            else:
                self._log("[补偿] 未进入可识别登录页（login_password/email_otp_verification）", "warning")
                return OAuthContinuationResult(
                    success=False,
                    error_message=f"登录页类型异常: {page_type}"
                )

            self._log(f"{step_prefix}8 [补偿] 等待登录 OTP...")
            code = self._get_verification_code(timeout=90)
            if not code:
                return OAuthContinuationResult(
                    success=False,
                    error_message="获取登录 OTP 失败"
                )

            self._log(f"{step_prefix}9 [补偿] 验证登录验证码...")
            otp_result = self._validate_verification_code(
                code,
                context="登录",
                referer="https://auth.openai.com/log-in/email-verification"
            )
            if not otp_result.success:
                self._log("[补偿] 验证验证码失败", "warning")
                return OAuthContinuationResult(
                    success=False,
                    error_message=f"验证登录验证码失败: {otp_result.error_message}"
                )

            self._log(f"{step_prefix}10 [补偿] 重新获取 Workspace ID...")
            workspace_id = self._get_workspace_id()
            if not workspace_id:
                return OAuthContinuationResult(
                    success=False,
                    error_message="登录 OTP 验证后仍未获取到 Workspace ID"
                )

            self._log(f"{step_prefix}11 [补偿] 选择 Workspace...")
            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                return OAuthContinuationResult(
                    success=False,
                    workspace_id=workspace_id,
                    error_message="选择 Workspace 失败"
                )

            self._log(f"{step_prefix}12 [补偿] 跟随重定向链...")
            callback_url = self._follow_redirects(continue_url)
            if not callback_url:
                return OAuthContinuationResult(
                    success=False,
                    workspace_id=workspace_id,
                    error_message="跟随 Workspace 重定向失败"
                )

            return OAuthContinuationResult(
                success=True,
                workspace_id=workspace_id,
                callback_url=callback_url,
            )
        except Exception as e:
            self._log(f"登录补偿流程异常: {e}", "warning")
            return OAuthContinuationResult(success=False, error_message=str(e))
        finally:
            # 保持主流程来源标记不被补偿流程污染
            self._is_existing_account = original_is_existing

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._log("1. 检查 IP 地理位置...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._log("2. 创建邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            # 3. 初始化会话
            self._log("3. 初始化会话...")
            if not self._init_session():
                result.error_message = "初始化会话失败"
                return result

            # 4. 开始 OAuth 流程
            self._log("4. 开始 OAuth 授权流程...")
            if not self._start_oauth():
                result.error_message = "开始 OAuth 流程失败"
                return result

            # 5. 获取 Device ID
            self._log("5. 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result

            # 6. 检查 Sentinel 拦截
            self._log("6. 检查 Sentinel 拦截...")
            sen_token = self._check_sentinel(did)
            if sen_token:
                self._log("Sentinel 检查通过")
            else:
                self._log("Sentinel 检查失败或未启用", "warning")

            # 7. 提交注册表单 + 解析响应判断账号状态
            self._log("7. 提交注册表单...")
            signup_result = self._submit_signup_form(did, sen_token)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result
            if signup_result.response_data is not None:
                self._log_response_snapshot("注册账号步骤", signup_result.response_data)

            # 8. [已注册账号跳过] 注册密码
            if self._is_existing_account:
                self._log("8. [已注册账号] 跳过密码设置，OTP 已自动发送")
            else:
                self._log("8. 注册密码...")
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result

            # 9. [已注册账号跳过] 发送验证码
            if self._is_existing_account:
                self._log("9. [已注册账号] 跳过发送验证码，使用自动发送的 OTP")
                # 已注册账号的 OTP 在提交表单时已自动发送，记录时间戳
                self._otp_sent_at = time.time()
            else:
                self._log("9. 发送验证码...")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result

            # 10. 获取验证码
            self._log("10. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return result

            # 11. 验证验证码
            self._log("11. 验证验证码...")
            otp_result = self._validate_verification_code(code)
            if not otp_result.success:
                result.error_message = "验证验证码失败"
                return result

            # 12. [已注册账号跳过] 创建用户账户
            if self._is_existing_account:
                self._log("12. [已注册账号] 跳过创建用户账户")
            else:
                self._log("12. 创建用户账户...")
                if not self._create_user_account():
                    create_account_error = self._last_create_account_error or {}
                    error_code = str(create_account_error.get("error_code") or "").strip()
                    result.metadata = {
                        "step": "create_user_account",
                        "create_account_error": create_account_error,
                    }
                    result.error_message = (
                        f"创建用户账户失败 ({error_code})" if error_code else "创建用户账户失败"
                    )
                    return result

            callback_url: Optional[str] = None

            self._log("13. 获取 Workspace ID...")
            workspace_id = self._get_workspace_id()

            if workspace_id:
                result.workspace_id = workspace_id

                # 14. 选择 Workspace
                self._log("14. 选择 Workspace...")
                continue_url = self._select_workspace(workspace_id)
                if not continue_url:
                    result.error_message = "选择 Workspace 失败"
                    return result

                # 15. 跟随重定向链
                self._log("15. 跟随重定向链...")
                callback_url = self._follow_redirects(continue_url)
                if not callback_url:
                    result.error_message = "跟随重定向链失败"
                    return result
            else:
                self._log("13. 未获取到 Workspace ID，触发 fresh-session 登录降级流程...", "warning")
                recovery_result = self._oauth_login_reauth_with_otp(
                    step_prefix="13.",
                    use_fresh_session=True
                )
                if not recovery_result.success:
                    result.error_message = recovery_result.error_message or "获取 Workspace ID 失败"
                    return result

                result.workspace_id = recovery_result.workspace_id
                callback_url = recovery_result.callback_url

            # 16. 处理 OAuth 回调
            self._log("16. 处理 OAuth 回调...")
            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "处理 OAuth 回调失败"
                return result

            # 提取账户信息
            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.password = self.password or ""  # 保存密码（已注册账号为空）

            # 设置来源标记
            result.source = "login" if self._is_existing_account else "register"

            # 尝试获取 session_token 从 cookie
            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = session_cookie
                result.session_token = session_cookie
                self._log(f"获取到 Session Token")

            # 17. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功! (已注册账号)")
            else:
                self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()

            with get_db() as db:
                # 保存账户信息
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source
                )

                self._log(f"账户已保存到数据库，ID: {account.id}")
                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
