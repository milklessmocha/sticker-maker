"""Run the real processing pipeline on a local image, without Telegram.

    .venv/bin/python scripts/try_pipeline.py photo.jpg [more.png ...]

Writes <name>.sticker.webp and <name>.sticker.png next to each input, plus a
<name>.preview.png that composites the result over a dark checkerboard so the
white outline is actually visible.
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from config import load_config
from processor import ProcessingError, StickerProcessor


def checkerboard(size: tuple[int, int], step: int = 32) -> Image.Image:
    board = Image.new("RGBA", size, (38, 42, 50, 255))
    dark = Image.new("RGBA", (step, step), (28, 31, 37, 255))
    for y in range(0, size[1], step):
        for x in range(0, size[0], step):
            if (x // step + y // step) % 2:
                board.paste(dark, (x, y))
    return board


async def main(paths: list[Path]) -> int:
    config = load_config()
    processor = StickerProcessor(config)

    print(f"Loading {config.rembg_model}… (first run downloads ~176 MB of weights)")
    started = time.monotonic()
    await processor.start()
    print(f"Model ready in {time.monotonic() - started:.1f}s\n")

    failures = 0
    for path in paths:
        if not path.is_file():
            print(f"✗ {path}: not a file")
            failures += 1
            continue
        try:
            started = time.monotonic()
            result = await processor.process(path.read_bytes())
        except ProcessingError as exc:
            print(f"✗ {path.name}: {exc}")
            failures += 1
            continue

        webp_path = path.with_suffix(".sticker.webp")
        png_path = path.with_suffix(".sticker.png")
        webp_path.write_bytes(result.webp)
        png_path.write_bytes(result.png)

        sticker = Image.open(io.BytesIO(result.png)).convert("RGBA")
        preview_path = path.with_suffix(".preview.png")
        Image.alpha_composite(checkerboard(sticker.size), sticker).convert("RGB").save(preview_path)

        source = Image.open(path)
        bbox = sticker.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
        print(
            f"✓ {path.name}  {source.width}×{source.height} → {sticker.width}×{sticker.height}"
            f"  in {time.monotonic() - started:.1f}s\n"
            f"    subject+outline bbox {bbox}\n"
            f"    {webp_path.name} {len(result.webp) / 1024:.0f} KB"
            f"  |  {png_path.name} {len(result.png) / 1024:.0f} KB"
            f"  |  preview {preview_path.name}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main([Path(arg) for arg in sys.argv[1:]])))
