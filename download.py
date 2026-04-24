#!/usr/bin/env python3
"""Download videos and images from X/Twitter, Instagram, and Telegram in best quality."""

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
COOKIES_FILES = {
    "twitter": SCRIPT_DIR / "x_cookies.txt",
    "instagram": SCRIPT_DIR / "www.instagram.com_cookies.txt",
    "telegram": SCRIPT_DIR / "web.telegram.org_cookies.txt",
    "youtube": SCRIPT_DIR / "youtube_cookies.txt",
}
GALLERY_DL = shutil.which("gallery-dl") or str(SCRIPT_DIR / "venv" / "bin" / "gallery-dl")
GALLERY_DL_TIMEOUT_SECONDS = int(os.environ.get("GALLERY_DL_TIMEOUT_SECONDS", "180"))


class DownloadError(Exception):
    """Raised when a download fails (non-fatal in batch mode)."""


# --- URL Parsing ---

def detect_platform(url: str) -> str:
    """Detect platform from URL. Returns 'twitter', 'instagram', or 'telegram'.

    Raises ValueError if URL doesn't match any supported platform.
    """
    parsed = urlparse(url.strip())
    domain = parsed.netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if domain in ("x.com", "twitter.com"):
        return "twitter"
    if domain == "instagram.com":
        return "instagram"
    if domain in ("t.me", "web.telegram.org"):
        return "telegram"
    if domain in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"):
        return "youtube"
    raise ValueError(f"Unsupported platform: {domain}")


def parse_tweet_url(url: str) -> tuple[str, str]:
    """Extract (username, tweet_id) from an X/Twitter URL.

    Raises ValueError if URL doesn't match expected pattern.
    """
    pattern = r"https?://(?:mobile\.)?(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)"
    match = re.match(pattern, url.strip().rstrip("/"))
    if not match:
        raise ValueError(f"Not a valid X/Twitter URL: {url}")
    return match.group(1), match.group(2)


def parse_instagram_url(url: str) -> tuple[str | None, str]:
    """Extract (username_or_none, shortcode) from an Instagram URL.

    Supports: /p/CODE, /reel/CODE, /stories/USER/ID, /reels/CODE
    Raises ValueError if URL doesn't match expected pattern.
    """
    url = url.strip().rstrip("/")
    match = re.match(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if match:
        return None, match.group(1)
    match = re.match(r"https?://(?:www\.)?instagram\.com/stories/([^/]+)/(\d+)", url)
    if match:
        return match.group(1), match.group(2)
    raise ValueError(f"Not a valid Instagram URL: {url}")


def parse_telegram_url(url: str) -> tuple[str, str | None]:
    """Extract (channel, message_id_or_none) from a Telegram URL.

    Supports:
      t.me/channel/123, t.me/c/1234567890/123 (single message)
      t.me/channel, t.me/c/1234567890 (full channel)
      web.telegram.org/a/#-100CHANNELID (full channel)
      web.telegram.org/a/#-100CHANNELID/MSGID (single message)
    Returns message_id=None for full channel download.
    Raises ValueError if URL doesn't match expected pattern.
    """
    url = url.strip().rstrip("/")

    match = re.match(r"https?://web\.telegram\.org/[ak]/#(-?\d+)(?:/(\d+))?", url)
    if match:
        raw_id = match.group(1)
        msg_id = match.group(2)
        if raw_id.startswith("-"):
            # Negative = channel/group. Strip -100 prefix if present
            channel_id = raw_id.lstrip("-")
            if channel_id.startswith("100") and len(channel_id) > 10:
                channel_id = channel_id[3:]
            return f"c/{channel_id}", msg_id
        else:
            # Positive = user/bot chat — use as user ID directly
            return raw_id, msg_id

    match = re.match(r"https?://t\.me/c/(\d+)/(\d+)", url)
    if match:
        return f"c/{match.group(1)}", match.group(2)

    match = re.match(r"https?://t\.me/c/(\d+)$", url)
    if match:
        return f"c/{match.group(1)}", None

    match = re.match(r"https?://t\.me/([^/]+)/(\d+)", url)
    if match:
        return match.group(1), match.group(2)

    match = re.match(r"https?://t\.me/([^/]+)$", url)
    if match:
        return match.group(1), None

    raise ValueError(f"Not a valid Telegram URL: {url}")


def parse_youtube_url(url: str) -> tuple[str, str | None]:
    """Extract (video_id, playlist_id_or_none) from a YouTube URL.

    Supports: /watch?v=, /shorts/, youtu.be/, /playlist?list=, /live/
    Returns (video_id, None) for single videos, ("", playlist_id) for playlists.
    Raises ValueError if URL doesn't match expected pattern.
    """
    from urllib.parse import parse_qs

    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    query = parse_qs(parsed.query)

    # Playlist-only URL: youtube.com/playlist?list=PLxxx
    if parsed.path == "/playlist" and "list" in query:
        return "", query["list"][0]

    # youtu.be/VIDEO_ID
    if domain == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
        if not video_id:
            raise ValueError(f"Not a valid YouTube URL: {url}")
        playlist_id = query.get("list", [None])[0]
        return video_id, playlist_id

    # /shorts/VIDEO_ID
    match = re.match(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path)
    if match:
        return match.group(1), None

    # /live/VIDEO_ID
    match = re.match(r"/live/([A-Za-z0-9_-]{11})", parsed.path)
    if match:
        return match.group(1), query.get("list", [None])[0]

    # /watch?v=VIDEO_ID
    if "v" in query:
        video_id = query["v"][0]
        playlist_id = query.get("list", [None])[0]
        return video_id, playlist_id

    raise ValueError(f"Not a valid YouTube URL: {url}")


def build_filenames(username: str, media_id: str, original_files: list[str]) -> dict[str, str]:
    """Map original filenames to @username_mediaID[_N].ext format.

    Returns dict of {original_name: new_name}.
    No index suffix for single files; _1, _2, etc. for multiple.
    """
    result = {}
    use_index = len(original_files) > 1
    for i, orig in enumerate(original_files, start=1):
        ext = Path(orig).suffix
        if use_index:
            new_name = f"@{username}_{media_id}_{i}{ext}"
        else:
            new_name = f"@{username}_{media_id}{ext}"
        result[orig] = new_name
    return result


# --- Cookies ---

def _get_cookies(platform: str) -> Path | None:
    """Get cookies file path for platform, or None if missing."""
    path = COOKIES_FILES.get(platform)
    if path and path.exists():
        return path
    return None


def _parse_cookie_expiry(cookies_path: Path) -> list[tuple[str, str, int]]:
    """Parse cookie file, return list of (domain, name, expiry_timestamp)."""
    entries = []
    with open(cookies_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                try:
                    expiry = int(parts[4])
                except ValueError:
                    expiry = 0
                entries.append((parts[0], parts[5], expiry))
    return entries


def check_cookies():
    """Check health of all cookie files and Telegram session."""
    print("Cookie Health Check", file=sys.stderr)
    print("=" * 40, file=sys.stderr)

    for platform, path in COOKIES_FILES.items():
        if not path.exists():
            print(f"  {platform:12s}: MISSING ({path.name})", file=sys.stderr)
            continue
        entries = _parse_cookie_expiry(path)
        if not entries:
            print(f"  {platform:12s}: EMPTY (no cookies found)", file=sys.stderr)
            continue
        # Check expiry of session cookies
        now = int(time.time())
        expired = [e for e in entries if 0 < e[2] < now]
        valid = [e for e in entries if e[2] == 0 or e[2] >= now]
        if expired and not valid:
            print(f"  {platform:12s}: EXPIRED (all {len(expired)} cookies expired)", file=sys.stderr)
        elif expired:
            min_valid = min((e[2] for e in valid if e[2] > 0), default=0)
            if min_valid:
                days_left = (min_valid - now) // 86400
                print(f"  {platform:12s}: OK ({len(valid)} valid, expires in ~{days_left} days)", file=sys.stderr)
            else:
                print(f"  {platform:12s}: OK ({len(valid)} valid, session cookies)", file=sys.stderr)
        else:
            min_expiry = min((e[2] for e in entries if e[2] > 0), default=0)
            if min_expiry:
                days_left = (min_expiry - now) // 86400
                print(f"  {platform:12s}: OK ({len(entries)} cookies, expires in ~{days_left} days)", file=sys.stderr)
            else:
                print(f"  {platform:12s}: OK ({len(entries)} session cookies)", file=sys.stderr)

    # Check Telegram session
    session_path = Path(SCRIPT_DIR / "telegram.session")
    if session_path.exists():
        print(f"  {'telegram':12s}: OK (session file exists)", file=sys.stderr)
    else:
        print(f"  {'telegram':12s}: NO SESSION (run: ./venv/bin/python setup_telegram.py)", file=sys.stderr)


# --- Twitter ---

def _extract_tweet_info(url: str) -> dict:
    """Extract tweet metadata using yt-dlp."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
    }
    cookies = _get_cookies("twitter")
    if cookies:
        ydl_opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info or {}
    except Exception as e:
        raise DownloadError(f"Failed to extract tweet info: {e}") from e


def _download_twitter_video(url: str, tmpdir: str, progress_callback=None) -> None:
    """Download video using yt-dlp (best quality with ffmpeg merge)."""
    def _progress_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            pct_str = d.get('_percent_str', '').strip().rstrip('%')
            try:
                progress_callback("download", int(float(pct_str)))
            except (ValueError, TypeError):
                pass

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s_%(autonumber)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_progress_hook],
    }
    cookies = _get_cookies("twitter")
    if cookies:
        ydl_opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        raise DownloadError(f"yt-dlp download failed: {e}") from e


def _download_twitter_images(url: str, tmpdir: str) -> None:
    """Download images using gallery-dl."""
    cmd = [
        GALLERY_DL,
        "-d", tmpdir,
        "--filename", "{tweet_id}_{num}.{extension}",
        "--no-mtime",
        "-o", "quoted=true",
    ]
    cookies = _get_cookies("twitter")
    if cookies:
        cmd.extend(["--cookies", str(cookies)])
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DownloadError(f"gallery-dl error: {result.stderr.strip()}")


def _download_twitter(url: str, tmpdir: str, progress_callback=None) -> tuple[str, str]:
    """Download Twitter media. Returns (username, tweet_id)."""
    url_username, tweet_id = parse_tweet_url(url)
    info = _extract_tweet_info(url)
    username = info.get("uploader_id") or url_username
    if info.get("formats"):
        _download_twitter_video(url, tmpdir, progress_callback=progress_callback)
    else:
        _download_twitter_images(url, tmpdir)
    return username, tweet_id


# --- Instagram ---

def _download_instagram(url: str, tmpdir: str, progress_callback=None) -> tuple[str, str]:
    """Download Instagram media via gallery-dl. Returns (username, post_id)."""
    url_username, shortcode = parse_instagram_url(url)
    cmd = [
        GALLERY_DL,
        "-d", tmpdir,
        "--filename", "{filename}.{extension}",
        "--no-mtime",
    ]
    cookies = _get_cookies("instagram")
    if cookies:
        cmd.extend(["--cookies", str(cookies)])
    else:
        print("Warning: instagram cookies not found. Some content may not be accessible.", file=sys.stderr)
    cmd.append(url)
    if progress_callback:
        progress_callback("info", "Contacting Instagram...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GALLERY_DL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise DownloadError(
            f"Instagram download timed out after {GALLERY_DL_TIMEOUT_SECONDS}s. Try again or refresh cookies."
        ) from e
    if result.returncode != 0:
        raise DownloadError(f"gallery-dl error: {result.stderr.strip()}")

    username = url_username or "unknown"
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            parts = Path(line.strip()).parts
            for j, part in enumerate(parts):
                if part == "instagram" and j + 1 < len(parts):
                    username = parts[j + 1]
                    break

    return username, shortcode


# --- YouTube ---


def _youtube_ydl_opts() -> dict:
    """Common yt-dlp options for YouTube (cookies + JS runtime + EJS solver)."""
    opts = {
        "js_runtimes": {"deno": {"path": None}, "node": {"path": None}},
        "remote_components": ["ejs:github"],
    }
    cookies = _get_cookies("youtube")
    if cookies:
        opts["cookiefile"] = str(cookies)
    return opts


def extract_youtube_info(url: str) -> dict:
    """Extract YouTube video/playlist metadata without downloading.

    Returns dict with: title, channel, duration, view_count, playlist_count, etc.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        **_youtube_ydl_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise DownloadError("Could not fetch video info. Video may be private, deleted, or region-locked.")
            result = {
                "title": info.get("title", "Unknown"),
                "channel": info.get("uploader") or info.get("channel") or "Unknown",
                "duration": info.get("duration", 0),
                "view_count": info.get("view_count"),
            }
            # Playlist info
            if info.get("_type") == "playlist":
                result["playlist_count"] = info.get("playlist_count") or "unknown"
                result["title"] = info.get("title", "Playlist")
            return result
    except DownloadError:
        raise
    except Exception as e:
        msg = str(e)
        if "Sign in" in msg or "cookies" in msg.lower():
            raise DownloadError("YouTube requires authentication. Place youtube_cookies.txt on the server.") from e
        raise DownloadError(f"Failed to extract YouTube info: {e}") from e


def _format_duration(seconds: int) -> str:
    """Format seconds to human-readable duration."""
    if not seconds:
        return "unknown"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _download_youtube(url: str, tmpdir: str, mp3: bool = False, progress_callback=None) -> tuple[str, str]:
    """Download a single YouTube video. Returns (channel, video_id)."""
    video_id, _ = parse_youtube_url(url)
    if not video_id:
        raise DownloadError("Cannot download: URL is a playlist, not a single video.")

    def _progress_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            pct_str = d.get('_percent_str', '').strip().rstrip('%')
            try:
                progress_callback("download", int(float(pct_str)))
            except (ValueError, TypeError):
                pass

    yt_common = _youtube_ydl_opts()

    if mp3:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress_hook],
            **yt_common,
        }
    else:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress_hook],
            **yt_common,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            channel = info.get("uploader") or info.get("channel") or "unknown"
            channel = re.sub(r'[^\w\-.]', '_', channel)
            return channel, video_id
    except Exception as e:
        msg = str(e)
        if "Sign in" in msg or "cookies" in msg.lower():
            raise DownloadError("YouTube requires authentication. Place youtube_cookies.txt on the server.") from e
        raise DownloadError(f"YouTube download failed: {e}") from e


def _download_youtube_playlist(url: str, tmpdir: str, mp3: bool = False, progress_callback=None) -> tuple[list[tuple[int, str, str]], int]:
    """Download all videos from a YouTube playlist.

    Returns (results, skipped_count) where results is list of (original_index, channel, video_id).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        **_youtube_ydl_opts(),
    }

    # First pass: get list of video URLs
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise DownloadError(f"Failed to read playlist: {e}") from e

    if not info or not info.get("entries"):
        raise DownloadError("Playlist is empty or unavailable.")

    entries = list(info["entries"])
    total = len(entries)
    results = []
    skipped = 0

    for i, entry in enumerate(entries):
        video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}"

        def _playlist_progress(phase, pct, _i=i):
            if progress_callback:
                overall = int((_i * 100 + pct) / total)
                progress_callback(phase, overall)

        try:
            video_tmpdir = os.path.join(tmpdir, f"video_{i}")
            os.makedirs(video_tmpdir, exist_ok=True)
            channel, vid_id = _download_youtube(video_url, video_tmpdir, mp3=mp3, progress_callback=_playlist_progress)
            results.append((i, channel, vid_id))
            if progress_callback:
                progress_callback("download", int((i + 1) / total * 100))
        except DownloadError as e:
            skipped += 1
            print(f"  Warning: Skipping video {i + 1}/{total}: {e}", file=sys.stderr)

    if not results:
        raise DownloadError("No videos could be downloaded from playlist.")

    return results, skipped


# --- Telegram ---

TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "34456187"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "f451a6ed6c0b0f596bdbb7a5f3938440")
TELEGRAM_SESSION = str(SCRIPT_DIR / "telegram")


def _resolve_telegram_entity_id(channel: str):
    """Convert channel string to Telegram entity ID or username."""
    if channel.startswith("c/"):
        return int(f"-100{channel.split('/')[1]}")
    if channel.isdigit():
        return int(channel)
    return channel


def _get_telegram_channel_name(entity, fallback: str) -> str:
    """Get a clean channel name for file naming."""
    name = getattr(entity, 'username', None) or getattr(entity, 'title', fallback)
    return name.replace("/", "_").replace(" ", "_")


def _download_telegram(url: str, tmpdir: str) -> tuple[str, str]:
    """Download single Telegram message media. Returns (channel_name, message_id)."""
    from telethon import TelegramClient as AsyncTelegramClient

    channel, message_id = parse_telegram_url(url)

    session_path = Path(TELEGRAM_SESSION + ".session")
    if not session_path.exists():
        raise DownloadError("Telegram session not found. Run: ./venv/bin/python setup_telegram.py")

    async def _run():
        client = AsyncTelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.start()
        try:
            entity_id = _resolve_telegram_entity_id(channel)
            try:
                entity = await client.get_entity(entity_id)
            except ValueError:
                # Entity not in cache — fetch dialogs to populate it
                await client.get_dialogs()
                entity = await client.get_entity(entity_id)
            entity_name = getattr(entity, 'username', None) or getattr(entity, 'title', None) or getattr(entity, 'first_name', channel)
            entity_name = entity_name.replace("/", "_").replace(" ", "_")

            msg = await client.get_messages(entity, ids=int(message_id))
            if not msg or not msg.media:
                raise DownloadError("No media found in this Telegram message.")

            path = await client.download_media(msg, file=tmpdir)
            if path:
                print(f"Downloaded: {os.path.basename(path)}", file=sys.stderr)
            return entity_name, message_id
        finally:
            await client.disconnect()

    try:
        return asyncio.run(_run())
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"Telegram error: {e}") from e


def _download_telegram_channel(url: str, output_dir: Path, progress_callback=None) -> list[str]:
    """Download all media from a Telegram channel with async parallel. Returns saved file paths.

    progress_callback: optional callable(current, total, file_pct) called during downloads.
      current: files completed so far
      total: total files
      file_pct: current file download progress 0-100 (or -1 if between files)
    """
    from telethon import TelegramClient as AsyncTelegramClient

    channel, _ = parse_telegram_url(url)

    session_path = Path(TELEGRAM_SESSION + ".session")
    if not session_path.exists():
        raise DownloadError("Telegram session not found. Run: ./venv/bin/python setup_telegram.py")

    async def _run():
        client = AsyncTelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.start()
        try:
            return await _run_download(client)
        finally:
            await client.disconnect()

    async def _run_download(client):
        entity_id = _resolve_telegram_entity_id(channel)
        try:
            entity = await client.get_entity(entity_id)
        except ValueError:
            await client.get_dialogs()
            entity = await client.get_entity(entity_id)
        channel_name = getattr(entity, 'username', None) or getattr(entity, 'title', None) or getattr(entity, 'first_name', channel)
        channel_name = channel_name.replace("/", "_").replace(" ", "_")

        channel_dir = output_dir / channel_name
        channel_dir.mkdir(parents=True, exist_ok=True)

        print(f"Scanning channel: {channel_name}...", file=sys.stderr)
        media_messages = []
        async for msg in client.iter_messages(entity):
            if msg.photo or msg.video or msg.document:
                media_messages.append(msg)
        total = len(media_messages)
        print(f"Found {total} media messages.", file=sys.stderr)

        if total == 0:
            await client.disconnect()
            return []

        saved_paths = []
        counter = 0
        last_reported_pct = -1
        sem = asyncio.Semaphore(10)

        # Track per-file progress for overall calculation
        file_progress = {}  # msg.id -> fraction 0.0..1.0

        def _report_overall():
            nonlocal last_reported_pct
            if not progress_callback:
                return
            # Overall = (completed files + sum of partial fractions) / total
            partial_sum = sum(file_progress.values())
            overall_pct = min(int((counter + partial_sum) / total * 100), 99)
            if overall_pct > last_reported_pct:
                progress_callback(counter, total, overall_pct)
                last_reported_pct = overall_pct

        async def download_one(msg):
            nonlocal counter
            async with sem:
                try:
                    existing = list(channel_dir.glob(f"{msg.id}.*"))
                    if existing:
                        counter += 1
                        _report_overall()
                        return str(existing[0])

                    def _file_progress(received, file_total):
                        if file_total:
                            file_progress[msg.id] = received / file_total
                            _report_overall()

                    path = await client.download_media(msg, file=str(channel_dir), progress_callback=_file_progress)
                    if path:
                        actual_ext = Path(path).suffix
                        final_name = f"{msg.id}{actual_ext}"
                        final_path = channel_dir / final_name
                        if Path(path) != final_path:
                            Path(path).rename(final_path)
                        file_progress.pop(msg.id, None)
                        counter += 1
                        _report_overall()
                        print(f"\r  [{counter}/{total}] {final_name}", file=sys.stderr, end="", flush=True)
                        return str(final_path)
                    counter += 1
                    return None
                except Exception as e:
                    counter += 1
                    print(f"\n  Warning: Failed msg {msg.id}: {e}", file=sys.stderr)
                    return None

        tasks = [download_one(msg) for msg in media_messages]
        results = await asyncio.gather(*tasks)
        saved_paths = [r for r in results if r]

        print(f"\n  Done: {len(saved_paths)}/{total} files downloaded.", file=sys.stderr)
        return saved_paths

    return asyncio.run(_run())



# --- Common ---

def _get_video_duration(filepath: str) -> float:
    """Get video duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", filepath],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def _ensure_h264(filepath: str, progress_callback=None) -> str:
    """Re-encode video to H.264 if it uses VP9 or other incompatible codecs. Returns final path.

    progress_callback: optional callable(percent: int) called during encoding.
    """
    if not filepath.lower().endswith((".mp4", ".webm", ".mkv")):
        return filepath
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise DownloadError("ffprobe not found. Install ffmpeg: brew install ffmpeg")
    if probe.returncode != 0:
        print(f"Warning: ffprobe failed on {os.path.basename(filepath)}, skipping re-encode.", file=sys.stderr)
        return filepath
    codec = probe.stdout.strip()
    if codec and codec != "h264":
        out_path = filepath.rsplit(".", 1)[0] + "_h264.mp4"
        final_path = filepath.rsplit(".", 1)[0] + ".mp4"
        duration = _get_video_duration(filepath)
        print(f"Re-encoding {codec} -> H.264...", file=sys.stderr)
        try:
            # Use -progress pipe for progress tracking
            proc = subprocess.Popen(
                ["ffmpeg", "-i", filepath, "-c:v", "libx264", "-preset", "fast",
                 "-crf", "18", "-c:a", "aac", "-y", "-loglevel", "warning",
                 "-progress", "pipe:1", out_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            last_pct = -1
            while proc.poll() is None:
                line = proc.stdout.readline()
                if line.startswith("out_time_ms=") and duration > 0:
                    try:
                        time_ms = int(line.split("=")[1].strip())
                        pct = min(int(time_ms / (duration * 1_000_000) * 100), 99)
                        if pct != last_pct:
                            if progress_callback:
                                progress_callback("convert", pct)
                            last_pct = pct
                    except (ValueError, ZeroDivisionError):
                        pass
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, "ffmpeg")
        except FileNotFoundError:
            raise DownloadError("ffmpeg not found. Install ffmpeg: brew install ffmpeg")
        except subprocess.CalledProcessError as e:
            raise DownloadError(f"ffmpeg re-encode failed for {os.path.basename(filepath)}: {e}")
        # Delete original only after successful encode
        os.remove(filepath)
        shutil.move(out_path, final_path)
        return final_path
    return filepath


def _collect_files(tmpdir: str) -> list[str]:
    """Recursively collect all downloaded files from tmpdir."""
    files = []
    for root, _, filenames in os.walk(tmpdir):
        for f in filenames:
            files.append(os.path.join(root, f))
    files.sort()
    return files


def _format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _print_summary(saved_paths: list[str]):
    """Print download summary with file sizes to stderr."""
    if not saved_paths:
        return
    total_size = 0
    print(f"\nDownloaded {len(saved_paths)} file(s):", file=sys.stderr)
    for p in saved_paths:
        size = os.path.getsize(p)
        total_size += size
        print(f"  {os.path.basename(p):40s} {_format_size(size):>10s}", file=sys.stderr)
    if len(saved_paths) > 1:
        print(f"  {'Total:':40s} {_format_size(total_size):>10s}", file=sys.stderr)


def _check_duplicate(platform: str, username: str, media_id: str, output_dir: Path) -> list[str] | None:
    """Check if media already exists. Returns existing paths or None."""
    existing = list(output_dir.glob(f"@{username}_{media_id}.*"))
    existing += list(output_dir.glob(f"@{username}_{media_id}_*.*"))
    if existing:
        return [str(p) for p in existing]
    return None


def _check_disk_space(min_mb: int = 500) -> None:
    """Check if there's enough free disk space. Raises DownloadError if not."""
    check_path = DOWNLOADS_DIR if DOWNLOADS_DIR.exists() else SCRIPT_DIR
    usage = shutil.disk_usage(check_path)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_mb:
        raise DownloadError(f"Not enough disk space: {free_mb:.0f} MB free, need at least {min_mb} MB.")


def download_media(url: str, force: bool = False, mp3: bool = False, progress_callback=None) -> list[str]:
    """Download all media from a URL. Returns list of saved file paths."""
    platform = detect_platform(url)
    output_dir = DOWNLOADS_DIR / platform
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        _check_disk_space()

        # Telegram full channel download has its own flow
        if platform == "telegram":
            _, message_id = parse_telegram_url(url)
            if message_id is None:
                return _download_telegram_channel(url, DOWNLOADS_DIR / "telegram")

        # YouTube playlist has its own flow
        if platform == "youtube":
            video_id, playlist_id = parse_youtube_url(url)
            if playlist_id and not video_id:
                return _download_youtube_playlist_media(url, output_dir, mp3=mp3, progress_callback=progress_callback)

        # Early duplicate check (before downloading)
        if not force:
            try:
                if platform == "twitter":
                    url_username, tweet_id = parse_tweet_url(url)
                    existing = _check_duplicate(platform, url_username, tweet_id, output_dir)
                    if not existing:
                        pass  # proceed to download
                    else:
                        print(f"Skipped (already exists): {', '.join(os.path.basename(p) for p in existing)}", file=sys.stderr)
                        print("Use --force to re-download.", file=sys.stderr)
                        return existing
                elif platform == "instagram":
                    _, shortcode = parse_instagram_url(url)
                    existing = _check_duplicate(platform, "*", shortcode, output_dir)
                    if existing:
                        print(f"Skipped (already exists): {', '.join(os.path.basename(p) for p in existing)}", file=sys.stderr)
                        print("Use --force to re-download.", file=sys.stderr)
                        return existing
                elif platform == "telegram":
                    channel, msg_id = parse_telegram_url(url)
                    existing = _check_duplicate(platform, "*", msg_id, output_dir)
                    if existing:
                        print(f"Skipped (already exists): {', '.join(os.path.basename(p) for p in existing)}", file=sys.stderr)
                        print("Use --force to re-download.", file=sys.stderr)
                        return existing
                elif platform == "youtube":
                    video_id, _ = parse_youtube_url(url)
                    existing = _check_duplicate(platform, "*", video_id, output_dir)
                    if existing:
                        print(f"Skipped (already exists): {', '.join(os.path.basename(p) for p in existing)}", file=sys.stderr)
                        print("Use --force to re-download.", file=sys.stderr)
                        return existing
            except ValueError:
                pass  # URL parsing failed, let download handle the error

        with tempfile.TemporaryDirectory() as tmpdir:
            if platform == "twitter":
                username, media_id = _download_twitter(url, tmpdir, progress_callback=progress_callback)
            elif platform == "instagram":
                username, media_id = _download_instagram(url, tmpdir, progress_callback=progress_callback)
            elif platform == "telegram":
                username, media_id = _download_telegram(url, tmpdir)
            elif platform == "youtube":
                username, media_id = _download_youtube(url, tmpdir, mp3=mp3, progress_callback=progress_callback)
            else:
                raise DownloadError(f"No download handler for platform: {platform}")

            # Collect all downloaded files and ensure video compatibility (skip for mp3)
            downloaded_paths = _collect_files(tmpdir)
            if not mp3:
                downloaded_paths = [_ensure_h264(p, progress_callback=progress_callback) for p in downloaded_paths]
            if not downloaded_paths:
                raise DownloadError("No media files were downloaded.")

            # Extract just filenames for renaming
            downloaded_names = [os.path.basename(p) for p in downloaded_paths]
            name_map = build_filenames(username, media_id, downloaded_names)

            saved_paths = []
            for full_path, orig_name in zip(downloaded_paths, downloaded_names):
                new_name = name_map[orig_name]
                dst = output_dir / new_name
                shutil.move(full_path, dst)
                saved_paths.append(str(dst))

        return saved_paths
    except ValueError as e:
        raise DownloadError(str(e)) from e


def _download_youtube_playlist_media(url: str, output_dir: Path, mp3: bool = False, progress_callback=None) -> list[str]:
    """Download YouTube playlist and save all files. Returns saved paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results, skipped = _download_youtube_playlist(url, tmpdir, mp3=mp3, progress_callback=progress_callback)

        all_saved = []
        for orig_idx, channel, video_id in results:
            video_tmpdir = os.path.join(tmpdir, f"video_{orig_idx}")
            downloaded_paths = _collect_files(video_tmpdir)
            if not mp3:
                downloaded_paths = [_ensure_h264(p) for p in downloaded_paths]

            downloaded_names = [os.path.basename(p) for p in downloaded_paths]
            name_map = build_filenames(channel, video_id, downloaded_names)

            for full_path, orig_name in zip(downloaded_paths, downloaded_names):
                new_name = name_map[orig_name]
                dst = output_dir / new_name
                shutil.move(full_path, dst)
                all_saved.append(str(dst))

        if not all_saved:
            raise DownloadError("No files downloaded from playlist.")

        if skipped and progress_callback:
            progress_callback("info", f"Downloaded {len(results)} videos, {skipped} skipped")

        return all_saved


def _get_clipboard_url() -> str:
    """Read URL from system clipboard (macOS)."""
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"pbpaste failed (exit {result.returncode})")
    url = result.stdout.strip()
    if not url:
        raise ValueError("Clipboard is empty.")
    return url


def main():
    parser = argparse.ArgumentParser(
        description="Download media from X/Twitter, Instagram, and Telegram.",
    )
    parser.add_argument("urls", nargs="*", help="URLs to download")
    parser.add_argument("-c", "--clipboard", action="store_true", help="Read URL from clipboard")
    parser.add_argument("-f", "--file", type=str, help="Read URLs from file (one per line)")
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    parser.add_argument("--mp3", action="store_true", help="Extract audio only as MP3 (YouTube)")
    parser.add_argument("--check", action="store_true", help="Check cookie health and exit")

    args = parser.parse_args()

    # Cookie health check
    if args.check:
        check_cookies()
        sys.exit(0)

    # Collect URLs from all sources
    urls = list(args.urls)

    if args.clipboard:
        try:
            clip_url = _get_clipboard_url()
            urls.append(clip_url)
            print(f"From clipboard: {clip_url}", file=sys.stderr)
        except (ValueError, FileNotFoundError) as e:
            print(f"Error reading clipboard: {e}", file=sys.stderr)
            sys.exit(1)

    if args.file:
        try:
            with open(args.file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
            print(f"Loaded {len(urls)} URLs from {args.file}", file=sys.stderr)
        except FileNotFoundError:
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)

    if not urls:
        parser.print_help()
        sys.exit(1)

    # Download all URLs
    succeeded = 0
    failed = 0
    all_saved = []

    for i, url in enumerate(urls):
        if len(urls) > 1:
            print(f"\n[{i + 1}/{len(urls)}] {url}", file=sys.stderr)

        try:
            detect_platform(url)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            failed += 1
            continue

        try:
            saved = download_media(url, force=args.force, mp3=args.mp3)
            all_saved.extend(saved)
            succeeded += 1
        except DownloadError as e:
            print(f"Error: {e}", file=sys.stderr)
            failed += 1
            continue

    # Print summary
    _print_summary(all_saved)

    # Print paths to stdout (for piping)
    for path in all_saved:
        print(path)

    # Batch summary
    if len(urls) > 1:
        print(f"\nBatch complete: {succeeded} succeeded, {failed} failed", file=sys.stderr)

    if failed and not succeeded:
        sys.exit(1)


if __name__ == "__main__":
    main()
