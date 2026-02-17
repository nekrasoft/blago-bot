from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Bot, Message, Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .archive_parser import (
    ArchiveExtractionError,
    SUPPORTED_ARCHIVE_EXTENSIONS,
    extract_archive_document_texts,
)
from .config import Settings
from .docx_parser import (
    SUPPORTED_EXTENSIONS,
    DocumentExtractionError,
    extract_document_text,
)
from .summarizer import TenderSummarizer

logger = logging.getLogger(__name__)

MEDIA_GROUP_WAIT_SECONDS = 2.0
CONTEXT_MESSAGE_MAX_AGE_SECONDS = 30 * 60
CONTEXT_BUFFER_SIZE = 30
UNAUTHORIZED_CHAT_TEXT = (
    "Работа бота в этом чате не разрешена. "
    "Бот покидает чат."
)
SUMMARY_HEADING_PREFIXES = (
    "файл",
    "кратко о закупке",
    "тип закупки",
    "тип процедуры",
    "территориальность",
    "unit-экономика",
    "unit экономика",
    "требования к исполнителю",
    "контактные данные заказчика",
    "контактные данные",
    "основные требования",
    "документы/условия участия",
    "ключевые сроки",
    "деньги/гарантии",
    "риски и что уточнить",
    "не обработаны файлы",
)
PROCUREMENT_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?zakupki\.gov\.ru/epz/order/\S+",
    re.IGNORECASE,
)


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
    source_user_id: int | None
    first_message_id: int
    documents: list[DocumentPayload] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass
class RecentChatText:
    message_id: int
    text: str
    date: datetime
    has_procurement_link: bool


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
        self.app.add_handler(
            ChatMemberHandler(self.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
        )
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
                self.handle_group_text_message,
            )
        )
        self.app.add_handler(
            MessageHandler(filters.Document.ALL & filters.ChatType.GROUPS, self.handle_group_document)
        )

        self.pending_media_groups: dict[str, PendingMediaGroup] = {}
        self.pending_media_groups_lock = asyncio.Lock()
        self.recent_chat_texts: dict[tuple[int, int], deque[RecentChatText]] = {}
        self.recent_chat_texts_lock = asyncio.Lock()
        self.denied_chat_ids: set[int] = set()
        self.denied_chat_ids_lock = asyncio.Lock()

    async def handle_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_member_update = update.my_chat_member
        if not chat_member_update:
            return

        chat = chat_member_update.chat
        old_status = chat_member_update.old_chat_member.status
        new_status = chat_member_update.new_chat_member.status
        print(
            "[bot-membership]"
            f" chat_id={chat.id}"
            f" title={chat.title!r}"
            f" old_status={old_status}"
            f" new_status={new_status}"
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_group_allowed(update=update, bot=context.bot):
            return
        if update.effective_message:
            await update.effective_message.reply_text(
                "Бот активен. Пришлите .doc/.docx/.xlsx/.pdf/.rar в группу. "
                "Если файлов несколько в одном сообщении, сделаю общее саммари. "
                "Текст перед пакетом (например ссылка на закупку и цена) тоже учитываю."
            )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_group_allowed(update=update, bot=context.bot):
            return
        if update.effective_message:
            await update.effective_message.reply_text(
                "Я обрабатываю .doc/.docx/.xlsx/.pdf/.rar в группе и возвращаю саммари по тендерной документации. "
                "Для пакета файлов также учитываю последнее текстовое сообщение автора."
            )

    async def handle_group_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_group_allowed(update=update, bot=context.bot):
            return
        message = update.effective_message
        user = update.effective_user
        if not message or not user or user.is_bot:
            return

        text = (message.text or "").strip()
        if not text:
            return

        await self.store_recent_text_message(
            chat_id=message.chat_id,
            user_id=user.id,
            message_id=message.message_id,
            text=text,
            message_date=message.date,
        )

    async def handle_group_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_group_allowed(update=update, bot=context.bot):
            return
        message = update.effective_message
        user = update.effective_user
        if not message or not message.document:
            return

        document = message.document
        file_name = document.file_name or "document.bin"
        extension = detect_document_extension(file_name=file_name, mime_type=document.mime_type)
        if not is_supported_upload_extension(extension):
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
                sender_user_id=user.id if user else None,
                source_message_id=message.message_id,
                bot=context.bot,
            )
            return

        await self.process_single_document(
            message=message,
            payload=payload,
            bot=context.bot,
        )

    async def process_single_document(
        self,
        message: Message,
        payload: DocumentPayload,
        bot: Bot,
    ) -> None:
        status_message = await message.reply_text("Получил документ, извлекаю текст...")
        try:
            extracted_parts = await self.extract_payload_texts(payload=payload, bot=bot)
            await status_message.edit_text("Текст извлечен, готовлю саммари...")
            fallback_name = payload.file_name
            if len(extracted_parts) > 1:
                fallback_name = f"{payload.file_name} ({len(extracted_parts)} файла)"
            summary = await self.summarize_extracted_parts(
                extracted_parts=extracted_parts,
                fallback_name=fallback_name,
            )
            formatted_summary = format_summary_for_telegram(summary)

            await status_message.delete()
            for part in split_for_telegram(formatted_summary):
                await message.reply_text(part, parse_mode="HTML")
        except (DocumentExtractionError, ArchiveExtractionError):
            logger.exception("Document extraction failed")
            await status_message.edit_text(
                "Не удалось извлечь текст из файла. Для .doc нужен LibreOffice/antiword/catdoc, "
                "для .rar нужен unrar/7z/bsdtar/unar. "
                "Для сканированных .pdf может понадобиться OCR."
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
        sender_user_id: int | None,
        source_message_id: int,
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
                    source_user_id=sender_user_id,
                    first_message_id=source_message_id,
                )
                self.pending_media_groups[media_group_id] = pending
            else:
                pending.first_message_id = min(pending.first_message_id, source_message_id)
                if pending.source_user_id is None:
                    pending.source_user_id = sender_user_id

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
            context_text = await self.find_recent_text_message(
                chat_id=pending.chat_id,
                user_id=pending.source_user_id,
                before_message_id=pending.first_message_id,
            )
            summary = await self.build_combined_summary(
                documents=pending.documents,
                context_text=context_text,
                bot=bot,
            )
            formatted_summary = format_summary_for_telegram(summary)

            await status_message.delete()
            for part in split_for_telegram(formatted_summary):
                await bot.send_message(chat_id=pending.chat_id, text=part, parse_mode="HTML")
        except (DocumentExtractionError, ArchiveExtractionError):
            logger.exception("Media group extraction failed")
            await status_message.edit_text(
                "Не удалось извлечь текст из пакета документов."
            )
        except Exception:
            logger.exception("Failed to process media group")
            await status_message.edit_text(
                "Не удалось обработать пакет документов. Проверьте формат файлов и ключ API."
            )

    async def build_combined_summary(
        self,
        documents: list[DocumentPayload],
        context_text: str | None,
        bot: Bot,
    ) -> str:
        extracted_parts: list[tuple[str, str]] = []
        failed_files: list[str] = []

        for payload in documents:
            try:
                parts = await self.extract_payload_texts(payload=payload, bot=bot)
                extracted_parts.extend(parts)
            except Exception as exc:
                failed_files.append(f"{payload.file_name}: {exc}")

        if not extracted_parts:
            raise DocumentExtractionError("No files were extracted")

        summary = await self.summarize_extracted_parts(
            extracted_parts=extracted_parts,
            fallback_name=f"Пакет документов ({len(extracted_parts)} файла)",
            context_text=context_text,
        )

        if failed_files:
            failures_text = "\n".join(f"- {item}" for item in failed_files)
            summary = f"{summary}\n\nНе обработаны файлы:\n{failures_text}"

        return summary

    async def store_recent_text_message(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        text: str,
        message_date: datetime,
    ) -> None:
        normalized_date = normalize_datetime(message_date)
        key = (chat_id, user_id)
        async with self.recent_chat_texts_lock:
            history = self.recent_chat_texts.get(key)
            if history is None:
                history = deque(maxlen=CONTEXT_BUFFER_SIZE)
                self.recent_chat_texts[key] = history

            history.append(
                RecentChatText(
                    message_id=message_id,
                    text=text,
                    date=normalized_date,
                    has_procurement_link=contains_procurement_link(text),
                )
            )
            self.cleanup_text_history(history)

    async def find_recent_text_message(
        self,
        chat_id: int,
        user_id: int | None,
        before_message_id: int,
    ) -> str | None:
        if user_id is None:
            return None

        key = (chat_id, user_id)
        async with self.recent_chat_texts_lock:
            history = self.recent_chat_texts.get(key)
            if not history:
                return None

            self.cleanup_text_history(history)
            candidates = [item for item in history if item.message_id < before_message_id]
            if not candidates:
                return None

            procurement_candidates = [item for item in candidates if item.has_procurement_link]
            if procurement_candidates:
                return max(procurement_candidates, key=lambda item: item.message_id).text

            return max(candidates, key=lambda item: item.message_id).text

    def cleanup_text_history(self, history: deque[RecentChatText]) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CONTEXT_MESSAGE_MAX_AGE_SECONDS)
        while history and history[0].date < cutoff:
            history.popleft()

    async def ensure_group_allowed(self, update: Update, bot: Bot) -> bool:
        chat = update.effective_chat
        if chat is None:
            return True

        if chat.type not in {"group", "supergroup"}:
            return True

        message = update.effective_message
        if message:
            print(
                "[group-message]"
                f" chat_id={message.chat.id}"
                f" message_id={message.message_id}"
            )

        if not self.settings.whitelist_chat_ids:
            return True

        if chat.id in self.settings.whitelist_chat_ids:
            return True

        await self.notify_and_leave_unauthorized_chat(chat_id=chat.id, bot=bot)
        return False

    async def notify_and_leave_unauthorized_chat(self, chat_id: int, bot: Bot) -> None:
        async with self.denied_chat_ids_lock:
            if chat_id in self.denied_chat_ids:
                return
            self.denied_chat_ids.add(chat_id)

        try:
            await bot.send_message(chat_id=chat_id, text=UNAUTHORIZED_CHAT_TEXT)
        except Exception:
            logger.exception("Failed to send unauthorized message for chat %s", chat_id)

        try:
            await bot.leave_chat(chat_id=chat_id)
        except Exception:
            logger.exception("Failed to leave unauthorized chat %s", chat_id)

    async def extract_payload_texts(
        self,
        payload: DocumentPayload,
        bot: Bot,
    ) -> list[tuple[str, str]]:
        if payload.extension in SUPPORTED_EXTENSIONS:
            text = await self.download_and_extract_text(payload=payload, bot=bot)
            if not text.strip():
                raise DocumentExtractionError(f"{payload.file_name}: пустой текст")
            return [(payload.file_name, text)]

        if payload.extension in SUPPORTED_ARCHIVE_EXTENSIONS:
            return await self.download_and_extract_archive_texts(payload=payload, bot=bot)

        raise DocumentExtractionError(f"{payload.file_name}: unsupported extension {payload.extension}")

    async def summarize_extracted_parts(
        self,
        extracted_parts: list[tuple[str, str]],
        fallback_name: str,
        context_text: str | None = None,
    ) -> str:
        if len(extracted_parts) == 1 and not context_text:
            return await self.summarizer.summarize(
                text=extracted_parts[0][1],
                file_name=extracted_parts[0][0],
            )

        sections: list[str] = []
        if context_text:
            sections.append(
                "### Контекст из сообщения перед пакетом\n"
                f"{context_text}"
            )

        sections.extend(
            f"### Документ {idx}: {name}\n{text}"
            for idx, (name, text) in enumerate(extracted_parts, start=1)
        )
        combined_text = "\n\n".join(sections)

        return await self.summarizer.summarize(
            text=combined_text,
            file_name=fallback_name,
        )

    async def download_and_extract_archive_texts(
        self,
        payload: DocumentPayload,
        bot: Bot,
    ) -> list[tuple[str, str]]:
        temp_path: Path | None = None
        try:
            telegram_file = await bot.get_file(payload.file_id)
            with tempfile.NamedTemporaryFile(
                suffix=payload.extension or ".rar",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)

            await telegram_file.download_to_drive(custom_path=str(temp_path))
            extracted = extract_archive_document_texts(temp_path)
            return [
                (f"{payload.file_name} / {inner_name}", text)
                for inner_name, text in extracted
            ]
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

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


def format_summary_for_telegram(text: str) -> str:
    lines = text.splitlines()
    formatted: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            formatted.append("")
            continue

        cleaned = stripped.lstrip("-• ").strip()
        if is_summary_heading(cleaned):
            heading, separator, tail = cleaned.partition(":")
            heading_html = f"<b>{html.escape((heading + separator).strip())}</b>"
            if tail.strip():
                formatted.append(f"{heading_html} {html.escape(tail.strip())}")
            else:
                formatted.append(heading_html)
            continue

        formatted.append(html.escape(stripped))

    return "\n".join(formatted).strip()


def is_summary_heading(line: str) -> bool:
    normalized = line.lower().strip()
    return any(normalized.startswith(prefix) for prefix in SUMMARY_HEADING_PREFIXES)


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
    if normalized_mime == "application/pdf":
        return ".pdf"
    if normalized_mime in {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return ".xlsx"
    if normalized_mime in {"application/vnd.rar", "application/x-rar-compressed"}:
        return ".rar"

    return ""


def is_supported_upload_extension(extension: str) -> bool:
    return extension in SUPPORTED_EXTENSIONS or extension in SUPPORTED_ARCHIVE_EXTENSIONS


def contains_procurement_link(text: str) -> bool:
    return bool(PROCUREMENT_LINK_PATTERN.search(text))


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
