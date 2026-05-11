import asyncio
import csv
import logging
import random
from datetime import datetime

from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, PeerFloodError,
    InputUserDeactivatedError, UsernameInvalidError, UsernameNotOccupiedError,
    UserNotMutualContactError,
)
from telethon.tl.types import InputPeerUser

from config import (
    DELAY_MIN_SECONDS, DELAY_MAX_SECONDS,
    SENT_CSV_PATH, MESSAGE_FILE_PATH, CSV_HEADERS,
)

logger = logging.getLogger(__name__)

_message_variants: list[str] | None = None


def load_message() -> str:
    """Load message variants from file (separated by ===) and return a random one."""
    global _message_variants
    if _message_variants is None:
        with open(MESSAGE_FILE_PATH, 'r', encoding='utf-8') as f:
            raw = f.read()
        _message_variants = [v.strip() for v in raw.split("===") if v.strip()]
        if not _message_variants:
            raise ValueError("message.txt is empty or has no valid variants")
        logger.info(f"Loaded {len(_message_variants)} message variant(s)")
    return random.choice(_message_variants)


async def send_dm(client, recipient, csv_lock, already_sent, account_label):
    username = recipient.get("username") or ""
    user_id = recipient.get("user_id")
    access_hash = recipient.get("access_hash")
    text = load_message()

    if username:
        target = username
        display = f"@{username}"
        dedup_key = username.lower()
        csv_username = username
    else:
        target = InputPeerUser(user_id, access_hash)
        display = f"id:{user_id}"
        dedup_key = f"id:{user_id}"
        csv_username = f"id:{user_id}"

    try:
        await client.send_message(target, text)
    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s — retrying {display} after sleep")
        await asyncio.sleep(e.seconds)
        try:
            await client.send_message(target, text)
        except Exception as retry_err:
            logger.error(f"Retry failed for {display}: {retry_err}")
            return False
    except UserPrivacyRestrictedError:
        logger.info(f"SKIP {display}: privacy restricted")
        return False
    except UserNotMutualContactError:
        logger.info(f"SKIP {display}: requires mutual contact")
        return False
    except (UsernameInvalidError, UsernameNotOccupiedError):
        logger.info(f"SKIP {display}: username no longer valid")
        return False
    except InputUserDeactivatedError:
        logger.info(f"SKIP {display}: account deleted")
        return False
    except PeerFloodError:
        logger.warning(f"PeerFloodError on {account_label}")
        raise
    except Exception as e:
        logger.error(f"Error sending to {display}: {e}")
        return False

    row = {
        "username": csv_username,
        "full_name": recipient.get("full_name", ""),
        "group_title": recipient.get("group_title", ""),
        "group_username": recipient.get("group_username", ""),
        "sent_at": datetime.now().isoformat(timespec='seconds'),
        "sent_from_account": account_label,
    }

    async with csv_lock:
        with open(SENT_CSV_PATH, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writerow(row)

    already_sent.add(dedup_key)

    delay = random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS)
    logger.info(f"Sent to {display} — sleeping {delay:.0f}s")
    await asyncio.sleep(delay)

    return True
