from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    openai_model: str = "gpt-4.1-mini"
    summary_language: str = "ru"
    max_doc_chars: int = 120_000
    chunk_size: int = 12_000
    chunk_overlap: int = 1_000



def load_settings() -> Settings:
    load_dotenv()

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is not set")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        summary_language=os.getenv("SUMMARY_LANGUAGE", "ru").strip() or "ru",
        max_doc_chars=max(1_000, int(os.getenv("MAX_DOC_CHARS", "120000"))),
        chunk_size=max(2_000, int(os.getenv("CHUNK_SIZE", "12000"))),
        chunk_overlap=max(0, int(os.getenv("CHUNK_OVERLAP", "1000"))),
    )
