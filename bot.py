#!/usr/bin/env python3
"""Telegram bot for downloading media from X/Twitter, Instagram, and Telegram."""

import logging
import os
import re
from pathlib import Path

from telegram import InputMediaDocument, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from download import (
    DownloadError,
    _download_telegram_channel,
    detect_platform,
    download_media,
    parse_telegram_url,
    DOWNLOADS_DIR,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DOWNLOADER_BOT_TOKEN", "")
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB

# Nginx-served directory for large files
NGINX_DIR = Path(os.environ.get("NGINX_DIR", "/var/www/downloads"))
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080/files")

URL_PATTERN = re.compile(r"https?://\S+")


async def start_command(update: Update, context) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Send me a URL from X/Twitter, Instagram, or Telegram.\n"
        "I'll download the media and send it back in best quality.\n\n"
        "Supported:\n"
        "- Single posts (image/video)\n"
        "- Telegram channels (all media)\n\n"
        "Media is sent as documents to preserve original quality."
    )


async def handle_url(update: Update, context) -> None:
    """Handle incoming URLs — download and send media."""
    text = update.message.text.strip()
    urls = URL_PATTERN.findall(text)

    if not urls:
        return

    for url in urls:
        await _process_url(update, url)


async def _process_url(update: Update, url: str) -> None:
    """Download media from URL and send back to user."""
    # Validate platform
    try:
        platform = detect_platform(url)
    except ValueError as e:
        await update.message.reply_text(f"Unsupported URL: {e}")
        return

    # Send typing indicator
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    status_msg = await update.message.reply_text(f"Downloading from {platform}...")

    try:
        # Check if this is a full channel download
        is_channel = False
        if platform == "telegram":
            _, msg_id = parse_telegram_url(url)
            if msg_id is None:
                is_channel = True

        if is_channel:
            await _handle_channel_download(update, status_msg, url)
        else:
            saved = download_media(url, force=True)
            await _send_files(update, status_msg, saved)

    except DownloadError as e:
        await status_msg.edit_text(f"Download failed: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error for {url}")
        await status_msg.edit_text(f"Error: {e}")


async def _handle_channel_download(update: Update, status_msg, url: str) -> None:
    """Download entire channel and send in batches."""
    output_dir = DOWNLOADS_DIR / "telegram"
    output_dir.mkdir(parents=True, exist_ok=True)

    saved = _download_telegram_channel(url, output_dir)

    if not saved:
        await status_msg.edit_text("No media found in this channel.")
        return

    await status_msg.edit_text(f"Downloaded {len(saved)} files. Sending...")
    await _send_files(update, status_msg, saved)


async def _send_files(update: Update, status_msg, file_paths: list[str]) -> None:
    """Send files to user as documents, grouped in albums of 10."""
    if not file_paths:
        await status_msg.edit_text("No files to send.")
        return

    # Separate into sendable (≤50MB) and too-large (>50MB)
    sendable = []
    too_large = []

    for path in file_paths:
        size = os.path.getsize(path)
        if size <= TELEGRAM_UPLOAD_LIMIT:
            sendable.append(path)
        else:
            too_large.append(path)

    # Send sendable files as albums of 10
    sent_count = 0
    for i in range(0, len(sendable), 10):
        batch = sendable[i:i + 10]
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

        if len(batch) == 1:
            with open(batch[0], "rb") as f:
                await update.message.reply_document(
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
                await update.message.reply_media_group(media=media_group)
            finally:
                for fh in file_handles:
                    fh.close()

        sent_count += len(batch)
        if len(sendable) > 10:
            await status_msg.edit_text(f"Sent {sent_count}/{len(sendable)} files...")

    # Handle too-large files via nginx link
    for path in too_large:
        link = _serve_large_file(path)
        if link:
            filename = os.path.basename(path)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            await update.message.reply_text(
                f"File too large for Telegram ({size_mb:.1f} MB):\n"
                f"[{filename}]({link})\n\n"
                f"Link expires in 24 hours.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"File too large ({os.path.basename(path)}), and nginx serving is not configured."
            )

    # Final status
    total = len(sendable) + len(too_large)
    summary = f"Done: {len(sendable)} sent"
    if too_large:
        summary += f", {len(too_large)} as download link(s)"
    await status_msg.edit_text(summary)


def _serve_large_file(filepath: str) -> str | None:
    """Move large file to nginx-served directory, return URL."""
    if not NGINX_DIR.exists():
        try:
            NGINX_DIR.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return None

    filename = os.path.basename(filepath)
    dest = NGINX_DIR / filename
    # Avoid name collisions
    if dest.exists():
        stem = Path(filename).stem
        ext = Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = NGINX_DIR / f"{stem}_{counter}{ext}"
            counter += 1
        filename = dest.name

    import shutil
    shutil.move(filepath, dest)
    return f"{SERVER_URL}/{filename}"


def main():
    if not BOT_TOKEN:
        print("Error: DOWNLOADER_BOT_TOKEN environment variable not set.", flush=True)
        raise SystemExit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started. Waiting for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
