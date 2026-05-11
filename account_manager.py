import asyncio
from datetime import date, datetime, timezone

from config import MAX_MESSAGES_PER_DAY

PEER_FLOOD_COOLDOWN_SECONDS = 3600  # 1 hour cooldown per account after PeerFloodError


class AllAccountsExhaustedError(Exception):
    pass


class AccountManager:
    def __init__(self, accounts, today_sent=None):
        """
        today_sent: dict {account_label: count} — pre-loaded from sent.csv on restart.
        Without this, a server restart resets counters and the bot can exceed MAX_MESSAGES_PER_DAY.
        """
        self._accounts = accounts
        today_sent = today_sent or {}
        self._counters = {a["phone"]: today_sent.get(a["label"], 0) for a in accounts}
        self._dates = {a["phone"]: date.today() for a in accounts}
        self._flood_until = {}  # phone -> datetime after which account can retry
        self._lock = asyncio.Lock()

    async def get_active_account(self):
        async with self._lock:
            today = date.today()
            now = datetime.now(tz=timezone.utc)
            for account in self._accounts:
                phone = account["phone"]
                if self._dates[phone] != today:
                    self._counters[phone] = 0
                    self._dates[phone] = today
                if phone in self._flood_until and now < self._flood_until[phone]:
                    continue
                if self._counters[phone] < MAX_MESSAGES_PER_DAY:
                    return account
            raise AllAccountsExhaustedError("All accounts hit daily limit or PeerFlood cooldown.")

    async def record_send(self, phone):
        async with self._lock:
            self._counters[phone] += 1

    async def mark_exhausted(self, phone):
        async with self._lock:
            self._counters[phone] = MAX_MESSAGES_PER_DAY

    async def mark_flood(self, phone):
        """Put account in cooldown after PeerFloodError instead of exhausting it for the day."""
        async with self._lock:
            from datetime import timedelta
            self._flood_until[phone] = datetime.now(tz=timezone.utc) + timedelta(seconds=PEER_FLOOD_COOLDOWN_SECONDS)

    async def get_account_if_available(self, phone):
        """Return account by phone if it has quota and is not in flood cooldown, else None."""
        async with self._lock:
            today = date.today()
            now = datetime.now(tz=timezone.utc)
            account = next((a for a in self._accounts if a["phone"] == phone), None)
            if account is None:
                return None
            if self._dates[phone] != today:
                self._counters[phone] = 0
                self._dates[phone] = today
            if phone in self._flood_until and now < self._flood_until[phone]:
                return None
            if self._counters[phone] >= MAX_MESSAGES_PER_DAY:
                return None
            return account
