#!/usr/bin/env python3
"""Download videos and images from X/Twitter in best quality."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
VENV_BIN = SCRIPT_DIR / "venv" / "bin"


def parse_tweet_url(url: str) -> tuple[str, str]:
    """Extract (username, tweet_id) from an X/Twitter URL.

    Raises ValueError if URL doesn't match expected pattern.
    """
    pattern = r"https?://(?:mobile\.)?(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)"
    match = re.match(pattern, url.strip().rstrip("/"))
    if not match:
        raise ValueError(f"Not a valid X/Twitter URL: {url}")
    return match.group(1), match.group(2)


def build_filenames(username: str, tweet_id: str, original_files: list[str]) -> dict[str, str]:
    """Map original filenames to @username_tweetID[_N].ext format.

    Returns dict of {original_name: new_name}.
    No index suffix for single files; _1, _2, etc. for multiple.
    """
    result = {}
    use_index = len(original_files) > 1
    for i, orig in enumerate(original_files, start=1):
        ext = Path(orig).suffix
        if use_index:
            new_name = f"@{username}_{tweet_id}_{i}{ext}"
        else:
            new_name = f"@{username}_{tweet_id}{ext}"
        result[orig] = new_name
    return result


def _extract_tweet_info(url: str) -> dict:
    """Extract tweet metadata using yt-dlp. Returns info dict with uploader_id, id, formats."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
    }
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info or {}


def _download_video(url: str, tmpdir: str) -> None:
    """Download video using yt-dlp (best quality with ffmpeg merge)."""
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s_%(autonumber)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
    }
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def _download_images(url: str, tmpdir: str) -> None:
    """Download images using gallery-dl."""
    cmd = [
        str(VENV_BIN / "gallery-dl"),
        "-d", tmpdir,
        "--filename", "{tweet_id}_{num}.{extension}",
        "--no-mtime",
    ]
    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gallery-dl error: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _collect_files(tmpdir: str) -> list[str]:
    """Recursively collect all downloaded files from tmpdir."""
    files = []
    for root, _, filenames in os.walk(tmpdir):
        for f in filenames:
            files.append(os.path.join(root, f))
    files.sort()
    return files


def download_media(url: str) -> list[str]:
    """Download all media from a tweet URL. Returns list of saved file paths."""
    url_username, url_tweet_id = parse_tweet_url(url)

    DOWNLOADS_DIR.mkdir(exist_ok=True)

    if not COOKIES_FILE.exists():
        print(f"Warning: {COOKIES_FILE} not found. Proceeding without auth.", file=sys.stderr)
        print("Some content (NSFW, private) may not be accessible.", file=sys.stderr)

    # Extract metadata to get real username and detect media type
    info = _extract_tweet_info(url)
    username = info.get("uploader_id") or url_username
    tweet_id = url_tweet_id
    has_video = bool(info.get("formats"))

    with tempfile.TemporaryDirectory() as tmpdir:
        if has_video:
            _download_video(url, tmpdir)
        else:
            _download_images(url, tmpdir)

        # Collect all downloaded files
        downloaded_paths = _collect_files(tmpdir)
        if not downloaded_paths:
            print("Error: No media files were downloaded.", file=sys.stderr)
            sys.exit(1)

        # Extract just filenames for renaming
        downloaded_names = [os.path.basename(p) for p in downloaded_paths]
        name_map = build_filenames(username, tweet_id, downloaded_names)

        saved_paths = []
        for full_path, orig_name in zip(downloaded_paths, downloaded_names):
            new_name = name_map[orig_name]
            dst = DOWNLOADS_DIR / new_name
            shutil.move(full_path, dst)
            saved_paths.append(str(dst))

    return saved_paths


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <tweet_url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]

    try:
        parse_tweet_url(url)  # validate early
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    saved = download_media(url)
    for path in saved:
        print(path)


if __name__ == "__main__":
    main()
