"""
config.py — Central configuration for the Telegram Outreach Bot.

=== BUSINESS CONTEXT (for LLMs reading this file) ===
This bot is used by an affiliate/broker who sells advertising space in 5 Telegram
groups located in Melitopol (Russian-occupied territory). The broker earns 50% of
each deal (~5000 RUB per client). The bot finds potential advertisers — any local
business that might benefit from Telegram group advertising — and sends them a
cold DM offering the ad placement service.

=== CREDENTIALS & ENV VARS ===
All sensitive values are read from environment variables (or .env locally).
Never hardcode credentials here — they will end up in GitHub.

Required env vars:
  TELEGRAM_API_ID_1    — integer app ID from my.telegram.org
  TELEGRAM_API_HASH_1  — string app hash from my.telegram.org
  TELEGRAM_PHONE_1     — phone number in international format (+XXXXXXXXXXX)

Optional env vars:
  DATA_DIR   — absolute path where sessions/, sent.csv live (default: current dir)
               On Railway set this to the volume mount path (e.g. /data)
  LOG_LEVEL  — DEBUG / INFO / WARNING (default: INFO)

=== ARCHITECTURE OVERVIEW ===
- Multiple Telegram accounts rotate sending to stay under Telegram's spam radar
- Each account is limited to MAX_MESSAGES_PER_DAY DMs per calendar day
- All sent usernames are globally deduplicated via sent.csv (one person = one DM ever)
- Groups are discovered via Telegram search using Russian-language keywords
- Only users with a public @username are targeted (no username = can't DM safely)
- All delays are randomized to simulate human behavior and avoid bot detection

=== FILE DEPENDENCIES ===
- {DATA_DIR}/sessions/{phone}.session  — Telethon session file per account
- {DATA_DIR}/sent.csv                  — append-only log of every DM sent (never delete)
- message.txt                          — the DM template text (edit freely)

=== HOW TO ADD A NEW ACCOUNT ===
1. Add env vars: TELEGRAM_API_ID_2, TELEGRAM_API_HASH_2, TELEGRAM_PHONE_2
2. Add a dict to ACCOUNTS list below following the same pattern.
Each phone must be a real Telegram account in international format (+XXXXXXXXXXX).

=== ANTI-BAN PHILOSOPHY ===
All timing constants are tuned conservatively. Do NOT lower them without understanding
the risk: Telegram issues PeerFloodError and bans on accounts that send DMs too fast.
30 msgs/day per account is a safe ceiling based on community-tested Telethon patterns.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── DATA DIRECTORY ───────────────────────────────────────────────────────────
# On Railway: set DATA_DIR to your volume mount path (e.g. /data)
# Locally: defaults to current directory

DATA_DIR = os.environ.get("DATA_DIR", ".")

# ─── ACCOUNT ROSTER ──────────────────────────────────────────────────────────
# Credentials come from env vars — never hardcode here.
# Add more dicts to scale to multiple accounts.

def _build_accounts():
    accounts = []
    for i in range(1, 10):
        api_id = os.environ.get(f"TELEGRAM_API_ID_{i}")
        api_hash = os.environ.get(f"TELEGRAM_API_HASH_{i}")
        phone = os.environ.get(f"TELEGRAM_PHONE_{i}")
        if not (api_id and api_hash and phone):
            break
        accounts.append({
            "api_id":  int(api_id),
            "api_hash": api_hash,
            "phone":    phone,
            "label":    f"account_{i}",
        })
    return accounts

ACCOUNTS = _build_accounts()

# ─── EXCLUDED GROUPS ─────────────────────────────────────────────────────────
# Groups we sell ads IN — never parse leads from them.
# Stored lowercase; matching is case-insensitive.

EXCLUDED_GROUPS = {
    "melitopol2024b",
    "melitopolonlain",
    "baraxolca_melitopol",
    "avtohubmelitopol",
    "baraxolkamlz",
}

# ─── SEARCH KEYWORDS ─────────────────────────────────────────────────────────
# Russian-language keywords used to discover Telegram groups via search.
# Mix of geo-specific (Мелитополь) and business/entrepreneur terms.

SEARCH_KEYWORDS = [
    "Мелитополь",
    "Мелитополь бизнес",
    "Мелитополь реклама",
    "Мелитополь барахолка",
    "Мелитополь услуги",
    "Мелитополь магазин",
    "ДНР бизнес",
    "ДНР предприниматели",
    "реклама телеграм",
]

# ─── SENDING LIMITS & TIMING ─────────────────────────────────────────────────
# MAX_MESSAGES_PER_DAY: safe daily ceiling per account. Do not raise above 40.
# DELAY_*: random sleep between DMs — mimics human behavior, avoids spam detection.
# Never set DELAY_MIN below 30 seconds.

MAX_MESSAGES_PER_DAY = 10

DELAY_MIN_SECONDS = 600   # 10 min
DELAY_MAX_SECONDS = 900   # 15 min

GROUP_INTERACTION_DELAY_MIN = 5
GROUP_INTERACTION_DELAY_MAX = 15

# Telethon GetParticipants chunk size. Keep at 200 — higher values increase flood risk.
MEMBER_FETCH_LIMIT = 200

# ─── FILE PATHS ──────────────────────────────────────────────────────────────

SESSIONS_DIR      = os.path.join(DATA_DIR, "sessions")
SENT_CSV_PATH     = os.path.join(DATA_DIR, "sent.csv")
MESSAGE_FILE_PATH = "message.txt"

# ─── CSV SCHEMA ──────────────────────────────────────────────────────────────
# Column order in sent.csv. Must match the writer in sender.py.

CSV_HEADERS = ["username", "full_name", "group_title", "group_username", "sent_at", "sent_from_account"]

# ─── SEARCH SETTINGS ─────────────────────────────────────────────────────────

MAX_GROUPS_PER_KEYWORD = 10
MIN_GROUP_MEMBERS      = 50

# ─── APPROVED GROUPS (WHITELIST) ─────────────────────────────────────────────
# When non-empty, main.py targets ONLY these groups (in addition to EXCLUDED filter).
# When empty (default), all discovered groups are eligible — recommended.

APPROVED_GROUPS: set = set()

# ─── PRIORITY GROUPS ─────────────────────────────────────────────────────────
# Groups to parse FIRST on every cycle, before keyword search results.
# Bot fetches these directly by username — no search needed.
# Stored lowercase without @.

PRIORITY_GROUPS = [
    "obyavlenia_melytopol",
    "avtobaraholka_melitopol",
    "nedvizhimost1_melitopol",
    "melitopol_nedvijimostu",
    "melitopolru2022",
]
