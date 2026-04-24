#!/usr/bin/env python3
"""Telegram bot for downloading media from YouTube, X/Twitter, Instagram, and Telegram."""

import asyncio
import functools
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from download import (
    COOKIES_FILES,
    DownloadError,
    _download_telegram_channel,
    _format_duration,
    detect_platform,
    download_media,
    extract_youtube_info,
    parse_telegram_url,
    parse_youtube_url,
    DOWNLOADS_DIR,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DOWNLOADER_BOT_TOKEN", "")
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB
PROGRESS_POLL_INTERVAL_SECONDS = 2
PROGRESS_HEARTBEAT_SECONDS = 15

# Nginx-served directory for large files
NGINX_DIR = Path(os.environ.get("NGINX_DIR", "/var/www/downloads"))
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:9090/files")

URL_PATTERN = re.compile(r"https?://\S+")

# Allowed Telegram user IDs
ALLOWED_IDS = {2556187, 504147733, 5967132135}


async def _safe_edit(status_msg, text: str) -> None:
    """Edit status message, ignoring Telegram API errors."""
    try:
        await status_msg.edit_text(text)
    except Exception as e:
        logger.warning(f"Failed to edit status message: {e}")


async def _is_allowed(update: Update) -> bool:
    """Check if user is allowed to use the bot."""
    user = update.effective_user
    if not user:
        return False
    if user.id in ALLOWED_IDS:
        return True
    logger.info(f"Access denied for user_id={user.id} username=@{user.username}")
    return False


async def start_command(update: Update, context) -> None:
    """Handle /start command."""
    if not await _is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return
    await update.message.reply_text(
        "Send me a URL from YouTube, X/Twitter, Instagram, or Telegram.\n"
        "I'll download the media and send it back in best quality.\n\n"
        "Supported:\n"
        "- YouTube videos, Shorts, playlists\n"
        "- X/Twitter posts (image/video)\n"
        "- Instagram posts, reels, stories\n"
        "- Telegram channels (all media)\n\n"
        "Media is sent as documents to preserve original quality."
    )


async def clean_command(update: Update, context) -> None:
    """Handle /clean — delete all downloaded files."""
    if not await _is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return

    deleted = 0
    freed = 0
    for directory in [DOWNLOADS_DIR, NGINX_DIR]:
        if not directory.exists():
            continue
        for root, dirs, files in os.walk(directory):
            for f in files:
                path = os.path.join(root, f)
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    deleted += 1
                except OSError:
                    pass

    freed_mb = freed / (1024 * 1024)
    await update.message.reply_text(f"Cleaned {deleted} files ({freed_mb:.1f} MB freed).")
    logger.info(f"Manual cleanup by user {update.effective_user.id}: {deleted} files, {freed_mb:.1f} MB")


async def handle_url(update: Update, context) -> None:
    """Handle incoming URLs — download and send media."""
    if not await _is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return
    text = update.message.text.strip()
    urls = URL_PATTERN.findall(text)

    if not urls:
        return

    for url in urls:
        await _process_url(update, context, url)


async def _process_url(update: Update, context, url: str, mp3: bool = False) -> None:
    """Download media from URL and send back to user."""
    try:
        platform = detect_platform(url)
    except ValueError as e:
        await update.message.reply_text(f"Unsupported URL: {e}")
        return

    # YouTube: show metadata and offer format choice
    if platform == "youtube" and not mp3:
        await _handle_youtube_url(update, context, url)
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    status_msg = await update.message.reply_text(f"Downloading from {platform}...")

    try:
        is_channel = False
        if platform == "telegram":
            try:
                _, msg_id = parse_telegram_url(url)
                if msg_id is None:
                    is_channel = True
            except ValueError as e:
                raise DownloadError(f"Invalid Telegram URL: {e}")

        if is_channel:
            await _handle_channel_download(update, status_msg, url)
        else:
            await _run_download(update, status_msg, url, mp3=mp3)

    except DownloadError as e:
        await _safe_edit(status_msg, f"Download failed: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error for {url}")
        await _safe_edit(status_msg, "An unexpected error occurred. Check bot logs.")


async def _run_download(update: Update, status_msg, url: str, mp3: bool = False) -> None:
    """Run download with progress updates and send files."""
    platform = detect_platform(url)
    progress_state = {
        "msg": "",
        "active": True,
        "started_at": time.monotonic(),
        "last_update_at": time.monotonic(),
        "last_heartbeat_at": 0.0,
    }

    def _on_progress(phase, pct):
        if phase == "download":
            progress_state["msg"] = f"Downloading... {pct}%"
        elif phase == "convert":
            progress_state["msg"] = f"Converting video... {pct}%"
        elif phase == "info":
            progress_state["msg"] = str(pct)
        progress_state["last_update_at"] = time.monotonic()

    def _heartbeat_message() -> str:
        elapsed_seconds = int(time.monotonic() - progress_state["started_at"])
        return f"Still downloading from {platform}... {elapsed_seconds}s elapsed."

    async def _update_progress():
        last_msg = ""
        while progress_state["active"]:
            await asyncio.sleep(PROGRESS_POLL_INTERVAL_SECONDS)
            msg = progress_state["msg"]
            now = time.monotonic()
            if msg and msg != last_msg:
                await _safe_edit(status_msg, msg)
                last_msg = msg
            elif now - progress_state["last_update_at"] >= PROGRESS_HEARTBEAT_SECONDS:
                if now - progress_state["last_heartbeat_at"] >= PROGRESS_HEARTBEAT_SECONDS:
                    heartbeat_msg = _heartbeat_message()
                    if heartbeat_msg != last_msg:
                        await _safe_edit(status_msg, heartbeat_msg)
                        last_msg = heartbeat_msg
                    progress_state["last_heartbeat_at"] = now

    progress_task = asyncio.create_task(_update_progress())
    try:
        saved = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_media(url, force=True, mp3=mp3, progress_callback=_on_progress)
        )
    finally:
        progress_state["active"] = False
        progress_task.cancel()

    await _send_files(status_msg, saved)


async def _handle_youtube_url(update: Update, context, url: str) -> None:
    """Handle YouTube URL: show metadata and format selection keyboard."""
    status_msg = await update.message.reply_text("Fetching video info...")

    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: extract_youtube_info(url)
        )
    except DownloadError as e:
        await _safe_edit(status_msg, f"Failed: {e}")
        return

    title = info.get("title", "Unknown")
    channel = info.get("channel", "Unknown")
    duration = _format_duration(info.get("duration", 0))
    playlist_count = info.get("playlist_count")

    if playlist_count:
        meta_text = f"Playlist: {title}\nChannel: {channel}\nVideos: {playlist_count}"
    else:
        views = info.get("view_count")
        views_str = f"\nViews: {views:,}" if views else ""
        meta_text = f"{title}\nChannel: {channel}\nDuration: {duration}{views_str}"

    # Store URL in bot_data keyed by chat_id:message_id (unique across chats)
    chat_id = status_msg.chat_id
    msg_key = f"yt_{chat_id}_{status_msg.message_id}"
    context.bot_data[msg_key] = url

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Video", callback_data=f"yt:video:{chat_id}:{status_msg.message_id}"),
            InlineKeyboardButton("MP3", callback_data=f"yt:mp3:{chat_id}:{status_msg.message_id}"),
        ]
    ])

    try:
        await status_msg.edit_text(meta_text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Failed to show YouTube options: {e}")
        context.bot_data.pop(msg_key, None)
        await _safe_edit(status_msg, "Failed to show download options. Send the URL again.")


async def handle_youtube_callback(update: Update, context) -> None:
    """Handle Video/MP3 button press for YouTube downloads."""
    query = update.callback_query

    if not await _is_allowed(update):
        await query.answer("Access restricted.", show_alert=True)
        return

    await query.answer()

    data = query.data
    if not data.startswith("yt:"):
        return

    parts = data.split(":", 3)
    if len(parts) != 4:
        logger.warning(f"Malformed YouTube callback data: {data}")
        await query.edit_message_text("Something went wrong. Please send the URL again.")
        return

    _, format_choice, chat_id, msg_id = parts
    mp3 = format_choice == "mp3"

    # Retrieve URL from bot_data
    msg_key = f"yt_{chat_id}_{msg_id}"
    url = context.bot_data.pop(msg_key, None)
    if not url:
        await query.edit_message_text("Session expired. Please send the URL again.")
        return

    # Remove the keyboard
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Failed to remove keyboard: {e}")

    format_label = "MP3" if mp3 else "video"
    status_msg = query.message
    await _safe_edit(status_msg, f"Downloading {format_label}...")

    try:
        await _run_download(update, status_msg, url, mp3=mp3)
    except DownloadError as e:
        await _safe_edit(status_msg, f"Download failed: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error for YouTube {url}")
        await _safe_edit(status_msg, "An unexpected error occurred. Check bot logs.")


async def _handle_channel_download(update: Update, status_msg, url: str) -> None:
    """Download entire channel and send in batches."""
    output_dir = DOWNLOADS_DIR / "telegram"
    output_dir.mkdir(parents=True, exist_ok=True)

    import time as _time
    progress_state = {"msg": "", "active": True, "start": _time.time()}

    def _on_channel_progress(current, total, overall_pct):
        elapsed = _time.time() - progress_state["start"]
        eta_str = ""
        if overall_pct > 0 and elapsed > 5:
            eta_seconds = int(elapsed / overall_pct * (100 - overall_pct))
            if eta_seconds >= 3600:
                h = eta_seconds // 3600
                m = (eta_seconds % 3600) // 60
                s = eta_seconds % 60
                eta_str = f"\nETA: ~{h}h {m}m {s}s left"
            elif eta_seconds >= 60:
                m = eta_seconds // 60
                s = eta_seconds % 60
                eta_str = f"\nETA: ~{m}m {s}s left"
            else:
                eta_str = f"\nETA: ~{eta_seconds}s left"
        progress_state["msg"] = f"Downloading: {current} of {total} files (Progress: {overall_pct}%){eta_str}"

    async def _update_progress():
        last_msg = ""
        while progress_state["active"]:
            await asyncio.sleep(2)
            msg = progress_state["msg"]
            if msg and msg != last_msg:
                await _safe_edit(status_msg, msg)
                last_msg = msg

    progress_task = asyncio.create_task(_update_progress())
    try:
        loop = asyncio.get_event_loop()
        saved = await loop.run_in_executor(
            None, functools.partial(_download_telegram_channel, url, output_dir, progress_callback=_on_channel_progress)
        )
    finally:
        progress_state["active"] = False
        progress_task.cancel()

    if not saved:
        await _safe_edit(status_msg, "No media found in this channel.")
        return

    await _safe_edit(status_msg, f"Downloaded {len(saved)} files. Sending...")
    await _send_files(status_msg, saved)


async def _send_files(status_msg, file_paths: list[str]) -> None:
    """Send files to user as documents, grouped in albums of 10.

    Uses status_msg.chat for sending — works for both message and callback contexts.
    """
    if not file_paths:
        await _safe_edit(status_msg, "No files to send.")
        return

    chat = status_msg.chat

    # Separate into sendable (≤50MB) and too-large (>50MB)
    sendable = []
    too_large = []

    for path in file_paths:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            logger.error(f"File missing before upload: {path}")
            continue
        if size <= TELEGRAM_UPLOAD_LIMIT:
            sendable.append(path)
        else:
            too_large.append((path, size))

    # Send sendable files as albums of 10
    sent_count = 0
    for i in range(0, len(sendable), 10):
        batch = sendable[i:i + 10]
        try:
            await chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        except Exception as e:
            logger.warning(f"Failed to send upload action for batch {i + 1}–{i + len(batch)}: {e}")

        try:
            if len(batch) == 1:
                with open(batch[0], "rb") as f:
                    await chat.send_document(
                        document=f,
                        filename=os.path.basename(batch[0]),
                    )
            else:
                media_group = []
                file_handles = []
                for path in batch:
                    fh = open(path, "rb")
                    file_handles.append(fh)
                    media_group.append(InputMediaDocument(media=fh, filename=os.path.basename(path)))
                try:
                    await chat.send_media_group(media=media_group)
                finally:
                    for fh in file_handles:
                        fh.close()

            sent_count += len(batch)
        except Exception as e:
            logger.error(f"Failed to send batch {i + 1}–{i + len(batch)}: {e}")
            await chat.send_message(
                f"Failed to send files {i + 1}–{i + len(batch)}. Continuing with rest..."
            )

        if len(sendable) > 10:
            await _safe_edit(status_msg, f"Sent {sent_count}/{len(sendable)} files...")

    # Handle too-large files via nginx link
    for path, size_bytes in too_large:
        filename = os.path.basename(path)
        size_mb = size_bytes / (1024 * 1024)
        link = _serve_large_file(path)
        if link:
            await chat.send_message(
                f"File too large for Telegram ({size_mb:.1f} MB):\n"
                f"{filename}\n"
                f"{link}\n\n"
                f"Link expires in 24 hours.",
                disable_web_page_preview=True,
            )
        else:
            await chat.send_message(
                f"File too large ({filename}, {size_mb:.1f} MB). Could not serve via download link."
            )

    # Final status
    summary = f"Done: {sent_count} sent"
    if too_large:
        summary += f", {len(too_large)} as download link(s)"
    await _safe_edit(status_msg, summary)


def _serve_large_file(filepath: str) -> str | None:
    """Move large file to nginx-served directory, return URL."""
    if not NGINX_DIR.exists():
        try:
            NGINX_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Cannot create nginx directory {NGINX_DIR}: {e}")
            return None

    filename = os.path.basename(filepath)
    dest = NGINX_DIR / filename
    if dest.exists():
        stem = Path(filename).stem
        ext = Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = NGINX_DIR / f"{stem}_{counter}{ext}"
            counter += 1
        filename = dest.name

    try:
        shutil.move(filepath, dest)
    except (OSError, shutil.Error) as e:
        logger.error(f"Failed to move {filepath} to {dest}: {e}")
        return None

    return f"{SERVER_URL.rstrip('/')}/{quote(filename)}"


# Map filenames users might send to the expected cookie file paths
_COOKIE_FILENAME_MAP = {}
for _platform, _path in COOKIES_FILES.items():
    _COOKIE_FILENAME_MAP[_path.name] = _path


async def handle_cookie_file(update: Update, context) -> None:
    """Handle uploaded cookie files — replace existing cookies on disk."""
    if not await _is_allowed(update):
        await update.message.reply_text("Access restricted.")
        return

    doc = update.message.document
    filename = doc.file_name

    target_path = _COOKIE_FILENAME_MAP.get(filename)
    if not target_path:
        # Not a recognized cookie file — ignore, let other handlers deal with it
        return

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(target_path))

    platform = target_path.stem.replace("_cookies", "").replace("www.", "").replace(".com", "")
    logger.info(f"Cookie file updated: {filename} by user {update.effective_user.id}")
    await update.message.reply_text(f"Cookies updated for {platform}.")


def main():
    if not BOT_TOKEN:
        print("Error: DOWNLOADER_BOT_TOKEN environment variable not set.", flush=True)
        raise SystemExit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clean", clean_command))
    app.add_handler(CallbackQueryHandler(handle_youtube_callback, pattern=r"^yt:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started. Waiting for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
