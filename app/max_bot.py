from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.enums.chat_type import ChatType
from maxapi.enums.parse_mode import ParseMode
from maxapi.types import (
    BotAdded,
    BotRemoved,
    BotStarted,
    Command,
    MessageCreated,
)
from maxapi.types.attachments.file import File as MaxFile
from maxapi.types.message import MessageBody

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

FILE_GROUP_WAIT_SECONDS = 2.0
CONTEXT_MESSAGE_MAX_AGE_MS = 30 * 60 * 1000
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
    download_url: str
    file_name: str
    extension: str


@dataclass
class PendingFileGroup:
    status_mid: str
    chat_id: int
    source_user_id: int | None
    first_message_seq: int
    documents: list[DocumentPayload] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass
class RecentChatText:
    message_seq: int
    text: str
    timestamp: int
    has_procurement_link: bool


class TenderMaxBot:
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
        self.bot = Bot(token=settings.max_bot_token)
        self.dp = Dispatcher()
        self._register_handlers()

        self.pending_file_groups: dict[tuple[int, int], PendingFileGroup] = {}
        self.pending_file_groups_lock = asyncio.Lock()
        self.recent_chat_texts: dict[tuple[int, int], deque[RecentChatText]] = {}
        self.recent_chat_texts_lock = asyncio.Lock()
        self.denied_chat_ids: set[int] = set()
        self.denied_chat_ids_lock = asyncio.Lock()

    def _register_handlers(self) -> None:
        self.dp.bot_added()(self.handle_bot_added)
        self.dp.bot_removed()(self.handle_bot_removed)
        self.dp.bot_started()(self.handle_bot_started)
        self.dp.message_created(Command("start"))(self.handle_start)
        self.dp.message_created(Command("help"))(self.handle_help)
        self.dp.message_created()(self.handle_message)

    # ------------------------------------------------------------------
    # Обработчики событий бота
    # ------------------------------------------------------------------

    async def handle_bot_added(self, event: BotAdded) -> None:
        print(
            "[bot-membership]"
            f" chat_id={event.chat_id}"
            f" action=added"
        )

    async def handle_bot_removed(self, event: BotRemoved) -> None:
        print(
            "[bot-membership]"
            f" chat_id={event.chat_id}"
            f" action=removed"
        )

    async def handle_bot_started(self, event: BotStarted) -> None:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=(
                "Бот активен. Пришлите .doc/.docx/.xls/.xlsx/.pdf/.rar в группу. "
                "Если файлов несколько в одном сообщении, сделаю общее саммари. "
                "Текст перед пакетом (например ссылка на закупку и цена) тоже учитываю."
            ),
        )

    async def handle_start(self, event: MessageCreated) -> None:
        if not await self._ensure_chat_allowed(event):
            return
        await event.message.answer(
            text=(
                "Бот активен. Пришлите .doc/.docx/.xls/.xlsx/.pdf/.rar в группу. "
                "Если файлов несколько в одном сообщении, сделаю общее саммари. "
                "Текст перед пакетом (например ссылка на закупку и цена) тоже учитываю."
            ),
        )

    async def handle_help(self, event: MessageCreated) -> None:
        if not await self._ensure_chat_allowed(event):
            return
        await event.message.answer(
            text=(
                "Я обрабатываю .doc/.docx/.xls/.xlsx/.pdf/.rar в группе и возвращаю "
                "саммари по тендерной документации. "
                "Для пакета файлов также учитываю последнее текстовое сообщение автора."
            ),
        )

    async def handle_message(self, event: MessageCreated) -> None:
        if not await self._ensure_chat_allowed(event):
            return

        message = event.message
        sender = message.sender
        if not sender or sender.is_bot:
            return

        chat_id = message.recipient.chat_id
        if chat_id is None:
            return

        if message.recipient.chat_type != ChatType.CHAT:
            return

        user_id = sender.user_id
        body = message.body
        if body is None:
            return

        file_payloads = self._extract_file_payloads(body)

        if file_payloads:
            await self._queue_file_group(
                event=event,
                chat_id=chat_id,
                user_id=user_id,
                message_seq=body.seq,
                payloads=file_payloads,
            )
            return

        text = (body.text or "").strip()
        if text:
            await self._store_recent_text_message(
                chat_id=chat_id,
                user_id=user_id,
                message_seq=body.seq,
                text=text,
                timestamp=message.timestamp,
            )

    # ------------------------------------------------------------------
    # Извлечение файловых вложений из сообщения
    # ------------------------------------------------------------------

    def _extract_file_payloads(self, body: MessageBody) -> list[DocumentPayload]:
        if not body.attachments:
            return []

        payloads: list[DocumentPayload] = []
        for attachment in body.attachments:
            if not isinstance(attachment, MaxFile):
                continue
            file_name = attachment.filename or "document.bin"
            extension = Path(file_name).suffix.lower()
            if not _is_supported_upload_extension(extension):
                continue
            if attachment.payload is None:
                continue
            download_url = getattr(attachment.payload, "url", None)
            if not download_url:
                continue
            payloads.append(DocumentPayload(
                download_url=download_url,
                file_name=file_name,
                extension=extension,
            ))
        return payloads

    # ------------------------------------------------------------------
    # Группировка файлов по таймеру (аналог media_group в Telegram)
    # ------------------------------------------------------------------

    async def _queue_file_group(
        self,
        event: MessageCreated,
        chat_id: int,
        user_id: int,
        message_seq: int,
        payloads: list[DocumentPayload],
    ) -> None:
        group_key = (chat_id, user_id)

        async with self.pending_file_groups_lock:
            pending = self.pending_file_groups.get(group_key)
            if pending is None:
                status_result = await event.message.reply(
                    text="Получил документ, обрабатываю...",
                )
                if status_result is None or status_result.message.body is None:
                    return
                pending = PendingFileGroup(
                    status_mid=status_result.message.body.mid,
                    chat_id=chat_id,
                    source_user_id=user_id,
                    first_message_seq=message_seq,
                )
                self.pending_file_groups[group_key] = pending
            else:
                pending.first_message_seq = min(
                    pending.first_message_seq, message_seq
                )

            for payload in payloads:
                if any(
                    d.download_url == payload.download_url
                    for d in pending.documents
                ):
                    continue
                pending.documents.append(payload)

            if pending.task and not pending.task.done():
                pending.task.cancel()

            pending.task = asyncio.create_task(
                self._finalize_file_group(group_key=group_key)
            )

    async def _finalize_file_group(
        self, group_key: tuple[int, int]
    ) -> None:
        try:
            await asyncio.sleep(FILE_GROUP_WAIT_SECONDS)
        except asyncio.CancelledError:
            return

        async with self.pending_file_groups_lock:
            pending = self.pending_file_groups.pop(group_key, None)

        if pending is None:
            return

        status_mid = pending.status_mid
        is_single = len(pending.documents) == 1

        try:
            if is_single:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text="Получил документ, извлекаю текст...",
                )
                payload = pending.documents[0]
                extracted_parts = await self._extract_payload_texts(payload)

                await self.bot.edit_message(
                    message_id=status_mid,
                    text="Текст извлечен, готовлю саммари...",
                )

                fallback_name = payload.file_name
                if len(extracted_parts) > 1:
                    fallback_name = (
                        f"{payload.file_name} ({len(extracted_parts)} файла)"
                    )
                summary = await self._summarize_extracted_parts(
                    extracted_parts=extracted_parts,
                    fallback_name=fallback_name,
                )
            else:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text=(
                        f"Пакет получен ({len(pending.documents)} файлов), "
                        "извлекаю текст из всех файлов..."
                    ),
                )
                context_text = await self._find_recent_text_message(
                    chat_id=pending.chat_id,
                    user_id=pending.source_user_id,
                    before_message_seq=pending.first_message_seq,
                )
                summary = await self._build_combined_summary(
                    documents=pending.documents,
                    context_text=context_text,
                )

            formatted_summary = _format_summary_html(summary)
            await self.bot.delete_message(message_id=status_mid)

            for part in _split_message_text(formatted_summary):
                await self.bot.send_message(
                    chat_id=pending.chat_id,
                    text=part,
                    parse_mode=ParseMode.HTML,
                )
        except (DocumentExtractionError, ArchiveExtractionError):
            logger.exception("Ошибка извлечения текста из документов")
            if is_single:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text=(
                        "Не удалось извлечь текст из файла. "
                        "Для .doc нужен LibreOffice/antiword/catdoc, "
                        "для .rar нужен unrar/7z/bsdtar/unar. "
                        "Для сканированных .pdf может понадобиться OCR."
                    ),
                )
            else:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text="Не удалось извлечь текст из пакета документов.",
                )
        except Exception:
            logger.exception("Ошибка обработки документов")
            if is_single:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text="Не удалось обработать файл. Проверьте формат документа и ключ API.",
                )
            else:
                await self.bot.edit_message(
                    message_id=status_mid,
                    text="Не удалось обработать пакет документов. Проверьте формат файлов и ключ API.",
                )

    # ------------------------------------------------------------------
    # Извлечение текста из файлов
    # ------------------------------------------------------------------

    async def _extract_payload_texts(
        self,
        payload: DocumentPayload,
    ) -> list[tuple[str, str]]:
        if payload.extension in SUPPORTED_EXTENSIONS:
            text = await self._download_and_extract_text(payload)
            if not text.strip():
                raise DocumentExtractionError(
                    f"{payload.file_name}: пустой текст"
                )
            return [(payload.file_name, text)]

        if payload.extension in SUPPORTED_ARCHIVE_EXTENSIONS:
            return await self._download_and_extract_archive_texts(payload)

        raise DocumentExtractionError(
            f"{payload.file_name}: unsupported extension {payload.extension}"
        )

    async def _download_and_extract_text(
        self, payload: DocumentPayload
    ) -> str:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=payload.extension or ".bin",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)

            await _download_file(payload.download_url, temp_path)
            return extract_document_text(temp_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    async def _download_and_extract_archive_texts(
        self,
        payload: DocumentPayload,
    ) -> list[tuple[str, str]]:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=payload.extension or ".rar",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)

            await _download_file(payload.download_url, temp_path)
            extracted = extract_archive_document_texts(temp_path)
            return [
                (f"{payload.file_name} / {inner_name}", text)
                for inner_name, text in extracted
            ]
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Саммаризация
    # ------------------------------------------------------------------

    async def _summarize_extracted_parts(
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

    async def _build_combined_summary(
        self,
        documents: list[DocumentPayload],
        context_text: str | None,
    ) -> str:
        extracted_parts: list[tuple[str, str]] = []
        failed_files: list[str] = []

        for payload in documents:
            try:
                parts = await self._extract_payload_texts(payload)
                extracted_parts.extend(parts)
            except Exception as exc:
                failed_files.append(f"{payload.file_name}: {exc}")

        if not extracted_parts:
            raise DocumentExtractionError("No files were extracted")

        summary = await self._summarize_extracted_parts(
            extracted_parts=extracted_parts,
            fallback_name=f"Пакет документов ({len(extracted_parts)} файла)",
            context_text=context_text,
        )

        if failed_files:
            failures_text = "\n".join(f"- {item}" for item in failed_files)
            summary = f"{summary}\n\nНе обработаны файлы:\n{failures_text}"

        return summary

    # ------------------------------------------------------------------
    # Буфер контекстных текстовых сообщений
    # ------------------------------------------------------------------

    async def _store_recent_text_message(
        self,
        chat_id: int,
        user_id: int,
        message_seq: int,
        text: str,
        timestamp: int,
    ) -> None:
        key = (chat_id, user_id)
        async with self.recent_chat_texts_lock:
            history = self.recent_chat_texts.get(key)
            if history is None:
                history = deque(maxlen=CONTEXT_BUFFER_SIZE)
                self.recent_chat_texts[key] = history

            history.append(
                RecentChatText(
                    message_seq=message_seq,
                    text=text,
                    timestamp=timestamp,
                    has_procurement_link=_contains_procurement_link(text),
                )
            )
            _cleanup_text_history(history)

    async def _find_recent_text_message(
        self,
        chat_id: int,
        user_id: int | None,
        before_message_seq: int,
    ) -> str | None:
        if user_id is None:
            return None

        key = (chat_id, user_id)
        async with self.recent_chat_texts_lock:
            history = self.recent_chat_texts.get(key)
            if not history:
                return None

            _cleanup_text_history(history)
            candidates = [
                item
                for item in history
                if item.message_seq < before_message_seq
            ]
            if not candidates:
                return None

            procurement_candidates = [
                item for item in candidates if item.has_procurement_link
            ]
            if procurement_candidates:
                return max(
                    procurement_candidates,
                    key=lambda item: item.message_seq,
                ).text

            return max(
                candidates, key=lambda item: item.message_seq
            ).text

    # ------------------------------------------------------------------
    # Проверка доступа чата (whitelist)
    # ------------------------------------------------------------------

    async def _ensure_chat_allowed(self, event: MessageCreated) -> bool:
        message = event.message
        chat_id = message.recipient.chat_id

        if message.recipient.chat_type != ChatType.CHAT:
            return True

        if chat_id is None:
            return True

        body = message.body
        if body:
            print(
                "[group-message]"
                f" chat_id={chat_id}"
                f" message_mid={body.mid}"
            )

        if not self.settings.whitelist_chat_ids:
            return True

        if chat_id in self.settings.whitelist_chat_ids:
            return True

        await self._notify_and_leave_unauthorized_chat(chat_id=chat_id)
        return False

    async def _notify_and_leave_unauthorized_chat(
        self, chat_id: int
    ) -> None:
        async with self.denied_chat_ids_lock:
            if chat_id in self.denied_chat_ids:
                return
            self.denied_chat_ids.add(chat_id)

        try:
            await self.bot.send_message(
                chat_id=chat_id, text=UNAUTHORIZED_CHAT_TEXT
            )
        except Exception:
            logger.exception(
                "Не удалось отправить сообщение об ограничении для чата %s",
                chat_id,
            )

        try:
            await self.bot.delete_me_from_chat(chat_id=chat_id)
        except Exception:
            logger.exception(
                "Не удалось покинуть неавторизованный чат %s", chat_id
            )

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)


# ======================================================================
# Вспомогательные функции
# ======================================================================


async def _download_file(url: str, dest_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            dest_path.write_bytes(data)


def _split_message_text(text: str, limit: int = 3800) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
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


def _format_summary_html(text: str) -> str:
    lines = text.splitlines()
    formatted: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            formatted.append("")
            continue

        cleaned = stripped.lstrip("-• ").strip()
        if _is_summary_heading(cleaned):
            heading, separator, tail = cleaned.partition(":")
            heading_html = (
                f"<b>{html.escape((heading + separator).strip())}</b>"
            )
            if tail.strip():
                formatted.append(
                    f"{heading_html} {html.escape(tail.strip())}"
                )
            else:
                formatted.append(heading_html)
            continue

        formatted.append(html.escape(stripped))

    return "\n".join(formatted).strip()


def _is_summary_heading(line: str) -> bool:
    normalized = line.lower().strip()
    return any(
        normalized.startswith(prefix) for prefix in SUMMARY_HEADING_PREFIXES
    )


def _is_supported_upload_extension(extension: str) -> bool:
    return (
        extension in SUPPORTED_EXTENSIONS
        or extension in SUPPORTED_ARCHIVE_EXTENSIONS
    )


def _contains_procurement_link(text: str) -> bool:
    return bool(PROCUREMENT_LINK_PATTERN.search(text))


def _cleanup_text_history(history: deque[RecentChatText]) -> None:
    cutoff_ms = (
        int(datetime.now(timezone.utc).timestamp() * 1000)
        - CONTEXT_MESSAGE_MAX_AGE_MS
    )
    while history and history[0].timestamp < cutoff_ms:
        history.popleft()
