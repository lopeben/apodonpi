# NASA APOD Display

Kiosk service for a Raspberry Pi + [Waveshare 3.5" RPi LCD (A)](https://www.waveshare.com/wiki/3.5inch_RPi_LCD_(A)) touchscreen. Fetches NASA's Astronomy Picture of the Day once daily and shows it on the framebuffer — a static image, or a looping video on video days. Tapping the screen restarts the video loop.

## Requirements

**System packages:** `fbi`, `mplayer`, `ffmpeg`, `yt-dlp` (not `youtube-dl` — see [Design notes](#design-notes))

**Python packages:** `requests`, `pytz`, `evdev`, `Pillow`, `apscheduler`

**Hardware paths** (constants at the top of `nasa_apod.py`, adjust if your wiring/OS differs):
- Touchscreen: `/dev/input/event0`
- Framebuffer: `/dev/fb1`

## Configuration

`site.txt` must sit next to `nasa_apod.py` (the script reads it as a relative path). Python-literal dict, not JSON:

```python
{'url': 'https://api.nasa.gov/planetary/apod', 'key': 'YOUR_API_KEY'}
```

Get a free personal key at [api.nasa.gov](https://api.nasa.gov) — 1,000 requests/hour. `DEMO_KEY` works too but is limited to 30/hour and 50/day per IP, shared with everyone else using it, and will throttle you fast during testing.

## Usage

Must be run from the directory containing `site.txt`:

```bash
# Normal operation: runs forever — daily 6am fetch + touch listener
sudo ./nasa_apod.py

# Fetch and display one specific day, then exit (for testing/backfill)
sudo ./nasa_apod.py --date 2026-07-13
```

`--date` accepts `YYYY-MM-DD`, valid from `1995-06-16` (APOD's first day) through today.

Root is required for `/dev/input/event0`, `/dev/fb1`, and `/var/log/nasa_apod.log` access.

## Installing as a systemd service

1. `cd` into the install directory and note the absolute path (`pwd`).
2. Edit `nasa-apod.service`, replacing `/REPLACE/WITH/INSTALL/DIR` (two places) with that path.
3. Check for and disable any previously-installed unit first — two instances running at once will fight over the framebuffer/touch device:
   ```bash
   systemctl list-units --type=service --all | grep -i apod
   sudo systemctl stop <old-unit-name>
   sudo systemctl disable <old-unit-name>
   ```
4. Install:
   ```bash
   sudo cp nasa-apod.service /etc/systemd/system/nasa-apod.service
   sudo systemctl daemon-reload
   sudo systemctl enable nasa-apod.service
   sudo systemctl start nasa-apod.service
   ```
5. Check it:
   ```bash
   sudo systemctl status nasa-apod.service
   journalctl -u nasa-apod.service -f
   ```

`WorkingDirectory=` in the unit file matters — without it, systemd runs the script from `/`, and the relative `./site.txt` read fails immediately.

## Design notes

- **Startup fetch + retries.** The old script only fetched on the 6am cron trigger, so a reboot left the screen blank until the next morning. It now fetches once immediately at startup (with retries, since systemd can start the unit before the network is actually up) in addition to the daily job.
- **`media_type` over guesswork.** Image-vs-video is now read directly from the API's `media_type` field instead of inferring it from a `PIL.Image.open()` failure.
- **Thumbnail fallback.** The API's `thumbs=True` param returns a `thumbnail_url` for video days. If video download/playback fails for any reason, the script falls back to that static image instead of leaving a blank or garbled screen.
- **Direct video vs. yt-dlp.** Some video days link straight to an `.mp4`/`.webm` hosted on NASA's own servers rather than a YouTube/Vimeo embed — those download directly with `requests`, bypassing `yt-dlp` entirely. `yt-dlp` (not the unmaintained `youtube-dl`) is only used for actual embed links.
- **Looping playback.** Video plays via `mplayer -loop 0`, launched as a non-blocking background process (`Popen`, not `subprocess.run`) so it doesn't tie up the scheduler or the touch listener. Tapping the screen restarts the loop from the beginning. Switching to a new day's content (or shutting down) always stops any running loop first via `stop_video()`, which also sweeps for orphaned `mplayer` processes from a previous crash/run.
- **Clean shutdown.** Both `SIGINT` (Ctrl+C) and `SIGTERM` (`systemctl stop`/`restart`) trigger the same cleanup path, so a service restart doesn't leave a looping `mplayer` process behind.
- **Retry-with-backoff on the metadata fetch itself** (inside `fetch_apod()`), so a single slow/failed API response doesn't kill the whole day's run — applies uniformly to the cron job, startup fetch, and manual `--date` runs.

## Logs

```bash
tail -f /var/log/nasa_apod.log
journalctl -u nasa-apod.service -f
```

## Troubleshooting

**Fast `HTTP 429` errors** → actual rate limiting. Check quota without waiting for one:
```bash
curl -sD - -o /dev/null "https://api.nasa.gov/planetary/apod?api_key=YOUR_KEY"
```
Look for `X-RateLimit-Remaining` in the response headers.

**Requests hang and time out with zero bytes received, despite a clean TLS handshake** → not rate limiting (that's an instant, explicit `429`). This looks like a network-level MTU blackhole — something on the path is dropping full-size packets and swallowing the ICMP message that would normally trigger a resend at a smaller size. Test:
```bash
ping -M do -s 1472 -c 4 <hostname>
```
If that fails but a smaller size (e.g. `-s 1400`) succeeds, it's confirmed — fix by clamping MTU on the relevant interface, or MSS-clamping if this Pi is behind a VPN/PPPoE link.

**Screen doesn't come back after a reboot** → check that only one service instance is enabled, that `WorkingDirectory=` in the unit file is correct, and that the unit has `After=network-online.target` / `Wants=network-online.target` so it isn't racing the network at boot.

**Display looks wrong / was fine before an OS upgrade** → the Waveshare 3.5" (A) driver/overlay setup (`fbtft`, `fbcp` mirroring `fb0`→`fb1`) has changed across Raspberry Pi OS releases (notably around Bookworm). If the OS was ever upgraded, confirm the display driver and `fbcp` (or DRM-based equivalent) are still configured and running as expected — that's independent of anything in this script.

**Manual test run "hangs" with no shell prompt** → expected when run without `--date`. The daemon blocks forever in the touch-listener loop by design (same as it will under systemd). Use `--date` for a one-shot test, or `Ctrl+C` to stop, or background it with `sudo ./nasa_apod.py &`.