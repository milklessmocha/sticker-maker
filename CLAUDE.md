# Specification: Telegram Sticker Maker Bot

## Executive Summary
Build a private, async Telegram bot using Python 3.11+ (`aiogram` v3) that converts user-uploaded photos into 512x512 Telegram stickers or transparent PNGs. The processing pipeline automatically removes the image background, adds a soft white sticker outline around the subject, centers it on a 512x512 canvas, and delivers the result via interactive inline choices.

---

## 1. Environment & Deployment Target
- **Platform:** Oracle Cloud Always Free VPS (ARM64 / Ampere A1 Flex, 24 GB RAM).
- **Deployment Strategy:** Containerized using `Dockerfile` and `docker-compose.yml`.
- **User Authorization:** Restrict access strictly to the owner's Telegram ID via an `ALLOWED_USER_ID` environment variable. Reject unauthorized users silently or with a brief notification.

---

## 2. Core Image Pipeline Specifications (`processor.py`)

### Step A: Background Removal
- **Library:** `rembg` (installed via `pip install "rembg[cpu]"`).
- **Model:** Use `u2net` (standard model) pre-loaded into an ONNX session at bot startup to ensure fast inference and high-fidelity edge extraction.

### Step B: White Outline Generation
- Extract the alpha channel (transparency mask) from the background-removed image.
- Create an expanded outline layer by applying a dilation filter (e.g., `PIL.ImageFilter.MaxFilter(size=25)` or `cv2.dilate` on the alpha mask).
- Fill the dilated alpha area with solid white `(255, 255, 255, 255)`.
- Composite the original foreground subject over the newly generated white outline.

### Step C: Telegram Canvas Scaling (512×512)
- Preserve the subject's original aspect ratio using high-quality downsampling (`Image.Resampling.LANCZOS`).
- Ensure the output strictly fits inside a 512x512 pixel boundary (where at least one side is exactly 512px and neither side exceeds 512px).
- Center the composite on a transparent `512x512` RGBA canvas.

### Step D: Export Formats
- **Sticker Format:** Standard `.webp` (compressed under 512 KB).
- **Document Format:** Lossless `.png` with full transparency.

---

## 3. User Experience & Telegram Interface (`handlers.py`)

### Command Handlers
- `/start` - Displays a welcome message and usage instructions.
- `/settings` - Lets the user set a permanent default output format (Sticker, PNG, or Ask Every Time).

### Image Handler Flow
1. User sends a photo (as an image or uncompressed file).
2. Bot acknowledges with an editing/processing status message (e.g., `"⏳ Processing image background & generating outline..."`).
3. Bot processes the image and presents an **Inline Keyboard** below the status message:
    - `[ 🎨 Telegram Sticker (.webp) ]`
    - `[ 🖼️ PNG File (.png) ]`
    - `[ ⚡ Send Both ]`
4. Clicking an inline button immediately sends the requested file(s) and deletes or updates the status message.

---

## 4. Required Project Architecture