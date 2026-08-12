"""Environment configuration and persisted per-user preferences."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


class OutputFormat(str, Enum):
    """What the bot should hand back for a processed image."""

    STICKER = "sticker"
    PNG = "png"
    BOTH = "both"
    ASK = "ask"

    @property
    def wants_sticker(self) -> bool:
        return self in (OutputFormat.STICKER, OutputFormat.BOTH)

    @property
    def wants_png(self) -> bool:
        return self in (OutputFormat.PNG, OutputFormat.BOTH)

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS: dict[OutputFormat, str] = {
    OutputFormat.STICKER: "🎨 Telegram Sticker (.webp)",
    OutputFormat.PNG: "🖼️ PNG File (.png)",
    OutputFormat.BOTH: "⚡ Send Both",
    OutputFormat.ASK: "❓ Ask Every Time",
}

# Settings only exposes the three defaults named in the spec; BOTH stays
# available as a per-image choice.
SETTINGS_CHOICES: tuple[OutputFormat, ...] = (
    OutputFormat.STICKER,
    OutputFormat.PNG,
    OutputFormat.ASK,
)

DEFAULT_OUTPUT_FORMAT = OutputFormat.ASK


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise ConfigError(f"{name} is required — copy .env.example to .env and fill it in.")
    return default or ""


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_user_ids(name: str) -> frozenset[int]:
    raw = _env_str(name, required=True)
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"{name} must be a numeric Telegram user id (comma-separated for several), got {chunk!r}."
            ) from exc
    if not ids:
        raise ConfigError(f"{name} must contain at least one Telegram user id.")
    return frozenset(ids)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable snapshot of the environment, built once at startup."""

    bot_token: str
    allowed_user_ids: frozenset[int]
    notify_unauthorized: bool

    data_dir: Path
    rembg_model: str

    canvas_size: int
    outline_radius: int
    outline_softness: float
    alpha_threshold: int
    max_input_side: int

    max_upload_mb: int
    max_sticker_bytes: int
    upload_timeout: float
    max_concurrent_jobs: int
    result_ttl_seconds: int
    result_cache_size: int

    log_level: str

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_config() -> Config:
    """Read and validate the environment. Raises ConfigError on bad input."""
    config = Config(
        bot_token=_env_str("BOT_TOKEN", required=True),
        allowed_user_ids=_env_user_ids("ALLOWED_USER_ID"),
        notify_unauthorized=_env_bool("NOTIFY_UNAUTHORIZED", True),
        data_dir=Path(_env_str("DATA_DIR", "./data")).expanduser(),
        rembg_model=_env_str("REMBG_MODEL", "u2net"),
        canvas_size=_env_int("CANVAS_SIZE", 512, minimum=64),
        # Radius in final-canvas pixels; 12 matches the spec's MaxFilter(size=25).
        outline_radius=_env_int("OUTLINE_RADIUS", 12, minimum=0),
        outline_softness=_env_float("OUTLINE_SOFTNESS", 1.5, minimum=0.0),
        alpha_threshold=_env_int("ALPHA_THRESHOLD", 8, minimum=0),
        max_input_side=_env_int("MAX_INPUT_SIDE", 2000, minimum=512),
        # Telegram's Bot API caps bot downloads at 20 MB.
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 20, minimum=1),
        max_sticker_bytes=_env_int("MAX_STICKER_KB", 512, minimum=32) * 1024,
        # aiogram defaults to 60s, which a slow uplink can exceed on a 200 KB file.
        upload_timeout=_env_float("UPLOAD_TIMEOUT_SECONDS", 180.0, minimum=10.0),
        max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 2, minimum=1),
        result_ttl_seconds=_env_int("RESULT_TTL_SECONDS", 900, minimum=30),
        result_cache_size=_env_int("RESULT_CACHE_SIZE", 32, minimum=1),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )

    if config.alpha_threshold > 254:
        raise ConfigError("ALPHA_THRESHOLD must be <= 254.")
    # The outline grows the subject on every side, so it has to fit the canvas.
    reserved = 2 * (config.outline_radius + round(2 * config.outline_softness) + 1)
    if config.canvas_size - reserved < 64:
        raise ConfigError(
            "OUTLINE_RADIUS/OUTLINE_SOFTNESS are too large for CANVAS_SIZE — "
            f"they reserve {reserved}px of a {config.canvas_size}px canvas."
        )
    return config


class SettingsStore:
    """Per-user default output format, persisted as JSON under DATA_DIR.

    All reads are in-memory and synchronous; writes go through a lock and a
    temp-file rename so a crash mid-write cannot truncate the file.
    """

    def __init__(self, path: Path, *, default: OutputFormat = DEFAULT_OUTPUT_FORMAT) -> None:
        self._path = path
        self._default = default
        self._prefs: dict[int, OutputFormat] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)
        log.info("Loaded preferences for %d user(s) from %s", len(self._prefs), self._path)

    def get(self, user_id: int) -> OutputFormat:
        return self._prefs.get(user_id, self._default)

    async def set(self, user_id: int, fmt: OutputFormat) -> None:
        async with self._lock:
            self._prefs[user_id] = fmt
            await asyncio.to_thread(self._write_sync)

    def _load_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable settings file %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("Ignoring settings file %s: expected a JSON object.", self._path)
            return
        for key, value in raw.items():
            try:
                self._prefs[int(key)] = OutputFormat(value)
            except (TypeError, ValueError):
                log.warning("Skipping invalid settings entry %r: %r", key, value)

    def _write_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(user_id): fmt.value for user_id, fmt in sorted(self._prefs.items())}
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path)
