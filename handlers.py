"""Telegram interface: access control, commands, photo flow, inline callbacks."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    Message,
    PhotoSize,
    TelegramObject,
)

from config import Config, OutputFormat, SettingsStore
from keyboards import OutputCallback, SettingsCallback, output_keyboard, settings_keyboard
from processor import ProcessingError, ResultCache, StickerProcessor, StickerResult

log = logging.getLogger(__name__)

router = Router(name="sticker-maker")

PROCESSING_TEXT = "⏳ Processing image background &amp; generating outline…"
READY_TEXT = "✅ Done! How should I send it?"

START_TEXT = (
    "👋 <b>Sticker Maker</b>\n\n"
    "Send me a photo and I'll cut out the background, add a soft white sticker "
    "outline and return it on a 512×512 canvas.\n\n"
    "<b>How to use</b>\n"
    "• Send a photo (compressed or as a file) — one at a time or a whole album.\n"
    "• Pick <b>Sticker</b>, <b>PNG</b> or <b>Both</b> from the buttons.\n"
    "• To build a pack: forward the sticker to @Stickers and use /newpack.\n\n"
    "<b>Commands</b>\n"
    "/start — this message\n"
    "/settings — set a permanent default output format"
)

SETTINGS_TEXT = "⚙️ <b>Default output format</b>\n\nCurrent: <b>{current}</b>"


class AccessMiddleware(BaseMiddleware):
    """Drops every update that isn't from an allowed Telegram user id."""

    def __init__(self, config: Config) -> None:
        self._allowed = config.allowed_user_ids
        self._notify = config.notify_unauthorized

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user is not None and user.id in self._allowed:
            return await handler(event, data)

        log.warning("Rejected %s from user_id=%s", type(event).__name__, getattr(user, "id", None))
        if self._notify:
            with suppress(Exception):
                if isinstance(event, CallbackQuery):
                    await event.answer("This bot is private.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("This bot is private.")
        return None


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("settings"))
async def cmd_settings(message: Message, settings: SettingsStore) -> None:
    current = settings.get(message.from_user.id)
    await message.answer(
        SETTINGS_TEXT.format(current=current.label),
        reply_markup=settings_keyboard(current),
    )


@router.callback_query(SettingsCallback.filter())
async def on_settings_choice(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    settings: SettingsStore,
) -> None:
    await settings.set(callback.from_user.id, callback_data.fmt)
    await callback.answer("Saved")
    with suppress(TelegramBadRequest):  # e.g. the same option tapped twice
        await callback.message.edit_text(
            SETTINGS_TEXT.format(current=callback_data.fmt.label),
            reply_markup=settings_keyboard(callback_data.fmt),
        )


@router.message(F.photo)
async def on_photo(
    message: Message,
    bot: Bot,
    config: Config,
    processor: StickerProcessor,
    settings: SettingsStore,
    results: ResultCache,
) -> None:
    await _handle_upload(message.photo[-1], message, bot, config, processor, settings, results)


@router.message(F.document)
async def on_document(
    message: Message,
    bot: Bot,
    config: Config,
    processor: StickerProcessor,
    settings: SettingsStore,
    results: ResultCache,
) -> None:
    document = message.document
    if not _is_image_document(document):
        await message.answer("That file isn't an image — send a photo or an image file.")
        return
    await _handle_upload(document, message, bot, config, processor, settings, results)


@router.callback_query(OutputCallback.filter())
async def on_output_choice(
    callback: CallbackQuery,
    callback_data: OutputCallback,
    bot: Bot,
    config: Config,
    results: ResultCache,
) -> None:
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id
    key = ResultCache.key(chat_id, message_id)

    result = results.get(key)
    if result is None:
        await callback.answer("That result expired — send the photo again.", show_alert=True)
        with suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        return

    await callback.answer()
    log.info("Chose %s for chat %s (status message %s)", callback_data.fmt.value, chat_id, message_id)
    try:
        await _deliver(bot, chat_id, result, callback_data.fmt, config.upload_timeout)
    except Exception as exc:  # noqa: BLE001 - report, then keep the bot alive
        log.exception("Failed to deliver %s to chat %s", callback_data.fmt.value, chat_id)
        # The result stays cached, so the buttons below are a working retry.
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_delivery_error_text(exc),
                reply_markup=output_keyboard(),
            )
        return

    results.discard(key)
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Send me a photo and I'll turn it into a sticker. /start for help.")


# --- internals ------------------------------------------------------------


def _is_image_document(document: Document | None) -> bool:
    if document is None:
        return False
    if document.mime_type and document.mime_type.startswith("image/"):
        return True
    name = (document.file_name or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"))


async def _handle_upload(
    file: PhotoSize | Document,
    message: Message,
    bot: Bot,
    config: Config,
    processor: StickerProcessor,
    settings: SettingsStore,
    results: ResultCache,
) -> None:
    if file.file_size and file.file_size > config.max_upload_bytes:
        await message.answer(
            f"That file is too large — Telegram lets bots download up to {config.max_upload_mb} MB."
        )
        return

    status = await message.answer(PROCESSING_TEXT)
    try:
        buffer = await bot.download(file)
        if buffer is None:
            raise ProcessingError("I couldn't download that file from Telegram.")
        result = await processor.process(buffer.read())
    except ProcessingError as exc:
        await _fail(status, f"❌ {exc}")
        return
    except Exception:  # noqa: BLE001 - never let one bad image kill the poller
        log.exception("Unhandled error while processing an upload")
        await _fail(status, "❌ Something went wrong while processing that image.")
        return

    preferred = settings.get(message.from_user.id)
    if preferred is OutputFormat.ASK:
        results.put(ResultCache.key(status.chat.id, status.message_id), result)
        with suppress(TelegramBadRequest):
            await status.edit_text(READY_TEXT, reply_markup=output_keyboard())
        return

    try:
        await _deliver(bot, message.chat.id, result, preferred, config.upload_timeout)
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to deliver %s to chat %s", preferred.value, message.chat.id)
        await _fail(status, _delivery_error_text(exc))
        return

    with suppress(TelegramBadRequest):
        await status.delete()


async def _deliver(
    bot: Bot,
    chat_id: int,
    result: StickerResult,
    fmt: OutputFormat,
    timeout: float,
) -> None:
    # request_timeout overrides aiogram's 60s default, which a slow uplink can
    # blow through on a few hundred KB.
    if fmt.wants_sticker:
        sent = await bot.send_sticker(
            chat_id,
            BufferedInputFile(result.webp, filename="sticker.webp"),
            request_timeout=int(timeout),
        )
        log.info(
            "Sent sticker to chat %s: message_id=%s sticker=%s (%d bytes)",
            chat_id,
            sent.message_id,
            sent.sticker.file_unique_id if sent.sticker else "MISSING",
            len(result.webp),
        )
    if fmt.wants_png:
        sent = await bot.send_document(
            chat_id,
            BufferedInputFile(result.png, filename="sticker.png"),
            caption="Transparent PNG, 512×512",
            request_timeout=int(timeout),
        )
        log.info(
            "Sent PNG to chat %s: message_id=%s document=%s (%d bytes)",
            chat_id,
            sent.message_id,
            sent.document.file_unique_id if sent.document else "MISSING",
            len(result.png),
        )


def _delivery_error_text(exc: BaseException) -> str:
    """A timeout is the network's fault, not Telegram refusing the file."""
    if isinstance(exc, TelegramNetworkError):
        return "❌ The upload to Telegram timed out. Tap a button to try again."
    return "❌ Telegram rejected the upload. Try the other format?"


async def _fail(status: Message, text: str) -> None:
    with suppress(TelegramBadRequest):
        await status.edit_text(text, reply_markup=None)
