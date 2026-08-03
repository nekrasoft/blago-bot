from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


logger = logging.getLogger(__name__)


class SummaryEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipient = recipient
        self.timeout = timeout
        self._missing_config_logged = False

    @classmethod
    def from_settings(cls, settings: Settings) -> SummaryEmailSender:
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender=settings.smtp_sender,
            recipient=settings.email_recipient,
        )

    @property
    def is_configured(self) -> bool:
        return bool(
            self.host
            and self.port
            and self.username
            and self.password
            and self.sender
            and self.recipient
        )

    async def send_summary(self, *, subject: str, summary: str) -> bool:
        if not self.is_configured:
            if not self._missing_config_logged:
                logger.warning(
                    "Email delivery is disabled: SMTP settings are incomplete"
                )
                self._missing_config_logged = True
            return False

        try:
            await asyncio.to_thread(
                self._send_summary_sync,
                subject=_normalize_subject(subject),
                summary=summary,
            )
        except Exception:
            logger.exception(
                "Failed to send processing result to %s",
                self.recipient,
            )
            return False

        logger.info("Processing result sent to %s", self.recipient)
        return True

    def _send_summary_sync(self, *, subject: str, summary: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.recipient
        message["Subject"] = subject
        message.set_content(summary)

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self.host,
            self.port,
            timeout=self.timeout,
            context=context,
        ) as smtp:
            smtp.login(self.username, self.password)
            smtp.send_message(
                message,
                from_addr=self.sender,
                to_addrs=[self.recipient],
            )


def _normalize_subject(subject: str) -> str:
    normalized = " ".join(subject.splitlines()).strip()
    return normalized[:200] or "Результат обработки тендерных документов"
