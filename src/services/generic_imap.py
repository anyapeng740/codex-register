"""
通用 IMAP 邮箱服务
支持本地生成 alias，并通过任意 IMAP 收件箱轮询验证码邮件
"""

import email
import imaplib
import logging
import random
import re
import string
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceType
from .outlook.base import EmailMessage
from .outlook.email_parser import get_email_parser
from ..config.constants import OTP_CODE_PATTERN

logger = logging.getLogger(__name__)


class GenericImapEmailService(BaseEmailService):
    """通用 IMAP 邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.GENERIC_IMAP, name)

        default_config = {
            "alias": {
                "domain": "",
                "prefix_length": 10,
            },
            "imap": {
                "host": "",
                "port": 993,
                "use_ssl": True,
                "mailbox": "INBOX",
                "mailboxes": [],
                "username": "",
                "password": "",
            },
            "wait": {
                "timeout": 180,
                "poll_interval": 5,
                "unseen_only": False,
            },
            "match": {
                "recipient": "",
                "ignore_recipient": True,
                "sender_contains": ["openai"],
                "subject_contains": ["verification", "代码", "验证码", "chatgpt"],
            },
        }

        self.config = self._deep_merge(default_config, config or {})
        self.alias_config = self.config["alias"]
        self.imap_config = self.config["imap"]
        self.wait_config = self.config["wait"]
        self.match_config = self.config["match"]

        required = [
            ("alias.domain", self.alias_config.get("domain")),
            ("imap.host", self.imap_config.get("host")),
            ("imap.username", self.imap_config.get("username")),
            ("imap.password", self.imap_config.get("password")),
        ]
        missing = [key for key, value in required if not value]
        if missing:
            raise ValueError(f"缺少必需配置: {missing}")

        self._aliases: Dict[str, Dict[str, Any]] = {}
        self.email_parser = get_email_parser()

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _connect(self):
        host = self.imap_config["host"]
        port = int(self.imap_config.get("port") or 993)
        username = self.imap_config["username"]
        password = self.imap_config["password"]
        use_ssl = self.imap_config.get("use_ssl", True)

        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)
        conn.login(username, password)
        return conn

    def _get_mailboxes(self) -> List[str]:
        raw_mailboxes = self.imap_config.get("mailboxes") or []
        if isinstance(raw_mailboxes, str):
            raw_mailboxes = [raw_mailboxes]
        mailboxes = [str(item).strip() for item in raw_mailboxes if str(item).strip()]
        if not mailboxes:
            mailbox = str(self.imap_config.get("mailbox") or "INBOX").strip()
            mailboxes = [mailbox] if mailbox else ["INBOX"]

        deduped = []
        seen = set()
        for mailbox in mailboxes:
            key = mailbox.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(mailbox)
        return deduped

    def _generate_alias(self) -> str:
        length = int(self.alias_config.get("prefix_length") or 10)
        length = max(1, min(length, 64))
        prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
        return f"{prefix}@{self.alias_config['domain']}"

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        alias = self._generate_alias()
        info = {
            "email": alias,
            "service_id": alias,
            "id": alias,
            "created_at": time.time(),
        }
        self._aliases[alias] = info
        self.update_status(True)
        logger.info(f"生成 Generic IMAP alias: {alias}")
        return info

    def _decode_header_value(self, value: str) -> str:
        if not value:
            return ""
        try:
            parts = decode_header(value)
            decoded = []
            for text, charset in parts:
                if isinstance(text, bytes):
                    decoded.append(text.decode(charset or "utf-8", errors="replace"))
                else:
                    decoded.append(text)
            return "".join(decoded)
        except Exception:
            return value

    def _extract_body(self, msg) -> str:
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = (part.get_content_type() or "").lower()
                if content_type not in ("text/plain", "text/html"):
                    continue
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    continue
                if content_type == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
        else:
            try:
                payload = msg.get_payload(decode=True)
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                body = str(msg.get_payload() or "")
            if "html" in (msg.get_content_type() or "").lower():
                body = re.sub(r"<[^>]+>", " ", body)
            parts.append(body)
        return "\n".join(part for part in parts if part).strip()

    def _parse_message(self, message_id: bytes, raw: bytes) -> Optional[EmailMessage]:
        if not raw:
            return None
        msg = email.message_from_bytes(raw)
        subject = self._decode_header_value(msg.get("Subject", ""))
        sender = self._decode_header_value(msg.get("From", ""))
        to = self._decode_header_value(msg.get("To", ""))
        delivered_to = self._decode_header_value(msg.get("Delivered-To", ""))
        x_original_to = self._decode_header_value(msg.get("X-Original-To", ""))
        date_str = self._decode_header_value(msg.get("Date", ""))
        body = self._extract_body(msg)

        received_at = None
        received_timestamp = 0
        try:
            if date_str:
                received_at = parsedate_to_datetime(date_str)
                received_timestamp = int(received_at.timestamp())
        except Exception:
            pass

        recipients = [r for r in [to, delivered_to, x_original_to] if r]
        return EmailMessage(
            id=message_id.decode(errors="ignore"),
            subject=subject,
            sender=sender,
            recipients=recipients,
            body=body,
            body_preview=body[:200],
            received_at=received_at,
            received_timestamp=received_timestamp,
            raw_data=raw,
        )

    def _matches_filters(self, email_msg: EmailMessage, target_email: str, min_timestamp: int) -> bool:
        if min_timestamp > 0 and email_msg.received_timestamp > 0 and email_msg.received_timestamp < min_timestamp:
            return False

        sender_patterns = [str(x).lower() for x in (self.match_config.get("sender_contains") or []) if str(x).strip()]
        if sender_patterns:
            sender = email_msg.sender.lower()
            if not any(pattern in sender for pattern in sender_patterns):
                return False

        subject_patterns = [str(x).lower() for x in (self.match_config.get("subject_contains") or []) if str(x).strip()]
        if subject_patterns:
            subject = email_msg.subject.lower()
            if not any(pattern in subject for pattern in subject_patterns):
                return False

        if not self.match_config.get("ignore_recipient", True):
            expected = (self.match_config.get("recipient") or target_email or "").lower().strip()
            recipients = " ".join(email_msg.recipients).lower()
            if expected and expected not in recipients:
                return False

        return True

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        actual_timeout = timeout or int(self.wait_config.get("timeout") or 180)
        poll_interval = int(self.wait_config.get("poll_interval") or 5)
        unseen_only = bool(self.wait_config.get("unseen_only", False))
        mailboxes = self._get_mailboxes()
        # 仅接受 OTP 发送时间之后的邮件，避免重复命中旧验证码
        min_timestamp = int(otp_sent_at) if otp_sent_at else 0
        start_time = time.time()
        seen_ids = set()

        logger.info(f"[{email}] Generic IMAP 开始轮询 mailboxes={mailboxes}, timeout={actual_timeout}s")

        while time.time() - start_time < actual_timeout:
            conn = None
            try:
                conn = self._connect()
                for mailbox in mailboxes:
                    try:
                        status, _ = conn.select(mailbox, readonly=True)
                        if status != "OK":
                            logger.debug(f"[{email}] 选择邮箱夹失败: {mailbox}")
                            continue
                        status, data = conn.search(None, "UNSEEN" if unseen_only else "ALL")
                        if status != "OK" or not data or not data[0]:
                            continue
                        ids = data[0].split()
                        for message_id in ids[-20:][::-1]:
                            decoded_id = f"{mailbox}:{message_id.decode(errors='ignore')}"
                            if decoded_id in seen_ids:
                                continue
                            seen_ids.add(decoded_id)
                            fetch_status, fetch_data = conn.fetch(message_id, "(RFC822)")
                            if fetch_status != "OK" or not fetch_data:
                                continue
                            raw = b""
                            for part in fetch_data:
                                if isinstance(part, tuple) and len(part) > 1:
                                    raw = part[1]
                                    break
                            email_msg = self._parse_message(message_id, raw)
                            if not email_msg:
                                continue
                            if not self._matches_filters(email_msg, email, min_timestamp):
                                logger.debug(f"[{email}] 跳过邮件 mailbox={mailbox} subject={email_msg.subject[:80]}")
                                continue
                            code = self.email_parser.extract_verification_code(email_msg)
                            if code:
                                self.update_status(True)
                                logger.info(f"从 Generic IMAP 邮箱 {email} 找到验证码: {code} (mailbox={mailbox})")
                                return code
                            content = f"{email_msg.subject}\n{email_msg.body}"
                            match = re.search(pattern, content)
                            if match:
                                code = match.group(1)
                                self.update_status(True)
                                logger.info(f"从 Generic IMAP 邮箱 {email} 兜底提取验证码: {code} (mailbox={mailbox})")
                                return code
                    except Exception as mailbox_error:
                        logger.debug(f"[{email}] 检查邮箱夹 {mailbox} 时出错: {mailbox_error}")
            except Exception as e:
                logger.debug(f"检查 Generic IMAP 邮件时出错: {e}")
                self.update_status(False, e)
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        conn.logout()
                    except Exception:
                        pass
            time.sleep(poll_interval)

        logger.warning(f"等待 Generic IMAP 验证码超时: {email}, mailboxes={mailboxes}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._aliases.values())

    def delete_email(self, email_id: str) -> bool:
        removed = self._aliases.pop(email_id, None) is not None
        if removed:
            self.update_status(True)
        return removed

    def check_health(self) -> bool:
        conn = None
        try:
            conn = self._connect()
            for mailbox in self._get_mailboxes():
                status, _ = conn.select(mailbox, readonly=True)
                if status == "OK":
                    self.update_status(True)
                    return True
            self.update_status(False, RuntimeError("没有可用的邮箱夹"))
            return False
        except Exception as e:
            logger.warning(f"Generic IMAP 健康检查失败: {e}")
            self.update_status(False, e)
            return False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn.logout()
                except Exception:
                    pass

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "alias_domain": self.alias_config.get("domain"),
            "imap_host": self.imap_config.get("host"),
            "imap_port": self.imap_config.get("port"),
            "mailbox": self.imap_config.get("mailbox"),
            "mailboxes": self._get_mailboxes(),
            "status": self.status.value,
            "cached_aliases_count": len(self._aliases),
        }
