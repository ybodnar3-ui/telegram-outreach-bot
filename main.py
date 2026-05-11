"""
main.py — Entry point and orchestrator for the Telegram Outreach Bot.

=== WHAT THIS SCRIPT DOES ===
Finds potential advertising clients for 5 Telegram groups based in Melitopol and
sends them a cold DM offering ad placement services.

=== 24/7 OPERATION & QUIET HOURS ===
The bot runs in an infinite loop. It sends messages only between 08:00 and 23:00
Kyiv time (UTC+3). Outside this window it sleeps and resumes automatically at 08:00.
Ukraine has been permanently on UTC+3 (EEST) since 2024 — no DST adjustments needed.

=== DAILY FLOW ===
1. If quiet hours (23:00–08:00 Kyiv) → sleep until 08:00, then start
2. Load sent.csv → build dedup set of already-contacted usernames
3. Discover groups via group_finder using SEARCH_KEYWORDS
4. Parse members, send DMs with 45–90s delays between each
5. If 23:00 is reached mid-cycle → pause until 08:00 next day
6. When daily limits hit → sleep until next 08:00

=== ENV VARS REQUIRED ===
See config.py for the full list.
  TELEGRAM_API_ID_1, TELEGRAM_API_HASH_1, TELEGRAM_PHONE_1
  DATA_DIR (on Railway: set to your volume mount path, e.g. /data)
  LOG_LEVEL (optional, default INFO)
"""

import asyncio
import base64
import csv
import gzip
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import PeerFloodError

from config import ACCOUNTS, SESSIONS_DIR, SENT_CSV_PATH, LEADS_CSV_PATH, CSV_HEADERS, MESSAGE_FILE_PATH, PRIORITY_GROUPS
from account_manager import AccountManager, AllAccountsExhaustedError
from group_finder import find_groups
from member_parser import parse_members
from sender import send_dm
import notifier

# ─── LOGGING ─────────────────────────────────────────────────────────────────

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── TIMEZONE ────────────────────────────────────────────────────────────────
# Ukraine has been permanently on UTC+3 since 2024 (no DST).

KYIV_TZ = timezone(timedelta(hours=3))
QUIET_HOUR_START = 23   # stop sending at 23:00 Kyiv
QUIET_HOUR_END   = 8    # resume sending at 08:00 Kyiv


def now_kyiv() -> datetime:
    return datetime.now(tz=KYIV_TZ)


def is_quiet_hours() -> bool:
    hour = now_kyiv().hour
    return hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END


async def sleep_until_8am():
    """Sleep until 08:00 Kyiv time. Works whether it's late night or early morning."""
    now = now_kyiv()
    wake = now.replace(hour=QUIET_HOUR_END, minute=0, second=0, microsecond=0)
    if now.hour >= QUIET_HOUR_END:
        wake += timedelta(days=1)
    sleep_seconds = (wake - now).total_seconds()
    logger.info(
        f"Quiet hours (23:00–08:00 Kyiv). "
        f"Sleeping {sleep_seconds / 3600:.1f}h until {wake.strftime('%Y-%m-%d 08:00 Kyiv')}"
    )
    await asyncio.sleep(sleep_seconds)


# ─── STARTUP CHECKS ──────────────────────────────────────────────────────────

def decode_sessions_from_env():
    """Decode any TELEGRAM_SESSION_N env vars (gzip+base64) into SESSIONS_DIR."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    for account in ACCOUNTS:
        idx = account["label"].split("_")[1]
        env_var = f"TELEGRAM_SESSION_{idx}"
        phone = account["phone"].lstrip("+")
        dest = os.path.join(SESSIONS_DIR, f"{phone}.session")
        val = os.environ.get(env_var, "").strip()
        if val and not os.path.exists(dest):
            try:
                data = gzip.decompress(base64.b64decode(val))
                with open(dest, "wb") as f:
                    f.write(data)
                logger.info(f"Decoded session from env {env_var} → {phone}.session ({len(data)} bytes)")
            except Exception as e:
                logger.error(f"Failed to decode {env_var}: {e}")


def validate_environment():
    """Fail fast at startup if anything critical is missing."""
    errors = []

    if not os.path.exists(MESSAGE_FILE_PATH):
        errors.append(f"message.txt not found at '{MESSAGE_FILE_PATH}'")
    else:
        try:
            with open(MESSAGE_FILE_PATH, "r", encoding="utf-8") as f:
                if not f.read().strip():
                    errors.append("message.txt is empty — add the DM text before running")
        except Exception as e:
            errors.append(f"Cannot read message.txt: {e}")

    if not ACCOUNTS:
        errors.append("ACCOUNTS list is empty — check env vars TELEGRAM_API_ID_1 etc.")

    for acc in ACCOUNTS:
        if not acc.get("api_id") or not acc.get("api_hash") or not acc.get("phone"):
            errors.append(f"Incomplete credentials for {acc.get('label', '?')}")

    if errors:
        for e in errors:
            logger.error(f"CONFIG ERROR: {e}")
        sys.exit(1)

    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
    except OSError as e:
        logger.error(
            f"Cannot create sessions directory '{SESSIONS_DIR}': {e}\n"
            f"  → On Railway: make sure a persistent volume is mounted at DATA_DIR={os.environ.get('DATA_DIR', '.')}"
        )
        sys.exit(1)

    logger.info(f"Startup OK — {len(ACCOUNTS)} account(s) configured")
    logger.info(f"Quiet hours: {QUIET_HOUR_START}:00–{QUIET_HOUR_END:02d}:00 Kyiv time")


# ─── CSV HELPERS ─────────────────────────────────────────────────────────────

def load_sent_usernames():
    if not os.path.exists(SENT_CSV_PATH):
        return set()
    try:
        with open(SENT_CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return {row["username"].lower() for row in reader if row.get("username")}
    except Exception as e:
        logger.error(f"Could not read sent.csv — dedup disabled for this cycle: {e}")
        return set()


def load_today_send_counts():
    """Count messages already sent today per account label — survives server restarts."""
    if not os.path.exists(SENT_CSV_PATH):
        return {}
    today = datetime.now(tz=KYIV_TZ).strftime("%Y-%m-%d")
    counts: dict = {}
    try:
        with open(SENT_CSV_PATH, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("sent_at", "").startswith(today):
                    label = row.get("sent_from_account", "")
                    if label:
                        counts[label] = counts.get(label, 0) + 1
    except Exception as e:
        logger.warning(f"Could not load today's send counts: {e}")
    return counts


def ensure_csv_headers():
    if os.path.exists(SENT_CSV_PATH):
        return
    with open(SENT_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()


# ─── CLIENT SETUP ────────────────────────────────────────────────────────────

def make_client(account):
    session_path = os.path.join(SESSIONS_DIR, account["phone"].lstrip("+"))
    return TelegramClient(
        session_path,
        account["api_id"],
        account["api_hash"],
        connection_retries=-1,      # reconnect indefinitely on Railway network drops
        retry_delay=5,              # 5s between reconnect attempts
        flood_sleep_threshold=300,  # auto-absorb FloodWait up to 5 min silently
        receive_updates=False,      # bot only sends — skip incoming update processing
    )


async def connect_clients():
    clients = {}
    failed = []
    for account in ACCOUNTS:
        idx = account["label"].split("_")[1]
        phone = account["phone"]
        session_file = os.path.join(SESSIONS_DIR, phone.lstrip("+")) + ".session"
        auth_code_env = f"TELEGRAM_AUTH_CODE_{idx}"
        auth_state_file = os.path.join(SESSIONS_DIR, f"auth_pending_{idx}.json")
        pending = os.environ.get(auth_code_env, "").strip()

        # ── Two-step Railway auth ─────────────────────────────────────────────
        # Step 1: Set TELEGRAM_AUTH_CODE_N=SEND → bot sends code to phone, saves hash
        # Step 2: Set TELEGRAM_AUTH_CODE_N=<digits> → bot signs in with that code
        if pending == "SEND":
            try:
                # Remove stale/invalid session so Telegram issues a fresh code
                if os.path.exists(session_file):
                    os.remove(session_file)
                    logger.info(f"Removed stale session for {account['label']} before re-auth")
                client = make_client(account)
                await client.connect()
                sent = await client.send_code_request(phone)
                with open(auth_state_file, "w") as f:
                    json.dump({"phone_code_hash": sent.phone_code_hash}, f)
                logger.info(
                    f"[AUTH STEP 1] Code sent to {phone}. "
                    f"Now set {auth_code_env}=<the_code> in Railway dashboard → Save → redeploy."
                )
                await client.disconnect()
            except Exception as e:
                logger.error(f"Failed to send code for {account['label']}: {e}")
            continue  # account not connected yet

        if pending.isdigit() and os.path.exists(auth_state_file):
            try:
                with open(auth_state_file) as f:
                    state = json.load(f)
                client = make_client(account)
                await client.connect()
                await client.sign_in(phone, pending, phone_code_hash=state["phone_code_hash"])
                os.remove(auth_state_file)
                clients[phone] = client
                logger.info(f"[AUTH STEP 2] Connected: {account['label']} — session created on Railway IP")
                continue
            except Exception as e:
                logger.error(f"Failed to sign in {account['label']} with provided code: {e}")
                continue

        # ── Normal connect (session already exists) ───────────────────────────
        if not os.path.exists(session_file):
            logger.error(
                f"Session file missing for {account['label']}.\n"
                f"  → Set {auth_code_env}=SEND in Railway dashboard, redeploy, then set the code."
            )
            failed.append(account["label"])
            continue
        try:
            client = make_client(account)
            await client.start(phone=phone)
            clients[phone] = client
            logger.info(f"Connected: {account['label']} ({phone})")
        except Exception as e:
            logger.error(f"Failed to connect {account['label']}: {e}")
            failed.append(account["label"])

    if not clients:
        logger.error("No accounts connected — exiting.")
        sys.exit(1)

    # Register all clients with notifier — it tries each in order if one fails
    notifier.set_clients(list(clients.values()))
    connected = [a["label"] for a in ACCOUNTS if a["phone"] in clients]
    await notifier.notify_startup(connected, failed)

    return clients


# ─── DAILY OUTREACH CYCLE ────────────────────────────────────────────────────

async def run_daily_cycle(clients):
    ensure_csv_headers()
    already_sent = load_sent_usernames()
    logger.info(f"Daily cycle start — {len(already_sent)} usernames already contacted")

    today_counts = load_today_send_counts()
    if today_counts:
        logger.info(f"Restored today's send counts from CSV: {today_counts}")
    manager = AccountManager(ACCOUNTS, today_counts)
    csv_lock = asyncio.Lock()
    clients_list = list(clients.values())
    clients_phones = list(clients.keys())
    primary_client = clients_list[0]  # first successfully connected account
    sent_count = 0
    skipped_count = 0

    # ── Fetch priority groups first ───────────────────────────────────────────
    priority_groups = []
    priority_ids = set()
    for username in PRIORITY_GROUPS:
        try:
            entity = await primary_client.get_entity(username)
            priority_groups.append(entity)
            priority_ids.add(entity.id)
            logger.info(f"Priority group loaded: @{username}")
        except Exception as e:
            logger.warning(f"Could not fetch priority group @{username}: {e}")

    # ── Keyword search for remaining groups ───────────────────────────────────
    logger.info("Discovering groups via keyword search...")
    try:
        discovered = await find_groups(primary_client)
    except Exception as e:
        logger.error(f"Group discovery failed: {e}", exc_info=True)
        discovered = []

    # Combine: priority first, then discovered (skip duplicates)
    groups = priority_groups + [g for g in discovered if g.id not in priority_ids]

    if not groups:
        logger.warning("No groups found — check SEARCH_KEYWORDS or network connectivity")
        return

    logger.info(f"Found {len(groups)} groups ({len(priority_groups)} priority + {len(discovered)} discovered)")

    # ── Parse all groups in parallel, distributed across accounts ────────────
    logger.info(f"Parsing {len(groups)} groups in parallel across {len(clients_list)} account(s)...")
    parse_tasks = [
        parse_members(clients_list[i % len(clients_list)], group, already_sent,
                      clients_phones[i % len(clients_phones)])
        for i, group in enumerate(groups)
    ]
    parse_results = await asyncio.gather(*parse_tasks, return_exceptions=True)

    all_members = []
    for i, result in enumerate(parse_results):
        group_title = getattr(groups[i], "title", str(groups[i].id))
        if isinstance(result, Exception):
            logger.error(f"Failed to parse '{group_title}': {result}")
        else:
            logger.info(f"  '{group_title}' → {len(result)} leads")
            all_members.extend(result)

    # Deduplicate across groups — same person may appear in multiple groups
    seen_keys: set = set()
    unique_members = []
    for m in all_members:
        key = m["username"].lower() if m.get("username") else f"id:{m.get('user_id')}"
        if key not in seen_keys:
            seen_keys.add(key)
            unique_members.append(m)
    if len(unique_members) < len(all_members):
        logger.info(f"  Cross-group dedup: {len(all_members)} → {len(unique_members)} leads")
    all_members = unique_members

    logger.info(f"Total leads collected: {len(all_members)}")

    # Save full lead database to CSV and send to owner via Telegram
    LEADS_RECIPIENT = "alegtudasyda"
    try:
        with open(LEADS_CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["username", "full_name", "group_title", "group_username", "telegram_link"])
            writer.writeheader()
            for m in all_members:
                writer.writerow({
                    "username": m.get("username", ""),
                    "full_name": m.get("full_name", ""),
                    "group_title": m.get("group_title", ""),
                    "group_username": m.get("group_username", ""),
                    "telegram_link": f"https://t.me/{m['username']}" if m.get("username") else "",
                })
        logger.info(f"Leads saved to {LEADS_CSV_PATH} ({len(all_members)} rows)")
        await primary_client.send_file(
            LEADS_RECIPIENT,
            LEADS_CSV_PATH,
            caption=f"Leads export — {len(all_members)} contacts\n{now_kyiv().strftime('%Y-%m-%d %H:%M')} Kyiv",
        )
        logger.info(f"Leads CSV sent to @{LEADS_RECIPIENT}")
    except Exception as e:
        logger.warning(f"Could not save/send leads CSV: {e}")

    if not all_members:
        logger.info("No leads found this cycle.")
        return

    # ── Send DMs sequentially ─────────────────────────────────────────────────
    for recipient in all_members:

        if is_quiet_hours():
            logger.info(f"Reached quiet hours mid-cycle. Sent {sent_count} today.")
            await sleep_until_8am()
            already_sent = load_sent_usernames()

        has_username = bool(recipient.get("username"))
        parsed_by = recipient.get("parsed_by_phone")

        if has_username:
            # Any available account can send to a @username
            try:
                account = await manager.get_active_account()
            except AllAccountsExhaustedError:
                logger.info(f"Daily limit reached. Sent {sent_count}, skipped {skipped_count}.")
                await notifier.notify_daily_limit(sent_count, skipped_count)
                return
        else:
            # No username — access_hash is account-specific, must use parsing account
            account = await manager.get_account_if_available(parsed_by)
            if account is None or account["phone"] not in clients:
                skipped_count += 1
                continue

        client = clients[account["phone"]]
        try:
            success = await send_dm(
                client, recipient, csv_lock, already_sent, account["label"]
            )
            if success:
                await manager.record_send(account["phone"])
                sent_count += 1
            else:
                skipped_count += 1
        except PeerFloodError:
            logger.warning(f"PeerFlood on {account['label']} — cooling down for 1h")
            await manager.mark_flood(account["phone"])
            await notifier.notify_peer_flood(account["label"])
        except Exception as e:
            logger.error(
                f"Unexpected error sending to @{recipient.get('username', '?')}: {e}",
                exc_info=True,
            )

    logger.info(f"Daily cycle done. Sent: {sent_count}, skipped: {skipped_count}.")
    await notifier.notify_cycle_done(sent_count, skipped_count)


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

async def main():
    validate_environment()

    # Graceful shutdown on SIGTERM / SIGHUP (sent by Railway, Docker, systemd)
    loop = asyncio.get_running_loop()

    def _on_signal(sig_name: str):
        logger.info(f"{sig_name} received — shutting down cleanly.")
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda s=sig: _on_signal(s.name))

    decode_sessions_from_env()
    clients = await connect_clients()

    CYCLE_ERROR_SLEEP = 300  # 5 min cooldown after unexpected cycle crash

    while True:
        if is_quiet_hours():
            await sleep_until_8am()

        try:
            await run_daily_cycle(clients)
        except Exception as e:
            logger.error(f"Unhandled error in daily cycle: {e}", exc_info=True)
            await notifier.notify_error("daily cycle", e)
            logger.info(f"Cooling down {CYCLE_ERROR_SLEEP}s before retry...")
            await asyncio.sleep(CYCLE_ERROR_SLEEP)
            continue  # retry cycle, don't sleep until 08:00

        await sleep_until_8am()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting cleanly.")
        sys.exit(0)
