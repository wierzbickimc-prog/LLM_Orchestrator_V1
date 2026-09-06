from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Backend:
    alias: str
    base_url: str
    model_id: str
    api_key: str | None = None
    reasoning: str | None = None
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    provider: str = "local"
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    scout: Backend
    builder: Backend


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _optional_bool(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_bool_or(name: str, default: bool) -> bool:
    value = _optional_bool(name)
    return default if value is None else value


def load_settings() -> Settings:
    return Settings(
        host=os.getenv("ROUTER_HOST", "127.0.0.1"),
        port=int(os.getenv("ROUTER_PORT", "8100")),
        scout=Backend(
            alias=os.getenv("SCOUT_ALIAS", "scout"),
            base_url=os.getenv("SCOUT_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
            model_id=os.getenv("SCOUT_MODEL_ID", "scout"),
            api_key=_optional("SCOUT_API_KEY"),
            reasoning=_optional("SCOUT_REASONING") or "auto",
            enable_thinking=_optional_bool_or("SCOUT_ENABLE_THINKING", True),
        ),
        builder=Backend(
            alias=os.getenv("BUILDER_ALIAS", "builder"),
            base_url=os.getenv("BUILDER_BASE_URL", "http://127.0.0.1:8002/v1").rstrip("/"),
            model_id=os.getenv("BUILDER_MODEL_ID", "builder"),
            api_key=_optional("BUILDER_API_KEY"),
            reasoning=_optional("BUILDER_REASONING"),
            enable_thinking=_optional_bool("BUILDER_ENABLE_THINKING"),
        ),
    )
