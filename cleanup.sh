#!/bin/bash
# Delete files older than 24 hours from downloads and nginx directories
find /app/downloads -type f -mmin +1440 -delete 2>/dev/null
find /var/www/downloads -type f -mmin +1440 -delete 2>/dev/null
echo "$(date): Cleanup complete"
