# tapo-care-backup

Unofficial, personal CLI for backing up **your own** Tapo Care cloud recordings from a TP-Link/Tapo account.

This project was built as a safer, Kan-usable rewrite inspired by [`dimme/tapo-cli`](https://github.com/dimme/tapo-cli), without copying its unlicensed source. It targets the practical flow:

1. Log in with your TP-Link ID.
2. List Tapo camera devices.
3. Query Tapo Care cloud video clips.
4. Download MPEG-TS video streams and decrypt AES-128-CBC payloads when Tapo returns a decryption key.

> This is not affiliated with TP-Link or Tapo. Tapo Care cloud-video endpoints are private/mobile-app endpoints and may change without notice.

## What works

- `login` — cache a TP-Link cloud token locally at `~/.config/tapo-care-backup/session.json` with `0600` permissions.
- `devices` — list camera devices in the account.
- `list` — list Tapo Care cloud video clips for a time window.
- `download` — paginate through all clips in the selected window and download them into date/camera folders as `.ts` files.
- `doctor` — unauthenticated endpoint probe; a `401 token invalid` response means the regional Tapo Care endpoint is reachable.

## Important constraints

- Use the **owner TP-Link account**. Tapo shared accounts generally cannot access Tapo Care cloud recordings.
- Tapo Care stores event clips, not full 24/7 recordings. For continuous future recording, RTSP/ONVIF + NAS/NVR is usually more stable.
- Some Tapo accounts/regions/firmware may require newer signed mobile auth. This repo includes signing helpers, but does **not** ship mobile-app client keys in the public repo.
- The default region fallback is `aps1`, which is likely the useful default for Japan/APAC accounts. If login returns an `appServerUrl`, the region is auto-detected.

## Install / run

```bash
git clone https://github.com/kandotrun/tapo-care-backup.git
cd tapo-care-backup
uv run tapo-care-backup --help
```

## Usage

### 1. Log in

Interactive:

```bash
uv run tapo-care-backup login
```

Or with environment variables:

```bash
export TAPO_USERNAME='you@example.com'
export TAPO_PASSWORD='your-password'
uv run tapo-care-backup login
```

The token is saved to:

```text
~/.config/tapo-care-backup/session.json
```

### 2. Confirm the Tapo Care endpoint for Japan/APAC

```bash
uv run tapo-care-backup doctor --region aps1
```

Expected unauthenticated result:

```text
HTTP 401 {"code":15000,"message":"token invalid, expired or replaced"}
```

### 3. List cameras

```bash
uv run tapo-care-backup devices
```

### 4. List cloud clips

```bash
uv run tapo-care-backup list --days 7 --timezone Asia/Tokyo
```

### 5. Download cloud clips

```bash
uv run tapo-care-backup download --days 7 --timezone Asia/Tokyo --path ~/TapoBackups
```

Existing files are skipped by default. To re-download:

```bash
uv run tapo-care-backup download --days 7 --path ~/TapoBackups --overwrite
```

If device discovery is flaky but you know the camera `deviceId`:

```bash
uv run tapo-care-backup download --device-id 'YOUR_DEVICE_ID' --days 7 --path ~/TapoBackups
```

### 6. Monitor new clips every few minutes

`scripts/tapo_care_watch.py` is a cron-friendly watcher. It stays silent when there are no new clips, downloads any newly-seen clips as `.ts` backups from Tapo cameras and doorbells, and prints `MEDIA:/path/to/file.mp4` lines so Hermes/Slack can attach new recordings. Transient network timeouts from Tapo's private API are retried with exponential backoff; if they still fail, the cron tick stays silent and the next run retries naturally. When `ffmpeg` is available, the watcher remuxes only the notification-bound clips to sibling `.mp4` files without re-encoding. Set `TAPO_WATCH_NOTIFY_EVENT_TYPES=person` (Tapo returns this as `PD`) to keep saving every new clip while sending Slack/Hermes notifications only for person-detection clips. Set `TAPO_WATCH_GRID_ATTACHMENTS=1` to combine all notified clips into one grid MP4 attachment and notify all event categories, even if a legacy person-only filter is still present in the env file. `TAPO_WATCH_MAX_ATTACHMENTS` only limits the Slack text list / individual-attachment fallback; it does not limit the grid contents. For one-by-one delivery, set `TAPO_WATCH_NOTIFY_CLIPS_PER_RUN=1`, `TAPO_WATCH_MAX_ATTACHMENTS=1`, and `TAPO_WATCH_GRID_ATTACHMENTS=0`, then schedule the cron every minute (`* * * *`). New clips are still backed up immediately; Slack notifications drain one saved clip per run.

Create a local-only env file. Do **not** commit it:

```bash
mkdir -p ~/.config/tapo-care-backup
chmod 700 ~/.config/tapo-care-backup
cat > ~/.config/tapo-care-backup/monitor.env <<'EOF'
TAPO_USERNAME=you@example.com
TAPO_PASSWORD=your-password
TAPO_WATCH_OUTPUT_DIR=/home/kan/TapoBackups
TAPO_WATCH_DAYS=1
TAPO_WATCH_TIMEZONE=Asia/Tokyo
TAPO_WATCH_MAX_ATTACHMENTS=3
TAPO_WATCH_ATTACHMENT_FORMAT=mp4
TAPO_WATCH_NOTIFY_EVENT_TYPES=all
TAPO_WATCH_GRID_ATTACHMENTS=1
TAPO_WATCH_GRID_TILE_WIDTH=480
TAPO_WATCH_GRID_TILE_HEIGHT=270
TAPO_WATCH_BOOTSTRAP=mark_seen
EOF
chmod 600 ~/.config/tapo-care-backup/monitor.env
```

Run once manually:

```bash
uv run python scripts/tapo_care_watch.py
```

Default behavior is privacy-safe for first run: existing clips in the configured window are marked as already seen, and only future new clips are downloaded/shared.

## Signed auth mode

Default login uses the older TP-Link cloud login flow because it does not require publishing Tapo mobile-app client keys.

If your account requires MFA or the legacy login path stops working, the CLI also has a signed-auth path. Signed auth intentionally requires you to provide the reverse-engineered Tapo mobile client material through environment variables instead of hardcoding it in this public repo:

```bash
export TAPO_CLIENT_ACCESS_KEY='...'
export TAPO_CLIENT_SECRET='...'
uv run tapo-care-backup login --auth-mode signed
```

Those values are **not TP-Link account credentials**, but this repo still avoids committing them because they are reverse-engineered app-client material and may be sensitive/unstable.

## Development

```bash
uv run pytest -q
uv build
```

The tests mock behavior and do not require a Tapo account.

## Security notes

- Never commit `session.json`, TP-Link passwords, tokens, or downloaded videos.
- Do not expose RTSP/ONVIF ports directly to the internet. Use VPN if remote access is needed.
- This tool is intended only for backing up recordings from accounts/devices you own or are explicitly authorized to administer.
