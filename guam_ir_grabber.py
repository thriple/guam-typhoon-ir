#!/usr/bin/env python3
"""
guam_ir_grabber.py

Polls the NOAA OSPO Guam Infrared satellite loop directory, downloads any
.jpg frames it finds, and saves only ones it hasn't seen before -- dedup is
done by SHA-256 hash of the image bytes, NOT by filename, since the site
reuses filenames like ir01.jpg..ir20.jpg as the loop rolls forward.

USAGE
-----
One-off run (grab whatever frames are currently up):
    python3 guam_ir_grabber.py --once

Run forever, checking every 30 minutes, stopping after 5 days:
    python3 guam_ir_grabber.py --loop --interval 1800 --duration-hours 120

Recommended instead of --loop: schedule this to run every 30 min with cron
or Task Scheduler (see bottom of this file for examples), calling it with
--once each time. That's more reliable than a multi-day sleep loop, since
it survives your machine sleeping/rebooting.

Zip up everything collected so far:
    python3 guam_ir_grabber.py --zip-only

All downloaded images and a manifest.json (hash -> metadata) are kept in
the --outdir directory (default: ./guam_ir_frames).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

DIRECTORY_URL = "https://www.ospo.noaa.gov/Products_imagery/guam/guamloops/ir/"
DEFAULT_OUTDIR = "guam_ir_frames"
MANIFEST_NAME = "manifest.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; guam-ir-grabber/1.0; personal use script)"
}


def load_manifest(outdir):
    path = os.path.join(outdir, MANIFEST_NAME)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"seen_hashes": {}, "downloaded_count": 0}


def save_manifest(outdir, manifest):
    path = os.path.join(outdir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def list_jpg_urls(directory_url, start_frame=11, end_frame=20):
    """
    Build the list of candidate image URLs directly.

    The directory listing on this server returns 403 (browsing is disabled),
    but individual numbered files (e.g. .../ir/11.jpg) are directly
    accessible. The site rolls the loop forward by renaming files each half
    hour, so we just probe every plausible numeric filename each run and let
    the hash-based dedup in run_once() sort out what's actually new.

    Confirmed range is 11.jpg..20.jpg (two-digit, no zero-padding ambiguity
    since both n and n:02d produce the same string for n >= 10). Filenames
    below 11 aren't part of the rolling loop -- e.g. 1.jpg turned out to be
    an unrelated legacy file with an old 2015 timestamp -- so we don't probe
    them by default.
    """
    urls = []
    for n in range(start_frame, end_frame + 1):
        url = urljoin(directory_url, f"{n:02d}.jpg")
        urls.append(url)
    return urls


def run_once(outdir, directory_url, start_frame=11, end_frame=20, verbose=True):
    os.makedirs(outdir, exist_ok=True)
    manifest = load_manifest(outdir)
    seen = manifest["seen_hashes"]

    urls = list_jpg_urls(directory_url, start_frame, end_frame)

    if verbose:
        print(f"[{now_str()}] Probing {len(urls)} candidate filename(s).")

    new_count = 0
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                continue  # expected for unpadded/nonexistent guesses
            resp.raise_for_status()
            data = resp.content
        except requests.RequestException as e:
            print(f"[{now_str()}]   skip (fetch failed): {url} -> {e}", file=sys.stderr)
            continue

        digest = hashlib.sha256(data).hexdigest()

        if digest in seen:
            continue  # already have this exact image, regardless of filename

        # New image -> save with a timestamped, collision-proof filename.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        orig_name = os.path.basename(url)
        safe_name = f"{ts}_{orig_name}"
        out_path = os.path.join(outdir, safe_name)

        with open(out_path, "wb") as f:
            f.write(data)

        seen[digest] = {
            "saved_as": safe_name,
            "source_url": url,
            "downloaded_at_utc": ts,
        }
        manifest["downloaded_count"] = manifest.get("downloaded_count", 0) + 1
        new_count += 1

        if verbose:
            print(f"[{now_str()}]   NEW -> {safe_name}  (from {orig_name})")

    save_manifest(outdir, manifest)

    if verbose:
        if new_count == 0:
            print(f"[{now_str()}] No new frames this pass. "
                  f"Total collected so far: {manifest['downloaded_count']}")
        else:
            print(f"[{now_str()}] Saved {new_count} new frame(s). "
                  f"Total collected so far: {manifest['downloaded_count']}")

    return new_count


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_zip(outdir, zip_path=None):
    if zip_path is None:
        zip_path = outdir.rstrip("/\\") + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(outdir)):
            if fname.lower().endswith(".jpg"):
                zf.write(os.path.join(outdir, fname), arcname=fname)
    print(f"Wrote {zip_path}")
    return zip_path


def loop(outdir, directory_url, interval_seconds, duration_hours, start_frame=11, end_frame=20):
    end_time = time.time() + duration_hours * 3600
    print(f"[{now_str()}] Starting loop. Checking every {interval_seconds}s "
          f"until {datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')}.")
    while time.time() < end_time:
        run_once(outdir, directory_url, start_frame, end_frame)
        time.sleep(interval_seconds)
    print(f"[{now_str()}] Loop finished (duration reached).")


def main():
    parser = argparse.ArgumentParser(description="Grab & dedupe NOAA Guam IR satellite frames.")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR, help="Where to save images/manifest.")
    parser.add_argument("--url", default=DIRECTORY_URL, help="Directory URL to poll.")
    parser.add_argument("--start-frame", type=int, default=11, help="Lowest frame number to probe (default 11).")
    parser.add_argument("--end-frame", type=int, default=20, help="Highest frame number to probe (default 20).")
    parser.add_argument("--once", action="store_true", help="Run a single check-and-download pass.")
    parser.add_argument("--loop", action="store_true", help="Run repeatedly (not recommended for multi-day; prefer cron).")
    parser.add_argument("--interval", type=int, default=1800, help="Seconds between checks in --loop mode (default 1800 = 30 min).")
    parser.add_argument("--duration-hours", type=float, default=120, help="Total hours to run in --loop mode (default 120 = 5 days).")
    parser.add_argument("--zip-only", action="store_true", help="Just zip up whatever is currently in --outdir.")
    args = parser.parse_args()

    if args.zip_only:
        make_zip(args.outdir)
        return

    if args.loop:
        loop(args.outdir, args.url, args.interval, args.duration_hours, args.start_frame, args.end_frame)
    else:
        # default behavior (including plain --once) = single pass
        run_once(args.outdir, args.url, args.start_frame, args.end_frame)


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------
# SCHEDULING EXAMPLES
# -----------------------------------------------------------------------
#
# macOS / Linux (cron) -- run every 30 minutes for the next 5 days:
#   1. crontab -e
#   2. Add a line like:
#      */30 * * * * cd /path/to/script/dir && /usr/bin/python3 guam_ir_grabber.py --once >> grabber.log 2>&1
#   3. After 5 days, remove the line (crontab -e again) or just let it keep
#      running -- it's harmless, it'll just keep collecting new frames.
#
# Windows (Task Scheduler):
#   1. Open Task Scheduler -> Create Task
#   2. Trigger: Daily, repeat task every 30 minutes for a duration of 5 days
#   3. Action: Start a program
#        Program: python
#        Arguments: guam_ir_grabber.py --once
#        Start in: C:\path\to\script\dir
#
# When you're ready to build your video, zip everything up:
#   python3 guam_ir_grabber.py --zip-only
# -----------------------------------------------------------------------
