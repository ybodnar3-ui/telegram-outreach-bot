"""
export_leads.py — One-time script to export all discovered leads to CSV.
Run locally: python3 export_leads.py
Output: leads_export.csv (open with Excel)
"""

import asyncio
import csv
import logging
import os
import sys
from datetime import datetime

from telethon import TelegramClient

from config import ACCOUNTS, SESSIONS_DIR, PRIORITY_GROUPS, SEARCH_KEYWORDS
from group_finder import find_groups
from member_parser import parse_members

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

OUTPUT_FILE = "leads_export.csv"
FIELDNAMES = ["username", "full_name", "group_title", "group_username", "telegram_link"]


async def main():
    if not ACCOUNTS:
        print("ERROR: no accounts configured.")
        sys.exit(1)

    account = ACCOUNTS[0]
    session_path = os.path.join(SESSIONS_DIR, account["phone"].lstrip("+"))
    client = TelegramClient(session_path, account["api_id"], account["api_hash"])

    print(f"\nConnecting as {account['label']}...")
    await client.start(phone=account["phone"])
    me = await client.get_me()
    print(f"Connected: {me.first_name} @{me.username}\n")

    # Load priority groups
    print("Loading priority groups...")
    priority_groups = []
    priority_ids = set()
    for username in PRIORITY_GROUPS:
        try:
            entity = await client.get_entity(username)
            priority_groups.append(entity)
            priority_ids.add(entity.id)
            print(f"  ✓ @{username}")
        except Exception as e:
            print(f"  ✗ @{username}: {e}")

    # Discover groups via keyword search
    print("\nSearching for groups...")
    discovered = await find_groups(client)
    groups = priority_groups + [g for g in discovered if g.id not in priority_ids]
    print(f"Total groups to parse: {len(groups)}\n")

    # Parse all groups
    already_sent = set()  # export everything, ignore dedup
    all_leads = []
    seen_usernames = set()

    for i, group in enumerate(groups, 1):
        title = getattr(group, "title", str(group.id))
        print(f"[{i}/{len(groups)}] Parsing '{title}'...")
        try:
            members = await parse_members(client, group, already_sent)
            new = 0
            for m in members:
                key = m["username"].lower()
                if key not in seen_usernames:
                    seen_usernames.add(key)
                    all_leads.append(m)
                    new += 1
            print(f"  → {new} new leads (total so far: {len(all_leads)})")
        except Exception as e:
            print(f"  ✗ Error: {e}")

    # Write CSV
    print(f"\nWriting {len(all_leads)} leads to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for lead in all_leads:
            writer.writerow({
                "username": lead.get("username", ""),
                "full_name": lead.get("full_name", ""),
                "group_title": lead.get("group_title", ""),
                "group_username": lead.get("group_username", ""),
                "telegram_link": f"https://t.me/{lead['username']}" if lead.get("username") else "",
            })

    await client.disconnect()

    print(f"\n✓ Done! {len(all_leads)} leads saved to: {OUTPUT_FILE}")
    print(f"  Open with Excel or Google Sheets.\n")


if __name__ == "__main__":
    asyncio.run(main())
