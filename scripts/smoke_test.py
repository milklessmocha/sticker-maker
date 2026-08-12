"""Offline verification of the whole bot: pipeline, handlers, config, cache.

    .venv/bin/python scripts/smoke_test.py

Needs no BOT_TOKEN, no network and no Telegram: rembg is replaced with a
synthetic cutout and the Telegram transport with a fake session that records
the API calls the handlers make. Exits non-zero if any check fails, so it works
as a pre-deploy gate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# --------------------------------------------------------------------------
# Replace rembg with a deterministic elliptical cutout. Real weights are
# exercised by scripts/try_pipeline.py; here we want speed and repeatability.
# --------------------------------------------------------------------------
_rembg = types.ModuleType("rembg")
_calls: dict[str, list[str]] = {"new_session": [], "remove": []}


def _new_session(model, *args, **kwargs):
    _calls["new_session"].append(model)
    return {"model": model}


def _remove(data, session=None, post_process_mask=False, **kwargs):
    _calls["remove"].append(type(data).__name__)
    assert session is not None, "remove() must be called with the preloaded session"
    image = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
    image = image.convert("RGBA")
    w, h = image.size
    mask = Image.new("L", (w, h), 0)
    # Off-centre and non-square, so trim/scale/centre all get exercised.
    ImageDraw.Draw(mask).ellipse((w * 0.10, h * 0.30, w * 0.55, h * 0.95), fill=255)
    out = image.copy()
    out.putalpha(mask)
    return out


_rembg.new_session = _new_session
_rembg.remove = _remove
sys.modules["rembg"] = _rembg

DATA_DIR = Path(tempfile.mkdtemp(prefix="sticker-smoke-"))
os.environ.update(
    BOT_TOKEN="123456789:AABBCCDDEEFFgghhiijjkkllmmnnoopp",
    ALLOWED_USER_ID="42",
    DATA_DIR=str(DATA_DIR),
    LOG_LEVEL="WARNING",
    NOTIFY_UNAUTHORIZED="false",
)

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.methods import TelegramMethod  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User  # noqa: E402

import processor as processor_mod  # noqa: E402
from config import OutputFormat, SettingsStore, load_config  # noqa: E402
from keyboards import OutputCallback, SettingsCallback, output_keyboard, settings_keyboard  # noqa: E402
from main import build_dispatcher  # noqa: E402
from processor import ResultCache, StickerProcessor  # noqa: E402

OK: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (OK if condition else FAIL).append(f"{name}{(' — ' + detail) if detail else ''}")


class FakeSession(BaseSession):
    """Records the Telegram methods handlers invoke and returns canned replies."""

    def __init__(self) -> None:
        super().__init__()
        # (method, request_timeout) — aiogram passes the timeout alongside the
        # method rather than as an attribute on it.
        self.calls: list[tuple[TelegramMethod, float | None]] = []
        self._next_message_id = 100

    def names(self) -> list[str]:
        return [type(call).__name__ for call, _ in self.calls]

    def by_name(self, name: str) -> list[TelegramMethod]:
        return [call for call, _ in self.calls if type(call).__name__ == name]

    def timeout_for(self, name: str) -> float | None:
        for call, timeout in self.calls:
            if type(call).__name__ == name:
                return timeout
        return None

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append((method, timeout))
        name = type(method).__name__
        if name == "GetMe":
            return User(id=7, is_bot=True, first_name="Sticker", username="sticker_bot")
        if name in {"SendMessage", "SendSticker", "SendDocument", "EditMessageText"}:
            if name == "EditMessageText":
                message_id, chat_id = method.message_id, method.chat_id
            else:
                message_id, chat_id = self._next_message_id, method.chat_id
                self._next_message_id += 1
            return Message(
                message_id=message_id,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            ).as_(bot)
        return True


def make_update(update_id: int, *, user_id: int = 42, **message_fields) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=10 + update_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Hayk"),
            **message_fields,
        ),
    )


def make_photo_update(update_id: int, user_id: int = 42) -> Update:
    return make_update(
        update_id,
        user_id=user_id,
        photo=[PhotoSize(file_id="file-1", file_unique_id="u1", width=900, height=1200, file_size=90_000)],
    )


def make_callback_update(update_id: int, data: str, message: Message, user_id: int = 42) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb{update_id}",
            from_user=User(id=user_id, is_bot=False, first_name="Hayk"),
            chat_instance="ci",
            data=data,
            message=message,
        ),
    )


def source_photo_bytes() -> bytes:
    image = Image.new("RGB", (900, 1200), (30, 120, 200))
    ImageDraw.Draw(image).rectangle((100, 400, 500, 1100), fill=(240, 60, 60))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


async def main() -> int:
    config = load_config()
    check("load_config", config.canvas_size == 512 and 42 in config.allowed_user_ids)

    # --- callback payloads ------------------------------------------------
    packed = OutputCallback(fmt=OutputFormat.BOTH).pack()
    check("OutputCallback.pack", packed == "out:both", packed)
    check("OutputCallback.unpack", OutputCallback.unpack(packed).fmt is OutputFormat.BOTH)
    check("SettingsCallback.pack", SettingsCallback(fmt=OutputFormat.ASK).pack() == "cfg:ask")
    check("callback data <= 64 bytes", all(len(p) <= 64 for p in (packed, "cfg:sticker")))
    check("output_keyboard", len(output_keyboard().inline_keyboard) == 3)
    marks = [b.text for row in settings_keyboard(OutputFormat.PNG).inline_keyboard for b in row]
    check("settings_keyboard marks current", sum(m.startswith("✅") for m in marks) == 1, str(marks))

    # --- pipeline ---------------------------------------------------------
    proc = StickerProcessor(config)
    await proc.start()
    check("session preloaded once", _calls["new_session"] == [config.rembg_model], str(_calls["new_session"]))
    check("warm-up ran", len(_calls["remove"]) == 1)

    result = await proc.process(source_photo_bytes())
    check("remove() got a PIL image", _calls["remove"][-1] == "Image", _calls["remove"][-1])

    webp = Image.open(io.BytesIO(result.webp))
    png = Image.open(io.BytesIO(result.png))
    check("webp is 512x512", webp.size == (512, 512), str(webp.size))
    check("png is 512x512 RGBA", png.size == (512, 512) and png.mode == "RGBA", f"{png.size} {png.mode}")
    check("webp under 512 KB", len(result.webp) <= config.max_sticker_bytes, f"{len(result.webp)}B")

    rgba = png.convert("RGBA")
    pixels = rgba.load()
    check("corner transparent", pixels[2, 2][3] == 0, str(pixels[2, 2]))
    check("centre opaque", pixels[256, 256][3] > 250, str(pixels[256, 256]))

    bbox = rgba.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    long_side = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    check("subject+outline fills canvas", long_side >= 500, f"bbox={bbox}")
    check("centred horizontally", abs(bbox[0] - (512 - bbox[2])) <= 2, f"bbox={bbox}")
    check("centred vertically", abs(bbox[1] - (512 - bbox[3])) <= 2, f"bbox={bbox}")

    row = bbox[1] + (bbox[3] - bbox[1]) // 2
    opaque_xs = [x for x in range(512) if pixels[x, row][3] > 200]
    left = opaque_xs[0]
    ring = [pixels[left + offset, row] for offset in range(3, 9)]
    check("white outline present", all(px[:3] == (255, 255, 255) for px in ring), str(ring[:2]))
    subject_px = pixels[left + config.outline_radius + 6, row]
    check("subject over outline", subject_px[:3] != (255, 255, 255), str(subject_px))

    # dilation fallback when cv2 is unavailable
    saved_cv2 = processor_mod.cv2
    processor_mod.cv2 = None
    fallback = await proc.process(source_photo_bytes())
    processor_mod.cv2 = saved_cv2
    fb = Image.open(io.BytesIO(fallback.png))
    check("MaxFilter fallback works", fb.size == (512, 512) and fb.getchannel("A").getbbox() is not None)

    saved_remove = processor_mod.remove
    processor_mod.remove = lambda data, session=None, **kw: Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    try:
        await proc.process(source_photo_bytes())
        check("empty cutout rejected", False, "no error raised")
    except processor_mod.ProcessingError as exc:
        check("empty cutout rejected", "couldn't find a subject" in str(exc), str(exc))
    finally:
        processor_mod.remove = saved_remove

    def _boom(*args, **kwargs):
        raise RuntimeError("onnxruntime exploded")

    processor_mod.remove = _boom
    try:
        await proc.process(source_photo_bytes())
        check("inference failure wrapped", False, "no error raised")
    except processor_mod.ProcessingError:
        check("inference failure wrapped", True)
    finally:
        processor_mod.remove = saved_remove

    try:
        await proc.process(b"not an image at all")
        check("garbage upload rejected", False, "no error raised")
    except processor_mod.ProcessingError:
        check("garbage upload rejected", True)

    # --- settings persistence --------------------------------------------
    store = SettingsStore(config.settings_path)
    await store.load()
    check("default is ASK", store.get(42) is OutputFormat.ASK)
    await store.set(42, OutputFormat.PNG)
    reloaded = SettingsStore(config.settings_path)
    await reloaded.load()
    check("settings persist", reloaded.get(42) is OutputFormat.PNG)
    check("settings file written", config.settings_path.exists())

    # --- result cache -----------------------------------------------------
    cache = ResultCache(ttl_seconds=900, max_items=2)
    cache.put("a", result)
    cache.put("b", result)
    cache.put("c", result)
    check("cache evicts oldest", cache.get("a") is None and cache.get("c") is result)
    expiring = ResultCache(ttl_seconds=-1, max_items=4)
    expiring.put("a", result)
    check("cache honours ttl", expiring.get("a") is None)

    # --- handlers through the real dispatcher -----------------------------
    session = FakeSession()
    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    prefs = SettingsStore(config.settings_path.with_name("dispatcher.json"))
    await prefs.load()
    dispatcher = build_dispatcher(config, proc, prefs)
    check(
        "resolve_used_update_types",
        set(dispatcher.resolve_used_update_types()) == {"message", "callback_query"},
        str(dispatcher.resolve_used_update_types()),
    )

    async def fake_download(file, *args, **kwargs):
        return io.BytesIO(source_photo_bytes())

    bot.download = fake_download  # type: ignore[method-assign]

    session.calls.clear()
    await dispatcher.feed_update(bot, make_photo_update(1, user_id=999))
    check("stranger ignored", session.calls == [], str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(bot, make_update(2, text="/start"))
    sent = session.by_name("SendMessage")
    check("/start replies", len(sent) == 1 and "Sticker Maker" in (sent[0].text or ""), str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(bot, make_update(20, text="/help"))
    check("/help replies", len(session.by_name("SendMessage")) == 1, str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(bot, make_update(3, text="/settings"))
    check("/settings shows keyboard", session.by_name("SendMessage")[0].reply_markup is not None)

    session.calls.clear()
    panel = Message(message_id=200, date=dt.datetime.now(dt.UTC), chat=Chat(id=42, type="private")).as_(bot)
    await dispatcher.feed_update(bot, make_callback_update(4, "cfg:sticker", panel))
    check("settings callback answered", "AnswerCallbackQuery" in session.names(), str(session.names()))
    check("settings callback edits panel", "EditMessageText" in session.names())
    check("settings callback stored", prefs.get(42) is OutputFormat.STICKER)

    session.calls.clear()
    await dispatcher.feed_update(bot, make_photo_update(5))
    check(
        "default format skips the prompt",
        session.names() == ["SendMessage", "SendSticker", "DeleteMessage"],
        str(session.names()),
    )
    check(
        "upload timeout reaches transport",
        session.timeout_for("SendSticker") == int(config.upload_timeout),
        f"{session.timeout_for('SendSticker')} vs {int(config.upload_timeout)}",
    )

    await prefs.set(42, OutputFormat.ASK)
    session.calls.clear()
    await dispatcher.feed_update(bot, make_photo_update(6))
    check("status message sent", "Processing" in (session.by_name("SendMessage")[0].text or ""))
    edits = session.by_name("EditMessageText")
    check("prompt keyboard attached", len(edits) == 1 and edits[0].reply_markup is not None, str(session.names()))
    prompt_message_id = edits[0].message_id

    session.calls.clear()
    status = Message(
        message_id=prompt_message_id,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=42, type="private"),
    ).as_(bot)
    await dispatcher.feed_update(bot, make_callback_update(7, "out:both", status))
    check(
        "both formats delivered + status removed",
        session.names() == ["AnswerCallbackQuery", "SendSticker", "SendDocument", "DeleteMessage"],
        str(session.names()),
    )

    session.calls.clear()
    await dispatcher.feed_update(bot, make_callback_update(8, "out:sticker", status))
    answer = session.by_name("AnswerCallbackQuery")
    check("expired result explained", bool(answer) and answer[0].show_alert is True, str(session.names()))
    check("keyboard cleared on expiry", "EditMessageReplyMarkup" in session.names(), str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(
        bot,
        make_update(
            10,
            document={"file_id": "d1", "file_unique_id": "du1", "file_name": "notes.txt", "mime_type": "text/plain"},
        ),
    )
    replies = session.by_name("SendMessage")
    check("non-image document refused", bool(replies) and "isn't an image" in (replies[0].text or ""))

    session.calls.clear()
    await dispatcher.feed_update(
        bot,
        make_update(
            11,
            document={"file_id": "d2", "file_unique_id": "du2", "file_name": "shot.png", "mime_type": "image/png"},
        ),
    )
    check("image document processed", "EditMessageText" in session.names(), str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(
        bot,
        make_update(12, document={"file_id": "d4", "file_unique_id": "du4", "file_name": "photo.HEIC"}),
    )
    check("mime-less .heic accepted", "EditMessageText" in session.names(), str(session.names()))

    session.calls.clear()
    await dispatcher.feed_update(
        bot,
        make_update(
            13,
            document={
                "file_id": "d3",
                "file_unique_id": "du3",
                "file_name": "huge.png",
                "mime_type": "image/png",
                "file_size": 40 * 1024 * 1024,
            },
        ),
    )
    replies = session.by_name("SendMessage")
    check("oversized upload refused", bool(replies) and "too large" in (replies[0].text or ""))

    session.calls.clear()
    await dispatcher.feed_update(bot, make_update(14, text="hello there"))
    replies = session.by_name("SendMessage")
    check("fallback reply", bool(replies) and "Send me a photo" in (replies[0].text or ""))

    await bot.session.close()

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    for line in OK:
        print(f"  ok   {line}")
    for line in FAIL:
        print(f"  FAIL {line}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
