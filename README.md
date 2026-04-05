# X-Downloader

CLI tool and Telegram bot for downloading media from X (Twitter), Instagram, and Telegram in best quality.

## CLI Setup (local use)

```bash
cd X-downloader
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Cookies

Export cookies from your browser using a cookie extension (e.g., "Get cookies.txt") and save to the project root:

- `x_cookies.txt` — X/Twitter
- `www.instagram.com_cookies.txt` — Instagram

### Telegram Setup

```bash
./venv/bin/python setup_telegram.py
```

Enter your phone number and the code Telegram sends you. Creates `telegram.session`.

### Check Cookie Health

```bash
./download --check
```

## CLI Usage

```bash
./download <url>                          # Single URL
./download <url1> <url2> <url3>           # Multiple URLs (batch)
./download -c                             # Download from clipboard
./download -f urls.txt                    # Download from file
./download --force <url>                  # Re-download even if exists
```

## CLI Examples

```bash
# X/Twitter
./download "https://x.com/user/status/1234567890"

# Instagram
./download "https://www.instagram.com/p/ABC123/"
./download "https://www.instagram.com/reel/XYZ789/"

# Telegram - single message
./download "https://t.me/channel/123"

# Telegram - full channel (10 parallel downloads)
./download "https://t.me/channel"
./download "https://web.telegram.org/a/#-1002899724101"

# Batch + clipboard
./download -c
./download -f saved_urls.txt
```

## Telegram Bot (@yh_downloader_bot)

Send any URL to the bot — it downloads media and sends it back as documents (no compression).

### Bot Features

- Send URL -> get media back as documents (original quality)
- Multiple files grouped into albums (max 10 per album)
- Files > 50MB served as download links via nginx
- Full Telegram channel download supported
- Auto-cleanup: all files deleted after 24 hours

### Bot Deployment (Docker)

```bash
# Set environment variables
export DOWNLOADER_BOT_TOKEN=your_bot_token
export SERVER_HOST=your_server_ip

# Deploy
docker-compose up -d
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DOWNLOADER_BOT_TOKEN` | yes | Telegram bot token from @BotFather |
| `SERVER_HOST` | yes | Server IP/domain for nginx download links |
| `TELEGRAM_API_ID` | no | Telegram API ID (has default) |
| `TELEGRAM_API_HASH` | no | Telegram API hash (has default) |

### Docker Services

- **bot** — Python app running bot.py with download.py
- **nginx** — Serves large files (>50MB) on port 8080

### Required Files (mount as volumes)

- `x_cookies.txt` — X/Twitter cookies
- `www.instagram.com_cookies.txt` — Instagram cookies
- `telegram.session` — Telegram session (run setup_telegram.py locally first)

## Output Structure

```
downloads/
  twitter/        @username_ID.ext
  instagram/      @username_ID.ext
  telegram/
    ChannelName/  ID.ext
```

## Supported Platforms

| Platform | Images | Video | Full Channel | Auth |
|----------|--------|-------|-------------|------|
| X/Twitter | yes | yes | no | Optional (NSFW/private) |
| Instagram | yes | yes | no | Recommended |
| Telegram | yes | yes | yes (10 threads) | Required (Telegram API) |

## Dependencies

- Python 3.13+
- ffmpeg
- yt-dlp, gallery-dl, telethon, requests, python-telegram-bot
