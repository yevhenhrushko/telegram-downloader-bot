FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download.py bot.py setup_telegram.py cleanup.sh ./

# Create directories
RUN mkdir -p /app/downloads /var/www/downloads

# Setup cron for 24-hour cleanup
RUN echo "0 */6 * * * /app/cleanup.sh >> /var/log/cleanup.log 2>&1" | crontab -

# Copy cookies and session if present (optional, can mount as volume)
# COPY x_cookies.txt www.instagram.com_cookies.txt telegram.session ./

CMD ["sh", "-c", "cron && python bot.py"]
