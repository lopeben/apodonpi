#!/usr/bin/python3

import os
import re
import sys
import glob
import time
import pytz
import shutil
import signal
import logging
import argparse
import evdev
import requests
import datetime
import functools
import subprocess
import urllib.parse

from PIL import Image
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/var/log/nasa_apod.log'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('nasa_apod')

scheduler = BackgroundScheduler(timezone="Asia/Manila")

TOUCH_DEVICE = '/dev/input/event0'
FB_DEVICE = '/dev/fb1'          # framebuffer mplayer writes video to directly
TEMP_IMAGE = '/tmp/image.jpg'
VIDEO_RAW = '/tmp/videofile'
VIDEO_SCALED = '/tmp/videofile_320p.mp4'

isVideo = False
video_process = None

APOD_MIN_DATE = datetime.date(1995, 6, 16)   # first day APOD ever published

REQUIRED_TOOLS = ['fbi', 'mplayer', 'ffmpeg', 'yt-dlp']

# NASA retired api.nasa.gov/planetary/apod's APOD data on 2026-09-29 (moved to
# science.nasa.gov/apod; full shutdown of the old endpoint is scheduled for
# 2026-12-01). The old endpoint still answers with HTTP 200 but silently
# ignores the date param and always returns the same generic placeholder --
# that's why every fetch started showing the same NASA logo image. This is
# the new WordPress-REST-based replacement: no API key, no quota, but it's
# keyed by path (YYMMDD), not by query params.
APOD_BASE_URL = 'https://science.nasa.gov/wp-json/wp/v2/apod-basic'


def check_dependencies():
    missing = [t for t in REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        log.warning(f"Missing from PATH: {', '.join(missing)} -- related "
                    f"functionality will silently fall back until installed")


def valid_apod_date(value):
    """argparse type= validator: enforce YYYY-MM-DD (the API's format, not
    the ap<YYMMDD>.html scheme used by the apod.nasa.gov archive pages)."""
    try:
        d = datetime.datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' must be in YYYY-MM-DD format")
    today = datetime.datetime.now(pytz.timezone('US/Eastern')).date()
    if d < APOD_MIN_DATE or d > today:
        raise argparse.ArgumentTypeError(f"date must be between {APOD_MIN_DATE} and {today}")
    return d.strftime('%Y-%m-%d')   # normalize e.g. 2026-7-8 -> 2026-07-08 for the API


def fetch_apod(date, retries=3, backoff=5):
    """Fetch APOD metadata for one date (YYYY-MM-DD) from the new
    science.nasa.gov API. It takes the date as part of the path, in a
    2-digit YYMMDD format (2026-10-05 -> 261005), not as a query param."""
    yymmdd = datetime.datetime.strptime(date, '%Y-%m-%d').strftime('%y%m%d')
    url = f'{APOD_BASE_URL}/{yymmdd}'
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            log.warning(f"APOD metadata request attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(backoff)
    raise last_err


def get_date_now():
    dt = datetime.datetime.now(pytz.timezone('US/Eastern'))
    return dt.strftime('%Y-%m-%d')   # zero-padded, matches the API's required YYYY-MM-DD


def resize_hdurl(url, width=960):
    """In the new API, hdurl points at assets.science.nasa.gov, a dynamic
    image CDN that understands w=/h=/fit=/crop= query params -- and the
    hdurl NASA gives us already comes with its own w=/h=/crop= baked in,
    sized for a full desktop display (e.g. w=4222&h=2817&crop=faces).
    Layering a smaller w on top while leaving their h/crop alone would
    fight the aspect ratio or crop oddly, so replace the query entirely
    with just a width + fit=clip (scale down to that width, preserve
    aspect, no cropping) instead of merging into what's there."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    new_query = urllib.parse.urlencode({'w': width, 'fit': 'clip'})
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


VIDEO_HOSTS = ('youtube.com', 'youtu.be', 'vimeo.com')
_HREF_RE = re.compile(r'href="([^"]+)"')


def extract_video_url(explanation_html):
    """Unlike the old API, video days here have no dedicated video URL field
    at all -- hdurl is just a generic sitewide placeholder image, not
    per-video. The actual video link (YouTube/Vimeo embed, or occasionally a
    direct NASA-hosted file) has to be scraped out of the explanation
    field's HTML, which is normally prose plus a credit line of <a href>
    links. Prefer a link that looks like a video host or a direct video
    file; fall back to the first link on the page if nothing matches."""
    links = _HREF_RE.findall(explanation_html or '')
    for link in links:
        if any(host in link for host in VIDEO_HOSTS) or is_direct_video_url(link):
            return link
    return links[0] if links else None


def display_image(url, out_path):
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        im = Image.open(r.raw)
        if im.mode != 'RGB':
            im = im.convert('RGB')
        im.save(out_path)
        im.close()
        return True
    except Exception as e:
        log.error(f"Could not download/decode image from {url}: {e}")
        return False


def display_first_working_image(urls, out_path):
    """Try each candidate URL in order, skipping blanks/duplicates. NASA's
    own metadata occasionally has a broken hdurl (wrong filename, 404)
    while the plain url is fine -- so don't give up after just one try."""
    tried = set()
    for url in urls:
        if not url or url in tried:
            continue
        tried.add(url)
        if display_image(url, out_path):
            return True
    return False


def show_on_screen(path):
    subprocess.run(['pkill', 'fbi'], check=False)
    subprocess.run(['/usr/bin/fbi', '--autozoom', '--noverbose', '--vt', '1', path], check=False)


DIRECT_VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov', '.m4v')


def is_direct_video_url(url):
    """Some APOD video days link straight to an mp4/webm hosted on NASA's
    own servers (apod.nasa.gov, svs.gsfc.nasa.gov) rather than a YouTube/
    Vimeo embed. Those can just be downloaded with requests -- no yt-dlp,
    nothing that can be broken by a site changing its player."""
    path = url.split('?')[0].split('#')[0]
    return path.lower().endswith(DIRECT_VIDEO_EXTENSIONS)


def download_direct_video(url, out_path, chunk_timeout=120, max_seconds=360):
    try:
        r = requests.get(url, stream=True, timeout=chunk_timeout)
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        if total:
            log.info(f"Downloading video: {total / 1_000_000:.1f} MB")
        start = time.monotonic()
        written = 0
        with open(out_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                written += len(chunk)
                if time.monotonic() - start > max_seconds:
                    log.error(f"Video download exceeded {max_seconds}s budget "
                              f"({written / 1_000_000:.1f} MB downloaded); aborting")
                    return False
        return True
    except Exception as e:
        log.error(f"Direct video download failed for {url}: {e}")
        return False


def stop_video():
    """Terminate any video we're tracking, plus sweep for orphaned mplayer
    processes left over from a previous run/crash of this script."""
    global video_process
    if video_process and video_process.poll() is None:
        video_process.terminate()
        try:
            video_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            video_process.kill()
    video_process = None
    subprocess.run(['pkill', '-f', 'mplayer'], check=False)


def start_video_loop(video_path):
    """Launch mplayer looping forever in the background (non-blocking) so it
    doesn't tie up the scheduler thread or the touch-listener loop. Does a
    brief post-launch check to catch immediate failures (bad binary, bad
    file, fbdev busy) without waiting around for a real one."""
    global video_process
    stop_video()
    video_process = subprocess.Popen(
        ['mplayer', '-loop', '0', '-x', '480', '-y', '320', '-nosound',
         '-vo', f'fbdev:{FB_DEVICE}', video_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    if video_process.poll() is not None:
        log.error(f"mplayer exited immediately (code {video_process.returncode}); "
                  f"check it's installed and {FB_DEVICE} is correct")
        video_process = None
        return False
    log.info(f"Video looping in background (pid {video_process.pid})")
    return True


def try_play_video(video_url):
    """Download + transcode, then start it looping. Returns True only if
    playback actually started -- never raises, so a failure here always
    falls through to the thumbnail instead of blowing up the whole fetch."""
    try:
        for old in glob.glob(VIDEO_RAW + '*'):
            os.remove(old)
        if os.path.exists(VIDEO_SCALED):
            os.remove(VIDEO_SCALED)

        if is_direct_video_url(video_url):
            ext = os.path.splitext(video_url.split('?')[0])[1]
            raw_file = VIDEO_RAW + ext
            if not download_direct_video(video_url, raw_file):
                return False
        else:
            dl = subprocess.run(
                ['yt-dlp', '-f', 'bv*[height<=480]/bv*/b', video_url, '-o', VIDEO_RAW],
                capture_output=True, text=True, timeout=180)
            if dl.returncode != 0:
                log.error(f"yt-dlp failed: {dl.stderr.strip()[-500:]}")
                return False

            downloaded = glob.glob(VIDEO_RAW + '*')
            if not downloaded:
                log.error("yt-dlp exited 0 but no file was written")
                return False
            raw_file = downloaded[0]

        scale = subprocess.run([
            'ffmpeg', '-y', '-i', raw_file,
            '-vf', 'scale=480:320:force_original_aspect_ratio=decrease:eval=frame,pad=480:320:-1:-1:color=black',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            VIDEO_SCALED
        ], capture_output=True, text=True, timeout=240)
        if scale.returncode != 0 or not os.path.exists(VIDEO_SCALED):
            log.error(f"ffmpeg scaling failed: {scale.stderr.strip()[-500:]}")
            return False

        return start_video_loop(VIDEO_SCALED)

    except FileNotFoundError as e:
        log.error(f"Required tool not found: {e.filename} -- install it, video will use "
                  f"the thumbnail fallback until then")
        return False
    except Exception as e:
        log.error(f"Unexpected error in video pipeline: {e}")
        return False


def fetch_artifact(date=None):
    global isVideo
    if date is None:
        date = get_date_now()
    log.info(f"Fetching APOD for {date}")

    try:
        resp = fetch_apod(date)
    except Exception as e:
        log.error(f"APOD metadata request failed: {e}")
        return

    media_type = resp.get('media_type', 'image')
    hdurl = resp.get('hdurl')
    log.info(f"media_type={media_type} hdurl={hdurl}")

    if media_type == 'video':
        # 'url' is now the article permalink page, not a playable link --
        # the real video link has to be scraped out of the explanation HTML.
        video_url = extract_video_url(resp.get('explanation'))
        if video_url and try_play_video(video_url):
            isVideo = True
            return
        if video_url:
            log.warning("Video pipeline failed, falling back to placeholder image")
        else:
            log.warning("No video link found in explanation text, falling back to placeholder image")
        isVideo = False
        if hdurl and display_first_working_image([resize_hdurl(hdurl), hdurl], TEMP_IMAGE):
            stop_video()
            show_on_screen(TEMP_IMAGE)
        else:
            log.error("No usable fallback image either; leaving previous frame on screen")
        return

    # media_type == 'image' (also the fallback for any unrecognized type).
    # 'url' is now the article page, not an image, so hdurl is the only
    # usable image field -- try a display-sized resize first, then the
    # untouched original if that somehow fails.
    isVideo = False
    if hdurl and display_first_working_image([resize_hdurl(hdurl), hdurl], TEMP_IMAGE):
        stop_video()
        show_on_screen(TEMP_IMAGE)
    else:
        log.error("Image fetch/display failed (hdurl missing or unreachable); leaving previous frame on screen")


def fetch_with_retries(retries=5, delay=30):
    """Wraps fetch_artifact() with a more patient retry cycle than the
    inner one in fetch_apod(). Used both at boot (systemd can start this
    unit before the network is up) and for the daily cron job (which only
    runs once a day -- worth being patient rather than silently skipping
    a whole day over a transient DNS/network blip)."""
    for attempt in range(1, retries + 1):
        try:
            fetch_artifact()
            return
        except Exception as e:
            log.error(f"Fetch attempt {attempt}/{retries} failed: {e}")
            time.sleep(delay)
    log.error("Exhausted retries; will pick up at next scheduled run")


def display_callback():
    if isVideo and os.path.exists(VIDEO_SCALED):
        log.info("Tap detected: restarting video loop from the beginning")
        start_video_loop(VIDEO_SCALED)


def screen_pressed(callback):
    callback()


def main():
    parser = argparse.ArgumentParser(description="NASA APOD display service")
    parser.add_argument(
        '--date', type=valid_apod_date, metavar='YYYY-MM-DD',
        help="Fetch and display one specific APOD date, then exit "
             "(instead of running the scheduler/touchscreen daemon)."
    )
    args = parser.parse_args()

    check_dependencies()

    if args.date:
        fetch_artifact(args.date)
        return 0

    log.info("Starting NASA APOD display service")

    scheduler.add_job(fetch_with_retries, 'cron', day_of_week='mon-sun', hour=1, minute=0,
                       timezone=pytz.timezone('US/Eastern'))
    scheduler.start()

    # Show something immediately on boot instead of waiting for the next 6am job.
    fetch_with_retries()

    try:
        device = evdev.InputDevice(TOUCH_DEVICE)
    except Exception as e:
        # Don't let a missing/renumbered input device take the whole service down.
        # The scheduler thread above keeps running and the display keeps refreshing daily;
        # we just lose tap-to-replay until this is fixed.
        log.error(f"Touch input device {TOUCH_DEVICE} unavailable ({e}); "
                  f"tap-to-replay disabled, daily refresh continues")
        while True:
            time.sleep(3600)

    size = 2
    sample_buffer = ['' for _ in range(size)]
    pattern_buffer = ['up', 'down']

    log.info(f"Listening for taps on {TOUCH_DEVICE} (blocks here permanently; Ctrl+C to stop)")
    for event in device.read_loop():
        if event.type == evdev.ecodes.EV_KEY:
            event_string = str(evdev.categorize(event))
            event_list = event_string.split(",")

            sample_buffer.insert(0, event_list[2].strip())
            sample_buffer.pop(size)

            param_a = lambda x, y: x and y
            param_b = map(lambda a, b: a == b, sample_buffer, pattern_buffer)
            if functools.reduce(param_a, param_b, True):
                screen_pressed(display_callback)


def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        ret = main()
    except (KeyboardInterrupt, EOFError):
        stop_video()
        ret = 0
    sys.exit(ret)