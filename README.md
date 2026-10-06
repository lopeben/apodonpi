# NASA APOD Display

Kiosk service for a Raspberry Pi + [Waveshare 3.5" RPi LCD (A)](https://www.waveshare.com/wiki/3.5inch_RPi_LCD_(A)) touchscreen. Fetches NASA's Astronomy Picture of the Day once daily and shows it on the framebuffer — a static image, or a looping video on video days. Tapping the screen restarts the video loop.

## Requirements

**System packages:** `fbi`, `mplayer`, `ffmpeg`, `yt-dlp` (not `youtube-dl` — see [Design notes](#design-notes))

**Python packages:** `requests`, `pytz`, `evdev`, `Pillow`, `apscheduler`

**Hardware paths** (constants at the top of `nasa_apod.py`, adjust if your wiring/OS differs):
- Touchscreen: `/dev/input/event0`
- Framebuffer: `/dev/fb1`

## Configuration

None needed. The script fetches from `https://science.nasa.gov/wp-json/wp/v2/apod-basic/<YYMMDD>` — no API key, no account, no quota. (An older version of this script used `api.nasa.gov/planetary/apod` with a `site.txt` key file; that endpoint has been retired — see [Design notes](#design-notes).)

## Usage

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

`WorkingDirectory=` isn't load-bearing for config anymore (there's no more `site.txt`), but it's still good practice to leave it pointed at the install dir for a consistent `cwd`.

## Design notes

- **The science.nasa.gov migration (Oct 2026).** NASA moved APOD off `api.nasa.gov/planetary/apod` to `science.nasa.gov/apod` on 2026-09-29 (old endpoint fully shuts down 2026-12-01). The old endpoint doesn't error — it returns `HTTP 200` with a generic NASA-logo placeholder for every request, silently ignoring the `date` param, which is why a fetch could look "successful" in the logs while showing the same wrong image every day. The script now calls the new endpoint, `https://science.nasa.gov/wp-json/wp/v2/apod-basic/<YYMMDD>` (note: 2-digit year, in the URL *path*, not a query param). Two field meanings changed along with it:
  - `url` is now the **article page**, not an image — `hdurl` is the real image, served from a resizable CDN (`assets.science.nasa.gov`). `resize_hdurl()` asks it for a display-sized copy instead of the full-resolution original.
  - Video days no longer get a dedicated video URL field at all — `hdurl` on those days is just a generic sitewide placeholder image. `extract_video_url()` scrapes the actual video link out of the `explanation` field's HTML instead.
- **Startup fetch + retries.** The old script only fetched on the 6am cron trigger, so a reboot left the screen blank until the next morning. It now fetches once immediately at startup (with retries, since systemd can start the unit before the network is actually up) in addition to the daily job.
- **`media_type` over guesswork.** Image-vs-video is read directly from the API's `media_type` field instead of inferring it from a `PIL.Image.open()` failure.
- **Placeholder-image fallback.** If video extraction or playback fails for any reason, the script falls back to the (resized) `hdurl` image instead of leaving a blank or garbled screen. On a video day this is just NASA's generic placeholder, not a real thumbnail of that day's video — the new API doesn't provide one.
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

**Same image every day, logs show `media_type=image` with a `nasa-logo@2x.png` or similarly generic URL** → you're hitting the old, retired `api.nasa.gov/planetary/apod` endpoint, which now ignores the date and always returns a placeholder instead of erroring. Confirm `APOD_BASE_URL` in `nasa_apod.py` points at `https://science.nasa.gov/wp-json/wp/v2/apod-basic` (see [Design notes](#design-notes)); if it does and this still happens, NASA's `hdurl` field for that day may itself be a placeholder — check the logged `hdurl` against the article at the `url` it was paired with.

**Requests hang and time out with zero bytes received, despite a clean TLS handshake** → not rate limiting (that's an instant, explicit `429`). This looks like a network-level MTU blackhole — something on the path is dropping full-size packets and swallowing the ICMP message that would normally trigger a resend at a smaller size. Test:
```bash
ping -M do -s 1472 -c 4 <hostname>
```
If that fails but a smaller size (e.g. `-s 1400`) succeeds, it's confirmed — fix by clamping MTU on the relevant interface, or MSS-clamping if this Pi is behind a VPN/PPPoE link.

**Screen doesn't come back after a reboot** → check that only one service instance is enabled, that `WorkingDirectory=` in the unit file is correct, and that the unit has `After=network-online.target` / `Wants=network-online.target` so it isn't racing the network at boot.

**Display looks wrong / was fine before an OS upgrade** → the Waveshare 3.5" (A) driver/overlay setup (`fbtft`, `fbcp` mirroring `fb0`→`fb1`) has changed across Raspberry Pi OS releases (notably around Bookworm). If the OS was ever upgraded, confirm the display driver and `fbcp` (or DRM-based equivalent) are still configured and running as expected — that's independent of anything in this script.

**Manual test run "hangs" with no shell prompt** → expected when run without `--date`. The daemon blocks forever in the touch-listener loop by design (same as it will under systemd). Use `--date` for a one-shot test, or `Ctrl+C` to stop, or background it with `sudo ./nasa_apod.py &`.
