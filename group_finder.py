"""
group_finder.py — Discovers Russian-language Telegram groups via keyword search.

=== RESPONSIBILITY ===
Takes a Telethon client and a list of keywords (from config.SEARCH_KEYWORDS),
calls Telegram's contact search for each keyword, and returns a deduplicated list
of group/channel entities worth parsing for leads.

=== WHAT COUNTS AS A VALID GROUP ===
A result from search is kept if ALL pass:
  1. It is a Chat or Channel (not a User or bot)
  2. It has at least config.MIN_GROUP_MEMBERS members
  3. It is public (has a username) — private groups can't be joined to parse messages
  4. Its username is NOT in config.EXCLUDED_GROUPS (groups we sell ads in)
  5. It is NOT a Ukrainian-oriented group about Melitopol (see _is_ukrainian_group)
  6. It hasn't been seen before in this run (deduped by Telegram entity ID)
  7. If config.APPROVED_GROUPS is non-empty, username must be in that whitelist

=== UKRAINIAN GROUP FILTER ===
Melitopol is under Russian occupation. Ukrainian groups about Melitopol (diaspora,
resistance communities) use the Ukrainian spelling "Мелітополь" (і not и) and
often contain Ukrainian patriotic signals. Advertising in Russian-occupation Telegram
channels is irrelevant for those communities, so we skip them automatically.

=== RATE LIMITING ===
Calls are spaced with GROUP_INTERACTION_DELAY to avoid Telegram's flood protection.
FloodWaitError is caught and respected.
"""

import asyncio
import logging
import random

from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat
from telethon.errors import FloodWaitError

from config import (
    SEARCH_KEYWORDS, MAX_GROUPS_PER_KEYWORD, MIN_GROUP_MEMBERS,
    GROUP_INTERACTION_DELAY_MIN, GROUP_INTERACTION_DELAY_MAX,
    EXCLUDED_GROUPS, APPROVED_GROUPS,
)

logger = logging.getLogger(__name__)

# Signals that a group is Ukrainian-oriented (diaspora / resistance communities).
# Ukrainian spelling of Melitopol is "Мелітополь" (і instead of и in Russian).
# These groups are not the target — their members don't advertise in Russian channels.
_UKRAINIAN_GROUP_SIGNALS = [
    "мелітопол",     # Ukrainian spelling (і = Ukrainian, и = Russian)
    "меліт",         # abbreviated Ukrainian form
    "зсу",           # ZSU — Ukrainian Armed Forces
    "тимчасово окуп", # "temporarily occupied"
    "слава укра",    # "Slava Ukraini" variations
    "украін",        # Украïна / Україна in titles
    "melitopol_ua",  # common pattern in Ukrainian community usernames
    "_ua_",
]


def _is_ukrainian_group(chat) -> bool:
    """Return True if the group appears to be Ukrainian-oriented."""
    title = (getattr(chat, "title", "") or "").lower()
    username = (getattr(chat, "username", "") or "").lower()
    combined = title + " " + username
    return any(signal in combined for signal in _UKRAINIAN_GROUP_SIGNALS)


async def find_groups(client):
    seen_ids = set()
    groups = []

    for keyword in SEARCH_KEYWORDS:
        logger.info(f"Searching: '{keyword}'")
        try:
            result = await client(SearchRequest(q=keyword, limit=MAX_GROUPS_PER_KEYWORD * 3))
            count = 0
            for chat in result.chats:
                if chat.id in seen_ids:
                    continue
                if not isinstance(chat, (Channel, Chat)):
                    continue
                username = getattr(chat, "username", None)
                if not username:
                    continue
                if username.lower() in EXCLUDED_GROUPS:
                    logger.info(f"  Skipping own group: @{username}")
                    continue
                if _is_ukrainian_group(chat):
                    logger.info(
                        f"  Skipping Ukrainian-oriented group: @{username} "
                        f"('{getattr(chat, 'title', '?')}')"
                    )
                    continue
                if APPROVED_GROUPS and username.lower() not in APPROVED_GROUPS:
                    logger.debug(f"  Not in APPROVED_GROUPS whitelist: @{username}")
                    continue
                members = getattr(chat, "participants_count", 0) or 0
                if members < MIN_GROUP_MEMBERS:
                    continue
                seen_ids.add(chat.id)
                groups.append(chat)
                count += 1
                if count >= MAX_GROUPS_PER_KEYWORD:
                    break
            logger.info(f"  → {count} groups from '{keyword}'")
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s on search '{keyword}'")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error on search '{keyword}': {e}")

        await asyncio.sleep(random.uniform(GROUP_INTERACTION_DELAY_MIN, GROUP_INTERACTION_DELAY_MAX))

    return groups
