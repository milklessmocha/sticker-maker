"""Image pipeline: background removal → white outline → 512×512 sticker.

The rembg/ONNX inference and the Pillow work are both CPU-bound and blocking,
so every job runs in a worker thread (`asyncio.to_thread`) behind a semaphore
that caps how many images are in flight at once.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps
from rembg import new_session, remove

from config import Config

log = logging.getLogger(__name__)

try:  # Optional: gives round outline corners and much faster dilation.
    import cv2
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - falls back to pure Pillow
    cv2 = None
    np = None

try:  # Optional: lets iPhone .heic/.heif uploads decode.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ModuleNotFoundError:  # pragma: no cover
    log.debug("pillow-heif is not installed; .heic uploads will be rejected.")


class ProcessingError(RuntimeError):
    """A failure worth showing to the user verbatim."""


@dataclass(frozen=True, slots=True)
class StickerResult:
    """Both export formats for one processed image."""

    webp: bytes
    png: bytes

    def size_for(self, is_sticker: bool) -> int:
        return len(self.webp) if is_sticker else len(self.png)


class StickerProcessor:
    """Owns the ONNX session and turns raw uploads into sticker bytes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_jobs)

    async def start(self) -> None:
        """Create the ONNX session up front so the first photo isn't slow."""
        model = self._config.rembg_model
        log.info("Loading rembg model %r…", model)
        started = time.monotonic()
        self._session = await asyncio.to_thread(new_session, model)
        await asyncio.to_thread(self._warm_up)
        log.info("Model %r ready in %.1fs", model, time.monotonic() - started)

    async def process(self, data: bytes) -> StickerResult:
        if self._session is None:
            raise ProcessingError("The background-removal model isn't loaded yet, try again in a moment.")
        async with self._semaphore:
            started = time.monotonic()
            result = await asyncio.to_thread(self._run, data)
            log.info(
                "Processed image in %.2fs (webp=%.0f KB, png=%.0f KB)",
                time.monotonic() - started,
                len(result.webp) / 1024,
                len(result.png) / 1024,
            )
            return result

    # --- pipeline ---------------------------------------------------------

    def _run(self, data: bytes) -> StickerResult:
        source = self._decode(data)
        cutout = self._remove_background(source)
        subject = self._trim(cutout)
        scaled = self._scale_to_fit(subject)
        outlined = self._add_outline(scaled)
        canvas = self._center(outlined)
        return StickerResult(webp=self._encode_webp(canvas), png=self._encode_png(canvas))

    def _warm_up(self) -> None:
        """Force ONNX graph initialisation with a throwaway inference."""
        try:
            remove(Image.new("RGB", (64, 64), (127, 127, 127)), session=self._session)
        except Exception:  # noqa: BLE001 - warm-up is best effort
            log.warning("Model warm-up failed; the first real image may be slower.", exc_info=True)

    def _decode(self, data: bytes) -> Image.Image:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:  # noqa: BLE001 - Pillow raises many types here
            raise ProcessingError("I couldn't read that file as an image.") from exc

        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")

        # u2net infers on a 320px tensor internally, so a huge source only costs
        # memory in post-processing. Cap the long side before inference.
        longest = max(image.size)
        if longest > self._config.max_input_side:
            scale = self._config.max_input_side / longest
            target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(target, Image.Resampling.LANCZOS)
        return image

    def _remove_background(self, image: Image.Image) -> Image.Image:
        try:
            cutout = remove(image, session=self._session, post_process_mask=True)
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises opaque errors
            raise ProcessingError("Background removal failed on this image.") from exc
        if not isinstance(cutout, Image.Image):  # bytes/ndarray, depending on rembg version
            cutout = Image.open(io.BytesIO(bytes(cutout)))
        return cutout.convert("RGBA")

    def _trim(self, cutout: Image.Image) -> Image.Image:
        """Crop away fully transparent margins so centring is about the subject."""
        threshold = self._config.alpha_threshold
        mask = cutout.getchannel("A").point(lambda value: 255 if value > threshold else 0)
        bbox = mask.getbbox()
        if bbox is None:
            raise ProcessingError(
                "I couldn't find a subject in that image — try a photo with a clearer foreground."
            )
        return cutout.crop(bbox)

    def _outline_pad(self) -> int:
        """Pixels the outline adds on every side (dilation plus blur bleed)."""
        return self._config.outline_radius + round(2 * self._config.outline_softness) + 1

    def _scale_to_fit(self, subject: Image.Image) -> Image.Image:
        """Resize the subject so subject + outline lands exactly in the canvas.

        The spec dilates before scaling; doing it in this order instead keeps the
        stroke a constant width in final-canvas pixels no matter how large the
        upload was (a fixed dilation applied pre-downscale would vanish on a
        4000px photo and swallow a 300px one).
        """
        inner = self._config.canvas_size - 2 * self._outline_pad()
        scale = min(inner / subject.width, inner / subject.height)
        target = (max(1, round(subject.width * scale)), max(1, round(subject.height * scale)))
        return subject.resize(target, Image.Resampling.LANCZOS)

    def _add_outline(self, subject: Image.Image) -> Image.Image:
        pad = self._outline_pad()
        # Pad first, otherwise the dilated mask is clipped at the subject's edges.
        base = ImageOps.expand(subject, border=pad, fill=(0, 0, 0, 0))

        mask = self._dilate(base.getchannel("A"), self._config.outline_radius)
        if self._config.outline_softness > 0:
            # Only affects the last couple of pixels: antialiases the stroke
            # while its interior stays fully opaque white.
            mask = mask.filter(ImageFilter.GaussianBlur(self._config.outline_softness))

        outline = Image.new("RGBA", base.size, (255, 255, 255, 255))
        outline.putalpha(mask)
        return Image.alpha_composite(outline, base)

    @staticmethod
    def _dilate(mask: Image.Image, radius: int) -> Image.Image:
        if radius <= 0:
            return mask
        size = radius * 2 + 1
        if cv2 is not None and np is not None:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
            return Image.fromarray(cv2.dilate(np.array(mask), kernel))
        return mask.filter(ImageFilter.MaxFilter(size))

    def _center(self, image: Image.Image) -> Image.Image:
        size = self._config.canvas_size
        if image.width > size or image.height > size:  # safety net against rounding
            image = ImageOps.contain(image, (size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        return canvas

    # --- encoding ---------------------------------------------------------

    def _encode_webp(self, image: Image.Image) -> bytes:
        """Lossless first, then step down quality until it fits Telegram's cap."""
        limit = self._config.max_sticker_bytes
        best = _save(image, "WEBP", lossless=True, quality=100, method=6)
        if len(best) <= limit:
            return best
        for quality in (95, 90, 85, 80, 70, 60, 50, 40):
            best = _save(image, "WEBP", lossless=False, quality=quality, method=6)
            if len(best) <= limit:
                return best
        log.warning("Could not compress sticker below %d bytes (got %d).", limit, len(best))
        return best

    @staticmethod
    def _encode_png(image: Image.Image) -> bytes:
        return _save(image, "PNG", optimize=True)


def _save(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


@dataclass(slots=True)
class _CacheEntry:
    result: StickerResult
    expires_at: float


class ResultCache:
    """Holds processed images between the status message and the button press.

    Callback payloads are capped at 64 bytes, so results are keyed by the status
    message they belong to. Entries expire; the cache is bounded. Everything runs
    on the event loop thread, so no locking is needed.
    """

    def __init__(self, *, ttl_seconds: float, max_items: int) -> None:
        self._ttl = ttl_seconds
        self._max_items = max_items
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    @staticmethod
    def key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def put(self, key: str, result: StickerResult) -> None:
        self._purge()
        self._entries[key] = _CacheEntry(result=result, expires_at=time.monotonic() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_items:
            self._entries.popitem(last=False)

    def get(self, key: str) -> StickerResult | None:
        self._purge()
        entry = self._entries.get(key)
        return entry.result if entry else None

    def discard(self, key: str) -> None:
        self._entries.pop(key, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [key for key, entry in self._entries.items() if entry.expires_at <= now]:
            del self._entries[key]
