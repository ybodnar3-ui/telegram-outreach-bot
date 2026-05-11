#!/bin/bash
set -e
mkdir -p /data/sessions

# Copy bundled session files (if any) to persistent volume
for f in sessions/*.session; do
    [ -f "$f" ] || continue
    dest="/data/sessions/$(basename "$f")"
    if [ ! -f "$dest" ]; then
        cp "$f" "$dest"
        echo "Copied session: $(basename "$f")"
    fi
done

# TELEGRAM_SESSION_N env vars are decoded by main.py using Python (gzip+base64)
exec python3 main.py
