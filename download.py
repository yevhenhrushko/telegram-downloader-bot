#!/usr/bin/env python3
"""Download videos and images from X/Twitter in best quality."""

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yt_dlp

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"


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


def download_media(url: str) -> list[str]:
    """Download all media from a tweet URL. Returns list of saved file paths."""
    username, tweet_id = parse_tweet_url(url)

    DOWNLOADS_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s_%(autonumber)s.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": False,
            "no_warnings": False,
        }

        if COOKIES_FILE.exists():
            ydl_opts["cookiefile"] = str(COOKIES_FILE)
        else:
            print(f"Warning: {COOKIES_FILE} not found. Proceeding without auth.", file=sys.stderr)
            print("Some content (NSFW, private) may not be accessible.", file=sys.stderr)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Collect downloaded files
        downloaded = sorted(os.listdir(tmpdir))
        if not downloaded:
            print("Error: No media files were downloaded.", file=sys.stderr)
            sys.exit(1)

        # Rename and move to downloads/
        name_map = build_filenames(username, tweet_id, downloaded)
        saved_paths = []
        for orig, new_name in name_map.items():
            src = os.path.join(tmpdir, orig)
            dst = DOWNLOADS_DIR / new_name
            shutil.move(src, dst)
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
