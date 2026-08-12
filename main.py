"""Entry point: wires config, the image processor and the Telegram poller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage

import handlers
from config import Config, ConfigError, SettingsStore, load_config
from handlers import AccessMiddleware
from processor import ResultCache, StickerProcessor

log = logging.getLogger("sticker-maker")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def build_dispatcher(config: Config, processor: StickerProcessor, settings: SettingsStore) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Injected into handlers by parameter name.
    dispatcher["config"] = config
    dispatcher["processor"] = processor
    dispatcher["settings"] = settings
    dispatcher["results"] = ResultCache(
        ttl_seconds=config.result_ttl_seconds,
        max_items=config.result_cache_size,
    )

    access = AccessMiddleware(config)
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)

    dispatcher.include_router(handlers.router)
    return dispatcher


async def preflight(bot: Bot) -> None:
    """Identify the account and clear any webhook before polling starts.

    A rejected token is fatal — restarting cannot fix it. A network hiccup is
    not: long polling reconnects on its own, so we log and carry on rather than
    turning a blip at boot into a crash loop.
    """
    try:
        me = await bot.get_me()
        log.info("Authorised as @%s (id=%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramUnauthorizedError:
        raise
    except (TelegramNetworkError, asyncio.TimeoutError) as exc:
        log.warning("Telegram unreachable during startup (%s); polling will retry.", exc)


async def run() -> None:
    config = load_config()
    setup_logging(config.log_level)
    log.info(
        "Starting sticker-maker (model=%s, canvas=%dpx, allowed users=%d)",
        config.rembg_model,
        config.canvas_size,
        len(config.allowed_user_ids),
    )

    settings = SettingsStore(config.settings_path)
    await settings.load()

    processor = StickerProcessor(config)
    await processor.start()

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher(config, processor, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)

    try:
        await preflight(bot)
        polling = asyncio.create_task(
            dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types()),
            name="polling",
        )
        waiter = asyncio.create_task(stop.wait(), name="shutdown-signal")
        await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)

        for task in (polling, waiter):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if polling.done() and not polling.cancelled():
            polling.result()  # re-raise a polling crash
    finally:
        log.info("Shutting down…")
        await bot.session.close()


def main() -> int:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except TelegramUnauthorizedError:
        print(
            "Telegram rejected BOT_TOKEN (401 Unauthorized).\n"
            "Get a fresh token from @BotFather (/mybots → API Token, or /revoke) "
            "and update .env — restarting will not help until you do.",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
