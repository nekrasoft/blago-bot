from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .docx_parser import (
    SUPPORTED_EXTENSIONS,
    DocumentExtractionError,
    extract_document_text,
)
from .summarizer import TenderSummarizer

logger = logging.getLogger(__name__)


class TenderTelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.summarizer = TenderSummarizer(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            language=settings.summary_language,
            max_doc_chars=settings.max_doc_chars,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(
            MessageHandler(filters.Document.ALL & filters.ChatType.GROUPS, self.handle_group_document)
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Бот активен. Пришлите .doc или .docx в группу, и я подготовлю краткое саммари."
            )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Я обрабатываю .doc/.docx в группе и возвращаю саммари по тендерной документации."
            )

    async def handle_group_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not message.document:
            return

        document = message.document
        file_name = document.file_name or "document.bin"
        extension = detect_document_extension(file_name=file_name, mime_type=document.mime_type)
        if extension not in SUPPORTED_EXTENSIONS:
            return

        status_message = await message.reply_text("Получил документ, извлекаю текст...")

        temp_path: Path | None = None
        try:
            telegram_file = await context.bot.get_file(document.file_id)
            suffix = extension if extension else ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                temp_path = Path(temp_file.name)

            await telegram_file.download_to_drive(custom_path=str(temp_path))
            text = extract_document_text(temp_path)

            if not text.strip():
                await status_message.edit_text("Не получилось извлечь текст из документа.")
                return

            await status_message.edit_text("Текст извлечен, готовлю саммари...")
            summary = await self.summarizer.summarize(text=text, file_name=file_name)

            await status_message.delete()
            for part in split_for_telegram(summary):
                await message.reply_text(part)

        except DocumentExtractionError:
            logger.exception("Document extraction failed")
            await status_message.edit_text(
                "Не удалось извлечь текст из документа. Для .doc установите LibreOffice, antiword или catdoc."
            )
        except Exception:
            logger.exception("Failed to process document")
            await status_message.edit_text(
                "Не удалось обработать файл. Проверьте формат документа и ключ API."
            )
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def run(self) -> None:
        self.app.run_polling(close_loop=False)



def split_for_telegram(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = []
    current_len = 0

    for paragraph in text.split("\n"):
        line = paragraph.strip()
        candidate_len = current_len + len(line) + 1

        if candidate_len > limit and current:
            parts.append("\n".join(current).strip())
            current = [line]
            current_len = len(line)
            continue

        if len(line) > limit:
            if current:
                parts.append("\n".join(current).strip())
                current = []
                current_len = 0
            for i in range(0, len(line), limit):
                parts.append(line[i : i + limit])
            continue

        current.append(line)
        current_len = candidate_len

    if current:
        parts.append("\n".join(current).strip())

    return [part for part in parts if part]


def detect_document_extension(file_name: str, mime_type: str | None) -> str:
    extension = Path(file_name).suffix.lower()
    if extension:
        return extension

    normalized_mime = (mime_type or "").lower()
    if normalized_mime == "application/msword":
        return ".doc"
    if (
        normalized_mime
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return ".docx"

    return ""
