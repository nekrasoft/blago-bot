from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    whitelist_chat_ids: frozenset[int]
    openai_model: str = "gpt-4.1-mini"
    summary_language: str = "ru"
    max_doc_chars: int = 120_000
    chunk_size: int = 12_000
    chunk_overlap: int = 1_000



def load_settings() -> Settings:
    load_dotenv()

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    whitelist_chat_ids = parse_whitelist_chat_ids(
        os.getenv("WHITELIST_CHAT_IDS", "")
    )

    if not telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is not set")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        openai_api_key=openai_api_key,
        whitelist_chat_ids=whitelist_chat_ids,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        summary_language=os.getenv("SUMMARY_LANGUAGE", "ru").strip() or "ru",
        max_doc_chars=max(1_000, int(os.getenv("MAX_DOC_CHARS", "120000"))),
        chunk_size=max(2_000, int(os.getenv("CHUNK_SIZE", "12000"))),
        chunk_overlap=max(0, int(os.getenv("CHUNK_OVERLAP", "1000"))),
    )


def parse_whitelist_chat_ids(raw: str) -> frozenset[int]:
    value = raw.strip()
    if not value:
        raise ValueError(
            "WHITELIST_CHAT_IDS is not set. "
            "Provide comma-separated Telegram group chat IDs."
        )

    chat_ids: set[int] = set()
    for token in value.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            chat_ids.add(int(item))
        except ValueError as exc:
            raise ValueError(
                f"Invalid chat ID in WHITELIST_CHAT_IDS: {item!r}"
            ) from exc

    if not chat_ids:
        raise ValueError(
            "WHITELIST_CHAT_IDS does not contain valid IDs."
        )

    return frozenset(chat_ids)
