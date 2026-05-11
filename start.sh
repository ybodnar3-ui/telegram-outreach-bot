#!/bin/bash
set -e
mkdir -p /data/sessions

# Copy bundled session files (accounts 1 & 2, pre-existing on volume)
for f in sessions/*.session; do
    [ -f "$f" ] || continue
    dest="/data/sessions/$(basename "$f")"
    if [ ! -f "$dest" ]; then
        cp "$f" "$dest"
        echo "Copied session: $(basename "$f")"
    fi
done

# Decode any gzip+base64 encoded sessions from env vars (TELEGRAM_SESSION_1..9)
# Used for session files too large for gitignore bypass or when not bundled.
for i in 1 2 3 4 5 6 7 8 9; do
    var="TELEGRAM_SESSION_${i}"
    phone_var="TELEGRAM_PHONE_${i}"
    val="${!var}"
    phone="${!phone_var}"
    if [ -n "$val" ] && [ -n "$phone" ]; then
        phone_stripped="${phone#+}"
        dest="/data/sessions/${phone_stripped}.session"
        if [ ! -f "$dest" ]; then
            echo "$val" | base64 -d | gunzip > "$dest"
            echo "Decoded session from env: ${phone_stripped}.session"
        fi
    fi
done

exec python3 main.py
