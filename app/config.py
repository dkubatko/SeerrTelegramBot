"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _clean(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required(name: str) -> str:
    value = _clean(name)
    if not value:
        raise ConfigError(f"{name} is required but was not set")
    return value


def _int(name: str, default: int | None = None) -> int | None:
    value = _clean(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    value = _clean(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def normalize_seerr_url(raw: str) -> str:
    """Accept anything from `host:5055` to `https://host/api/v1/` and return an origin."""
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    for suffix in ("/api/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_api_base: str
    seerr_url: str
    seerr_public_url: str
    seerr_api_key: str
    admin_chat_id: int | None
    webhook_auth_token: str | None
    webhook_path: str
    port: int
    log_level: str
    forward_other_notifications: bool
    notify_on_start: bool
    request_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        path = _clean("WEBHOOK_PATH") or "/webhook"
        if not path.startswith("/"):
            path = f"/{path}"

        seerr_url = normalize_seerr_url(_required("SEERR_URL"))
        public = _clean("SEERR_PUBLIC_URL")

        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            # Overridable for local testing and self-hosted Bot API servers.
            telegram_api_base=(
                _clean("TELEGRAM_API_BASE") or "https://api.telegram.org"
            ).rstrip("/"),
            seerr_url=seerr_url,
            # Used only for "Open in Seerr" link buttons, which are followed by
            # your phone rather than by this container.
            seerr_public_url=normalize_seerr_url(public) if public else seerr_url,
            seerr_api_key=_required("SEERR_API_KEY"),
            admin_chat_id=_int("ADMIN_CHAT_ID"),
            webhook_auth_token=_clean("WEBHOOK_AUTH_TOKEN"),
            webhook_path=path.rstrip("/") or "/webhook",
            port=_int("PORT", 8420),
            log_level=(_clean("LOG_LEVEL") or "INFO").upper(),
            forward_other_notifications=_bool("FORWARD_OTHER_NOTIFICATIONS"),
            notify_on_start=_bool("NOTIFY_ON_START"),
            request_timeout=float(_clean("SEERR_TIMEOUT") or 15.0),
        )
