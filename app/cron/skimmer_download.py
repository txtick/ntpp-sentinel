#!/usr/bin/env python3
"""Download a nightly Skimmer SQLite dump from Google Drive.

This script is meant to run as a cron job inside the Sentinel container.
It downloads the latest Skimmer DB dump into a local path and optionally
archives daily copies.

Environment variables:
  SKIMMER_GDRIVE_FILE_ID   : direct Google Drive file ID (preferred)
  SKIMMER_GDRIVE_FILE_URL  : direct Google Drive file URL
  SKIMMER_LINK_FILE        : local file path containing a URL or Google Drive file ID
  SKIMMER_DOWNLOAD_DIR     : local directory for downloaded DB files
  SKIMMER_DB_PATH          : local path for the current DB copy
  SKIMMER_ARCHIVE_DIR      : nightly archive directory
  SKIMMER_KEEP_DAILY       : if set to 1, keep per-day archived copies

Note: a Google Drive folder link is not sufficient unless the folder is
publicly shared and can be parsed without authentication. If the folder is
private, the recommended path is to publish a fixed file ID or fixed URL.
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "Mozilla/5.0 (compatible; sentinel-skimmer-sync/1.0)"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def get_env_path(name: str, default: str) -> str:
    return os.getenv(name, default)


def extract_drive_file_id(value: str) -> str | None:
    if not value:
        return None
    # direct file ID
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    # typical Google Drive file URL
    match = re.search(r"/d/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    return None


def get_link_file_path(download_dir: str) -> str:
    return get_env_path("SKIMMER_LINK_FILE", os.path.join(download_dir, "skimmer_link.txt"))


def resolve_source_spec(download_dir: str) -> str | None:
    env_id = os.getenv("SKIMMER_GDRIVE_FILE_ID", "").strip()
    if env_id:
        return env_id
    env_url = os.getenv("SKIMMER_GDRIVE_FILE_URL", "").strip()
    if env_url:
        return env_url
    link_file = get_link_file_path(download_dir)
    if os.path.isfile(link_file):
        with open(link_file, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or None
    return None


def normalize_source_spec(value: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        raise ValueError("Empty skimmer source")
    file_id = extract_drive_file_id(value)
    if file_id:
        return "drive_id", file_id
    if value.startswith("http://") or value.startswith("https://"):
        return "url", value
    raise ValueError("skimmer source must be a Google Drive file ID or URL")


def build_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    )
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ]
    return opener


def read_response(opener: urllib.request.OpenerDirector, request: urllib.request.Request) -> bytes:
    with opener.open(request, timeout=120) as response:
        return response.read()


def download_bytes(url: str, opener: urllib.request.OpenerDirector) -> tuple[bytes, str | None]:
    req = urllib.request.Request(url)
    with opener.open(req, timeout=120) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type")
    return body, content_type


def write_bytes_atomic(path: str, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as tmp:
        tmp.write(data)
        temp_path = tmp.name
    os.replace(temp_path, path)


def parse_drive_confirm_token(html: str) -> str | None:
    # Google Drive sometimes returns an interstitial confirmation page.
    match = re.search(r"confirm=([0-9A-Za-z_]+)", html)
    if match:
        return match.group(1)
    return None


def download_google_drive_id(file_id: str, dest_path: str) -> None:
    opener = build_opener()
    base_url = f"https://docs.google.com/uc?export=download&id={file_id}"

    body, content_type = download_bytes(base_url, opener)
    if content_type and "text/html" in content_type.lower():
        html = body.decode("utf-8", errors="ignore")
        token = parse_drive_confirm_token(html)
        if token:
            log("Detected Drive download confirmation token. Following confirmation flow.")
            confirm_url = f"https://docs.google.com/uc?export=download&confirm={token}&id={file_id}"
            body, content_type = download_bytes(confirm_url, opener)

    if content_type and "text/html" in content_type.lower():
        raise RuntimeError(
            "Google Drive download returned HTML instead of a binary file. "
            "Check that the file is shared publicly or the file ID is correct."
        )

    log(f"Downloaded {len(body)} bytes from Google Drive file ID {file_id}")
    write_bytes_atomic(dest_path, body)


def download_to_path(url: str, dest_path: str) -> None:
    opener = build_opener()
    req = urllib.request.Request(url)
    with opener.open(req, timeout=120) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type")
    if content_type and "text/html" in content_type.lower():
        raise RuntimeError(
            f"Download URL returned HTML content; expected a binary SQLite file. URL={url}"
        )
    log(f"Downloaded {len(body)} bytes from {url}")
    write_bytes_atomic(dest_path, body)


def main() -> int:
    download_dir = get_env_path("SKIMMER_DOWNLOAD_DIR", "/data/skimmer")
    current_path = get_env_path("SKIMMER_DB_PATH", os.path.join(download_dir, "skimmer.db"))
    archive_dir = get_env_path("SKIMMER_ARCHIVE_DIR", os.path.join(download_dir, "archive"))
    keep_daily = env_bool("SKIMMER_KEEP_DAILY", False)

    source_spec = resolve_source_spec(download_dir)
    if not source_spec:
        log(
            "No SKIMMER_GDRIVE_FILE_ID, SKIMMER_GDRIVE_FILE_URL, or SKIMMER_LINK_FILE configured/available. Exiting."
        )
        return 0

    ensure_dir(download_dir)
    if keep_daily:
        ensure_dir(archive_dir)

    kind, value = normalize_source_spec(source_spec)
    staging_path = os.path.join(download_dir, "skimmer.db.part")
    if kind == "drive_id":
        download_google_drive_id(value, staging_path)
    else:
        download_to_path(value, staging_path)

    # Verify downloaded file looks like SQLite.
    if not os.path.isfile(staging_path) or os.path.getsize(staging_path) < 1024:
        raise RuntimeError("Downloaded file is too small to be a valid SQLite DB.")

    os.replace(staging_path, current_path)
    log(f"Wrote current Skimmer DB to {current_path}")

    if keep_daily:
        archive_path = os.path.join(archive_dir, time.strftime("skimmer-%Y%m%d.db"))
        shutil.copy2(current_path, archive_path)
        log(f"Archived daily copy to {archive_path}")

    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(exit_code)
