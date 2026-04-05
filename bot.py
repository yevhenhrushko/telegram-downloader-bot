#!/usr/bin/env python3
"""Telegram bot for downloading media from X/Twitter, Instagram, and Telegram."""

import asyncio
import functools
import logging
import os
import re
import shutil
from pathlib import Path

from telegram import (
    InputMediaDocument,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:9090/files")

URL_PATTERN = re.compile(r"https?://\S+")

# Allowed users (usernames without @, and phone numbers)
ALLOWED_USERS = {
    "top_photographer",
    "yevhen_hrushko",
}
ALLOWED_PHONES = {
    "+380682649098",
}
# Cache of verified user IDs (populated at runtime)
_verified_user_ids: set[int] = set()


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
    if user.id in _verified_user_ids:
        return True
    if user.username and user.username.lower() in ALLOWED_USERS:
        _verified_user_ids.add(user.id)
        logger.info(f"Granted access to user_id={user.id} via username=@{user.username}")
        return True
    logger.info(f"Access denied for user_id={user.id} username=@{user.username}")
    return False


async def start_command(update: Update, context) -> None:
    """Handle /start command."""
    if not await _is_allowed(update):
        await update.message.reply_text(
            "Access restricted. Use /auth to verify via phone number."
        )
        return
    await update.message.reply_text(
        "Send me a URL from X/Twitter, Instagram, or Telegram.\n"
        "I'll download the media and send it back in best quality.\n\n"
        "Supported:\n"
        "- Single posts (image/video)\n"
        "- Telegram channels (all media)\n\n"
        "Media is sent as documents to preserve original quality."
    )


async def auth_command(update: Update, context) -> None:
    """Handle /auth — request phone number for verification."""
    user = update.effective_user
    if user and user.id in _verified_user_ids:
        await update.message.reply_text("You're already verified.")
        return
    if user and user.username and user.username.lower() in ALLOWED_USERS:
        _verified_user_ids.add(user.id)
        logger.info(f"Granted access to user_id={user.id} via username=@{user.username}")
        await update.message.reply_text("Verified by username. You're in!")
        return

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("Share phone number", request_contact=True)]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        "Please share your phone number to verify access.",
        reply_markup=keyboard,
    )


async def handle_contact(update: Update, context) -> None:
    """Handle shared contact for phone verification."""
    contact = update.message.contact
    if not contact:
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text(
            "Could not verify. Please try again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"

    if phone in ALLOWED_PHONES:
        _verified_user_ids.add(user.id)
        await update.message.reply_text(
            "Verified! You can now send URLs.",
            reply_markup=ReplyKeyboardRemove(),
        )
        logger.info(f"Granted access to user_id={user.id} via phone {phone}")
    else:
        await update.message.reply_text(
            "Phone number not authorized.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def handle_url(update: Update, context) -> None:
    """Handle incoming URLs — download and send media."""
    if not await _is_allowed(update):
        await update.message.reply_text("Access restricted. Use /auth to verify.")
        return
    text = update.message.text.strip()
    urls = URL_PATTERN.findall(text)

    if not urls:
        return

    for url in urls:
        await _process_url(update, url)


async def _process_url(update: Update, url: str) -> None:
    """Download media from URL and send back to user."""
    try:
        platform = detect_platform(url)
    except ValueError as e:
        await update.message.reply_text(f"Unsupported URL: {e}")
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
            saved = download_media(url, force=True)
            await _send_files(update, status_msg, saved)

    except DownloadError as e:
        await _safe_edit(status_msg, f"Download failed: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error for {url}")
        await _safe_edit(status_msg, "An unexpected error occurred. Check bot logs.")


async def _handle_channel_download(update: Update, status_msg, url: str) -> None:
    """Download entire channel and send in batches."""
    output_dir = DOWNLOADS_DIR / "telegram"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run in executor — _download_telegram_channel calls asyncio.run() internally
    loop = asyncio.get_event_loop()
    saved = await loop.run_in_executor(
        None, functools.partial(_download_telegram_channel, url, output_dir)
    )

    if not saved:
        await _safe_edit(status_msg, "No media found in this channel.")
        return

    await _safe_edit(status_msg, f"Downloaded {len(saved)} files. Sending...")
    await _send_files(update, status_msg, saved)


async def _send_files(update: Update, status_msg, file_paths: list[str]) -> None:
    """Send files to user as documents, grouped in albums of 10."""
    if not file_paths:
        await _safe_edit(status_msg, "No files to send.")
        return

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
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

        try:
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
        except Exception as e:
            logger.error(f"Failed to send batch {i + 1}–{i + len(batch)}: {e}")
            await update.message.reply_text(
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
            await update.message.reply_text(
                f"File too large for Telegram ({size_mb:.1f} MB):\n"
                f"[{filename}]({link})\n\n"
                f"Link expires in 24 hours.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
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

    return f"{SERVER_URL}/{filename}"


def main():
    if not BOT_TOKEN:
        print("Error: DOWNLOADER_BOT_TOKEN environment variable not set.", flush=True)
        raise SystemExit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("auth", auth_command))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started. Waiting for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
