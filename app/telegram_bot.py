from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import Bot, Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .docx_parser import (
    SUPPORTED_EXTENSIONS,
    DocumentExtractionError,
    extract_document_text,
)
from .summarizer import TenderSummarizer

logger = logging.getLogger(__name__)

MEDIA_GROUP_WAIT_SECONDS = 2.0


@dataclass
class DocumentPayload:
    file_id: str
    file_unique_id: str
    file_name: str
    extension: str


@dataclass
class PendingMediaGroup:
    status_message: Message
    chat_id: int
    documents: list[DocumentPayload] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


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

        self.pending_media_groups: dict[str, PendingMediaGroup] = {}
        self.pending_media_groups_lock = asyncio.Lock()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Бот активен. Пришлите .doc/.docx в группу. "
                "Если файлов несколько в одном сообщении, сделаю общее саммари."
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

        payload = DocumentPayload(
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            file_name=file_name,
            extension=extension,
        )

        media_group_id = message.media_group_id
        if media_group_id:
            await self.queue_media_group_document(
                media_group_id=media_group_id,
                message=message,
                payload=payload,
                bot=context.bot,
            )
            return

        await self.process_single_document(message=message, payload=payload, bot=context.bot)

    async def process_single_document(
        self,
        message: Message,
        payload: DocumentPayload,
        bot: Bot,
    ) -> None:
        status_message = await message.reply_text("Получил документ, извлекаю текст...")
        try:
            text = await self.download_and_extract_text(payload=payload, bot=bot)
            if not text.strip():
                await status_message.edit_text("Не получилось извлечь текст из документа.")
                return

            await status_message.edit_text("Текст извлечен, готовлю саммари...")
            summary = await self.summarizer.summarize(text=text, file_name=payload.file_name)

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

    async def queue_media_group_document(
        self,
        media_group_id: str,
        message: Message,
        payload: DocumentPayload,
        bot: Bot,
    ) -> None:
        async with self.pending_media_groups_lock:
            pending = self.pending_media_groups.get(media_group_id)
            if pending is None:
                status_message = await message.reply_text(
                    "Получил пакет документов, собираю все файлы..."
                )
                pending = PendingMediaGroup(
                    status_message=status_message,
                    chat_id=message.chat_id,
                )
                self.pending_media_groups[media_group_id] = pending

            if any(item.file_unique_id == payload.file_unique_id for item in pending.documents):
                return
            pending.documents.append(payload)

            if pending.task and not pending.task.done():
                pending.task.cancel()

            pending.task = asyncio.create_task(
                self.finalize_media_group(media_group_id=media_group_id, bot=bot)
            )

    async def finalize_media_group(self, media_group_id: str, bot: Bot) -> None:
        try:
            await asyncio.sleep(MEDIA_GROUP_WAIT_SECONDS)
        except asyncio.CancelledError:
            return

        async with self.pending_media_groups_lock:
            pending = self.pending_media_groups.pop(media_group_id, None)

        if pending is None:
            return

        status_message = pending.status_message
        try:
            await status_message.edit_text("Пакет получен, извлекаю текст из всех файлов...")
            summary = await self.build_combined_summary(documents=pending.documents, bot=bot)

            await status_message.delete()
            for part in split_for_telegram(summary):
                await bot.send_message(chat_id=pending.chat_id, text=part)
        except DocumentExtractionError:
            logger.exception("Media group extraction failed")
            await status_message.edit_text(
                "Не удалось извлечь текст из пакета документов."
            )
        except Exception:
            logger.exception("Failed to process media group")
            await status_message.edit_text(
                "Не удалось обработать пакет документов. Проверьте формат файлов и ключ API."
            )

    async def build_combined_summary(self, documents: list[DocumentPayload], bot: Bot) -> str:
        extracted_parts: list[tuple[str, str]] = []
        failed_files: list[str] = []

        for payload in documents:
            try:
                text = await self.download_and_extract_text(payload=payload, bot=bot)
                if text.strip():
                    extracted_parts.append((payload.file_name, text))
                else:
                    failed_files.append(f"{payload.file_name}: пустой текст")
            except Exception as exc:
                failed_files.append(f"{payload.file_name}: {exc}")

        if not extracted_parts:
            raise DocumentExtractionError("No files were extracted")

        if len(extracted_parts) == 1:
            summary = await self.summarizer.summarize(
                text=extracted_parts[0][1],
                file_name=extracted_parts[0][0],
            )
        else:
            combined_text = "\n\n".join(
                f"### Документ {idx}: {name}\n{text}"
                for idx, (name, text) in enumerate(extracted_parts, start=1)
            )
            summary = await self.summarizer.summarize(
                text=combined_text,
                file_name=f"Пакет документов ({len(extracted_parts)} файла)",
            )

        if failed_files:
            failures_text = "\n".join(f"- {item}" for item in failed_files)
            summary = f"{summary}\n\nНе обработаны файлы:\n{failures_text}"

        return summary

    async def download_and_extract_text(self, payload: DocumentPayload, bot: Bot) -> str:
        temp_path: Path | None = None
        try:
            telegram_file = await bot.get_file(payload.file_id)
            with tempfile.NamedTemporaryFile(
                suffix=payload.extension or ".bin",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)

            await telegram_file.download_to_drive(custom_path=str(temp_path))
            return extract_document_text(temp_path)
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
