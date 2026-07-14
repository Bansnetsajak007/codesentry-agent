"""Application configuration loaded from the environment via python-dotenv,
exposing a Pydantic ``Settings`` model and a cached ``get_settings()`` singleton
that surfaces the OpenAI credentials, model choice, token budget, and log level."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel


class Settings(BaseModel):
    """Resolved CodeSentry configuration."""

    openai_api_key: str | None = None
    model: str = "gpt-4.1"
    max_tokens: int = 4096
    log_level: str = "INFO"
    openai_base_url: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load configuration from the environment (and a .env file if present) once."""

    load_dotenv()
    return Settings(
        openai_api_key=os.getenv("MODEL_API_KEY") or None,
        model=os.getenv("CODESENTRY_MODEL", "gpt-4.1"),
        max_tokens=int(os.getenv("CODESENTRY_MAX_TOKENS", "4096")),
        log_level=os.getenv("CODESENTRY_LOG_LEVEL", "INFO"),
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
    )
