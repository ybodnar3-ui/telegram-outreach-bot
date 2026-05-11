"""
notifier.py — Sends status notifications to the bot owner via Telegram.

All key bot events (startup, errors, daily summary, PeerFlood, etc.)
are reported to NOTIFY_USERNAME so the owner can monitor without checking logs.
"""

import asyncio
import logging

from telethon.errors import FloodWaitError, FloodError

logger = logging.getLogger(__name__)

NOTIFY_USERNAME = "alegtudasyda"

_client = None


def set_client(client):
    """Register the Telethon client to use for sending notifications."""
    global _client
    _client = client


async def _send(text: str):
    if _client is None:
        logger.warning("Notifier: no client set, skipping notification")
        return
    for attempt in range(3):
        try:
            await _client.send_message(NOTIFY_USERNAME, text, parse_mode="md")
            return
        except FloodWaitError as e:
            wait = max(e.seconds, 5)
            logger.warning(f"Notifier: FloodWait {wait}s, retrying...")
            await asyncio.sleep(wait)
        except FloodError:
            logger.warning("Notifier: FloodError (too many requests), waiting 60s...")
            await asyncio.sleep(60)
        except Exception as e:
            logger.warning(f"Notifier: failed to send message: {e}")
            if attempt < 2:
                await asyncio.sleep(30)
    logger.warning("Notifier: gave up after 3 attempts")


async def notify_startup(accounts_connected: list, accounts_failed: list):
    lines = ["🟢 *Бот запущено*"]
    for label in accounts_connected:
        lines.append(f"  ✅ {label}")
    for label in accounts_failed:
        lines.append(f"  ❌ {label} — не підключився")
    await _send("\n".join(lines))


async def notify_peer_flood(account_label: str):
    await _send(
        f"🔴 *PeerFlood* на `{account_label}`\n"
        f"Telegram заблокував відправку. Акаунт у cooldown 1 год."
    )


async def notify_session_invalid(account_label: str, phone: str):
    await _send(
        f"❌ *Сесія зламана*: `{account_label}` (`{phone}`)\n"
        f"Потрібна повторна авторизація через Railway."
    )


async def notify_daily_limit(sent: int, skipped: int):
    await _send(
        f"⚠️ *Денний ліміт вичерпано*\n"
        f"Надіслано: {sent} | Пропущено: {skipped}\n"
        f"Бот відновиться о 08:00 Kyiv."
    )


async def notify_cycle_done(sent: int, skipped: int):
    if sent == 0 and skipped == 0:
        return
    await _send(
        f"📊 *Цикл завершено*\n"
        f"Надіслано: {sent} | Пропущено: {skipped}"
    )


async def notify_no_leads():
    await _send(
        "ℹ️ *Нові ліди не знайдені*\n"
        "Усі знайдені контакти вже були охоплені раніше."
    )


async def notify_error(context: str, error: str):
    short_error = str(error)[:200]
    await _send(
        f"🚨 *Критична помилка*\n"
        f"Де: `{context}`\n"
        f"Помилка: `{short_error}`"
    )
