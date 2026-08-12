# Deploying to Oracle Cloud (Always Free)

A private, single-user bot. It talks to Telegram over **long polling**, so it
opens no inbound ports and needs no domain, TLS certificate, or firewall rule.

## Why Oracle A1 and not a free PaaS

The bot loads a ~176 MB ONNX model and peaks around 1 GB of RAM, and it must stay
awake to hold its polling connection. That rules out the usual free tiers: Render,
Koyeb and similar sleep idle services (polling dies) and cap free RAM at 512 MB;
Railway and Fly are trials or usage-billed. Google Cloud's free `e2-micro` has
1 GB RAM total, which is too tight next to a 1.65 GB image.

Oracle's **Always Free A1 Flex** (Ampere ARM64, up to 4 OCPU / 24 GB across your
tenancy) is the only permanently free option with enough memory and no sleeping.

Two viable alternatives: run it on hardware you already own (a Raspberry Pi 4/5 or
an old laptop — same `docker compose` flow, no sleeping, and nothing to pay), or
set `REMBG_MODEL=u2netp` to cut the model to ~5 MB and the memory footprint with
it, which makes a 1 GB instance realistic at some cost in cutout quality.

## 1. Create the instance

- **Shape:** `VM.Standard.A1.Flex` — 2 OCPU / 12 GB is comfortable; 1 OCPU / 6 GB
  is enough. Both sit inside Always Free.
- **Image:** Ubuntu 22.04 or 24.04 (**aarch64**). Oracle Linux ships podman rather
  than Docker, so Ubuntu keeps the steps below shorter.
- **Networking:** accept the defaults. Do **not** open any ingress port — SSH is
  all you need, and the bot dials out only.
- If you hit `Out of host capacity`, that is Oracle rationing A1, not a mistake on
  your side. Retry, or try a different availability domain in the same region.

ARM64 is verified: all 47 packages in `requirements.txt` resolve to prebuilt
`aarch64` wheels, so nothing compiles from source during the build.

## 2. One-time server setup

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
```

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker $USER && sudo systemctl enable --now docker
```

Log out and back in so the group change applies, then confirm:

```bash
docker run --rm hello-world
```

## 3. Copy the project up

From your Mac, in the project directory:

```bash
rsync -av --exclude .venv --exclude .git --exclude data --exclude __pycache__ ./ ubuntu@YOUR_SERVER_IP:~/sticker-maker/
```

That carries your `.env` over SSH along with the code. If you would rather not
copy the secret, exclude it too and write `.env` on the server by hand — only
`BOT_TOKEN` and `ALLOWED_USER_ID` are required.

## 4. Build and start

```bash
cd ~/sticker-maker && mkdir -p data && sudo chown 1000:1000 data
```

The `chown` matters: the container runs as uid 1000 and needs to write
`data/settings.json`. On Ubuntu the `ubuntu` user is already uid 1000, so this is
usually a no-op — harmless either way.

```bash
docker compose up -d --build
```

First build takes a while: roughly 1 GB of wheels plus the model download. Later
builds reuse every layer except the code copy.

```bash
docker compose logs -f
```

Success looks like this, after ~40 s of quiet while Python imports scipy and numba:

```
Starting sticker-maker (model=u2net, canvas=512px, allowed users=1)
Model 'u2net' ready in 1.6s
Authorised as @YourBot (id=...)
aiogram.dispatcher: Run polling for bot @YourBot
```

Then message the bot a photo. `Processed image in N.Ns` in the log is the
end-to-end confirmation.

## 5. Operating it

| Task | Command |
|---|---|
| Follow logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Update after a code change | `git pull` (or rsync again), then `docker compose up -d --build` |
| Disk usage | `docker system df` |

`restart: unless-stopped` plus an enabled Docker service means the bot comes back
by itself after a reboot or a crash. Logs are capped at 3 × 10 MB, so they cannot
fill the boot volume.

## Failure modes worth recognising

**`Telegram rejected BOT_TOKEN (401 Unauthorized)`** — the token is wrong or
truncated. Restarting will not help; get a fresh one from @BotFather. The
container exits with code 3 rather than looping on a stack trace.

**Nothing in the logs for 40+ seconds on startup** — normal. That is the
scientific stack importing, before the first log line.

**`The upload to Telegram timed out`** — a slow uplink, not a rejected file. The
result stays cached, so tapping the button again retries. Raise
`UPLOAD_TIMEOUT_SECONDS` if it persists.

**Idle reclamation.** Oracle may reclaim Always Free compute instances that look
idle over a period of days, and a personal bot is idle almost all the time.
Converting the tenancy to Pay As You Go exempts you from that policy while the
Always Free resources stay free. Worth doing if you care about uptime.

**Memory.** `mem_limit: 4g` in `docker-compose.yml` assumes an instance with more
than 4 GB. Lower it, or drop it, if you provision a 1 GB shape.
