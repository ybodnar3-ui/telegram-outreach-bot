"""
scout.py — Interactive group scout: analyze discovered groups before sending.

=== WHAT THIS DOES ===
1. Finds all groups matching SEARCH_KEYWORDS (same logic as main.py).
2. For each group shows:
   - Member count and recent activity (unique active posters in last 30 days)
   - How many profiles look like businesses/entrepreneurs
   - Up to 5 sample lead names so you can judge the audience
3. Asks [y / n / q] to approve, skip, or quit early.
4. Saves the result to approved_groups.json.
5. Prints the APPROVED_GROUPS snippet to paste into config.py.

=== AFTER RUNNING ===
Copy the printed set into config.py → APPROVED_GROUPS.
When APPROVED_GROUPS is non-empty, main.py targets ONLY those groups.
Leave APPROVED_GROUPS empty (default) to target all discovered groups.

=== SPEED ===
Scans only the last 200 messages per group — this is a preview.
Full sending runs use up to 3 000 messages for deeper lead extraction.
"""

import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.tl.types import User

from config import (
    ACCOUNTS, SESSIONS_DIR,
    GROUP_INTERACTION_DELAY_MIN, GROUP_INTERACTION_DELAY_MAX,
)
from group_finder import find_groups

logging.basicConfig(
    level=logging.WARNING,  # suppress info spam — we print our own formatted output
    format="%(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_SCOUT_MSG_LIMIT = 200
_SCOUT_WINDOW_DAYS = 30

# Keywords that suggest a member profile belongs to a business / entrepreneur.
# Matches against username + first_name + last_name (lowercase, partial match).
_BUSINESS_KEYWORDS = [
    "магазин", "услуги", "строй", "ремонт", "мастер", "студия", "студио",
    "кафе", "ресторан", "салон", "продажа", "торг", "бизнес", "предприн",
    "реклам", "доставк", "оптов", "розниц", "склад", "логист", "агент",
    "дизайн", "фото", "видео", "клинин", "уборк", "сервис", "техник",
    "авто", "шиномонт", "запчаст", "ателье", "пекарн", "кондитер",
]


def _looks_like_business(user: User) -> bool:
    text = " ".join(filter(None, [
        user.username or "",
        user.first_name or "",
        user.last_name or "",
    ])).lower()
    return any(kw in text for kw in _BUSINESS_KEYWORDS)


async def _analyze_group(client, group) -> dict:
    """Scan last _SCOUT_MSG_LIMIT messages; return activity stats and sample leads."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_SCOUT_WINDOW_DAYS)
    seen_ids: set = set()
    active_count = 0
    business_count = 0
    samples: list = []

    try:
        async for message in client.iter_messages(group, limit=_SCOUT_MSG_LIMIT):
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < cutoff:
                break

            sender = message.sender
            if not isinstance(sender, User):
                continue
            if sender.id in seen_ids:
                continue
            seen_ids.add(sender.id)

            if sender.bot or sender.deleted or not sender.username:
                continue

            active_count += 1
            if _looks_like_business(sender):
                business_count += 1
            if len(samples) < 5:
                first = sender.first_name or ""
                last = sender.last_name or ""
                samples.append({
                    "username": sender.username,
                    "name": (first + " " + last).strip() or "—",
                })
    except Exception as e:
        logger.warning(f"Error scanning '{getattr(group, 'title', '?')}': {e}")

    return {
        "active_30d": active_count,
        "business_count": business_count,
        "samples": samples,
    }


def _hr():
    print("─" * 68)


def _print_group_card(i: int, total: int, group, analysis: dict):
    title = getattr(group, "title", "?")
    username = getattr(group, "username", "?")
    members = getattr(group, "participants_count", 0) or 0

    _hr()
    print(f"  [{i}/{total}]  {title}  (@{username})")
    print(f"  Учасників всього:          {members:,}")
    print(f"  Активних за 30 днів:       {analysis['active_30d']}")
    print(f"  Схожих на підприємців:     {analysis['business_count']}")

    if analysis["samples"]:
        print("  Приклади лідів:")
        for s in analysis["samples"]:
            print(f"    @{s['username']}  {s['name']}")
    else:
        print("  (немає активних лідів в останніх 200 повідомленнях)")


async def main():
    if not ACCOUNTS:
        print("ERROR: жоден акаунт не налаштований у config.py.")
        sys.exit(1)

    account = ACCOUNTS[0]
    session_path = os.path.join(SESSIONS_DIR, account["phone"].lstrip("+"))
    client = TelegramClient(session_path, account["api_id"], account["api_hash"])

    print("\n" + "=" * 68)
    print("  TELEGRAM GROUP SCOUT")
    print("=" * 68)
    print("Підключаємось до Telegram...\n")

    await client.start(phone=account["phone"])

    print("Шукаємо групи за ключовими словами...")
    groups = await find_groups(client)

    if not groups:
        print("Групи не знайдені — перевір SEARCH_KEYWORDS у config.py.")
        await client.disconnect()
        return

    print(f"Знайдено {len(groups)} груп. Аналізуємо активність...\n")

    approved: list[str] = []
    rejected: list[str] = []

    for i, group in enumerate(groups, 1):
        username = (getattr(group, "username", None) or str(group.id)).lower()

        try:
            analysis = await _analyze_group(client, group)
        except Exception as e:
            logger.warning(f"Could not analyze @{username}: {e}")
            analysis = {"active_30d": 0, "business_count": 0, "samples": []}

        _print_group_card(i, len(groups), group, analysis)

        while True:
            raw = input("\n  Додати цю групу до таргету? [y/n/q (quit)]: ").strip().lower()
            if raw in ("y", "n", "q"):
                break
            print("  Введи y (так), n (ні), або q (зупинитись)")

        if raw == "y":
            approved.append(username)
            print("  ✓ Додано")
        elif raw == "q":
            print("\nЗупинено. Зберігаємо результат...")
            break
        else:
            rejected.append(username)
            print("  — Пропущено")

        await asyncio.sleep(random.uniform(
            GROUP_INTERACTION_DELAY_MIN, GROUP_INTERACTION_DELAY_MAX
        ))

    _hr()

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "approved": approved,
        "rejected": rejected,
    }
    with open("approved_groups.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nЗбережено → approved_groups.json")
    print(f"Схвалено: {len(approved)},  Відхилено: {len(rejected)}\n")

    if approved:
        print("─" * 68)
        print("Встав це у config.py у поле APPROVED_GROUPS:\n")
        items = ",\n    ".join(f'"{g}"' for g in approved)
        print(f"APPROVED_GROUPS = {{\n    {items}\n}}\n")
        print("─" * 68)
        print("Після цього запускай main.py — він пише тільки в схвалені групи.\n")
    else:
        print("Жодну групу не схвалено.")
        print("APPROVED_GROUPS залишається порожнім → main.py пише в усі знайдені групи.\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
