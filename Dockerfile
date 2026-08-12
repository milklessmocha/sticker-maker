# syntax=docker/dockerfile:1
#
# Multi-arch base: builds natively on Oracle Cloud's Ampere A1 (linux/arm64).

# --------------------------------------------------------------------------
# Stage 1 — build the virtualenv (keeps compilers out of the final image)
# --------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Only needed if pip has to fall back to an sdist for this architecture;
# prebuilt aarch64 wheels exist for the whole dependency set today.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /tmp/requirements.txt

# --------------------------------------------------------------------------
# Stage 2 — runtime
# --------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    U2NET_HOME=/models \
    DATA_DIR=/data

# libgomp1      — OpenMP runtime for onnxruntime / scipy
# libglib2.0-0  — required by opencv-python-headless
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /models /data /app

# Bake the weights into the image: the container then starts without network
# access and restarts stay instant. This sits *before* the code COPY on purpose
# — it depends only on the venv, so editing a .py file below reuses this layer
# instead of downloading the model again.
#
# Keep this in sync with REMBG_MODEL at runtime, or the container downloads a
# second model on first use. u2net is ~176 MB; u2netp is ~5 MB and much lighter
# on RAM if you are running on a small instance.
ARG REMBG_MODEL=u2net
RUN python -c "import sys; from rembg import new_session; new_session(sys.argv[1])" "$REMBG_MODEL" \
 && chown -R app:app /models /data

WORKDIR /app
COPY --chown=app:app config.py processor.py keyboards.py handlers.py main.py ./

USER app
STOPSIGNAL SIGTERM
CMD ["python", "-u", "main.py"]
