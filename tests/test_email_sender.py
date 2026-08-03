from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.email_sender import SummaryEmailSender, _normalize_subject


class SummaryEmailSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_summary_uses_authenticated_smtp_ssl(self) -> None:
        sender = SummaryEmailSender(
            host="smtp.example.com",
            port=465,
            username="bot@example.com",
            password="secret",
            sender="bot@example.com",
            recipient="results@example.com",
        )
        smtp = MagicMock()
        smtp_context = MagicMock()
        smtp_context.__enter__.return_value = smtp

        with patch("app.email_sender.smtplib.SMTP_SSL", return_value=smtp_context):
            sent = await sender.send_summary(
                subject="Результат обработки: tender.docx",
                summary="Готовое саммари",
            )

        self.assertTrue(sent)
        smtp.login.assert_called_once_with("bot@example.com", "secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "results@example.com")
        self.assertEqual(message["Subject"], "Результат обработки: tender.docx")
        self.assertIn("Готовое саммари", message.get_content())

    async def test_missing_password_disables_email_without_error(self) -> None:
        sender = SummaryEmailSender(
            host="smtp.example.com",
            port=465,
            username="bot@example.com",
            password="",
            sender="bot@example.com",
            recipient="results@example.com",
        )

        with patch("app.email_sender.smtplib.SMTP_SSL") as smtp_ssl:
            sent = await sender.send_summary(subject="Результат", summary="Саммари")

        self.assertFalse(sent)
        smtp_ssl.assert_not_called()

    def test_subject_is_single_line_and_limited(self) -> None:
        subject = _normalize_subject("Файл\nс результатом" + "x" * 300)

        self.assertNotIn("\n", subject)
        self.assertLessEqual(len(subject), 200)


if __name__ == "__main__":
    unittest.main()
