"""
member_parser.py — Extracts targetable leads from a Telegram group via message history.

=== RESPONSIBILITY ===
Given a Telethon client and a group entity, scans the last 12 months of messages
and returns unique senders who are:
  (a) safe to DM (basic filters), AND
  (b) show commercial intent — their messages suggest they are a business owner,
      seller, or someone who might want to buy advertising in Telegram channels.

=== WHY MESSAGES INSTEAD OF MEMBER LIST ===
Message history gives us ACTIVE users who actually post. More importantly, it lets
us read WHAT they write — the only reliable signal for commercial intent.

=== COMMERCIAL INTENT LOGIC ===
We read every message in the scan window. For each unique sender who passes basic
filters, we check if ANY of their messages contains at least one ADVERTISER_KEYWORD.
A sender is included only if they have ≥1 commercial message.

Examples of commercial messages → INCLUDE:
  "Продам бочки, оптом и в розницу, цена 500 руб"
  "Ателье принимает заказы, пишите в ЛС"
  "Предлагаю услуги электрика, выезд по городу"

Examples of non-commercial messages → SKIP:
  "Привет всем! Хорошего дня 😊"
  "Подскажите, где купить хлеб?"
  "Кто знает расписание автобусов?"

=== FILTERS APPLIED ===
A sender is included only if ALL pass:
  1. Has a public @username — required to DM safely without prior contact
  2. Is not a bot
  3. Account is not deleted / anonymous
  4. Is not a confirmed admin of this group — admins file spam reports, high ban risk
  5. No prohibited keywords in profile (prostitution, fraud, weapons, drugs)
  6. Not already in the global sent.csv dedup set
  7. Has ≥1 message with ADVERTISER_KEYWORDS (commercial intent)

=== RETURN FORMAT ===
Returns a list of dicts:
  {
    "username": str,         # without @ symbol
    "full_name": str,        # first + last name, may be empty string
    "group_title": str,      # display name of the source group
    "group_username": str,   # @username of the source group
  }
"""

import logging
from datetime import datetime, timedelta, timezone

from telethon.tl.types import User, ChannelParticipantsAdmins

logger = logging.getLogger(__name__)

_SCAN_DAYS = 365
_MAX_MESSAGES = 10000

_PROHIBITED_KEYWORDS = [
    "эскорт", "интим", "досуг", "проститут", "шлюх",
    "взлом", "пробив", "кардинг", "мошенн",
    "оружие", "ствол", "пушка",
    "наркот", "закладк", "кристалл", "меф", "амфетамин",
    "героин", "кокаин", "марихуан", "гашиш", "спайс",
]

# Keywords in message TEXT that signal commercial intent.
# A sender is targeted only if at least one of their messages contains one of these.
# Goal: find people who sell/advertise things or run small businesses.
_ADVERTISER_KEYWORDS = [
    # Selling
    "продам", "продаю", "продается", "продаётся", "продаем", "продаём",
    "реализую", "реализуем",
    # Buying (someone who buys for resale / business)
    "куплю", "закупаю", "закупаем",
    # Services offered
    "услуга", "услуги", "предлагаю", "предлагаем", "оказываем", "оказываю",
    "выполним", "выполняем", "изготовим", "изготавливаем",
    "принимаем заказ", "принимаю заказ",
    # Pricing signals — direct commerce indicator
    "цена", "цены", "прайс", "прайслист", "стоимость",
    "скидка", "акция", "распродажа", "спецпредложение",
    "оптом", "в розницу", "розница",
    # Delivery/logistics
    "доставка", "доставляем", "самовывоз",
    "в наличии", "под заказ",
    # Business identity keywords IN messages
    "магазин", "компания", "фирма", "ип", "ООО",
    "мастер", "ателье", "студия", "мастерская", "салон",
    "кафе", "ресторан", "кофейня", "пекарня",
    "автосервис", "автомастер", "шиномонтаж",
    "строительство", "ремонт квартир", "ремонт помещений",
    # Advertising interest — the most direct signal
    "реклам",  # covers: реклама, рекламу, рекламировать, рекламный
    "продвижение", "продвигать",
    "объявлени",  # объявление, объявления
    # CTAs typical of sellers
    "пишите в лс", "пишите в личку", "в лс", "в личку",
    "обращайтесь", "звоните", "пишите нам",
    "заказывайте", "заказать можно",
]


def _is_prohibited(user: User) -> bool:
    text = " ".join(filter(None, [
        user.username or "",
        user.first_name or "",
        user.last_name or "",
    ])).lower()
    return any(kw in text for kw in _PROHIBITED_KEYWORDS)


def _has_commercial_intent(text: str) -> bool:
    """Return True if the message text contains at least one advertiser keyword."""
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in _ADVERTISER_KEYWORDS)


async def _get_admin_ids(client, group) -> set:
    try:
        admins = await client.get_participants(group, filter=ChannelParticipantsAdmins())
        return {a.id for a in admins}
    except Exception as e:
        logger.debug(f"Could not fetch admins for '{getattr(group, 'title', '?')}': {e}")
        return set()


async def parse_members(client, group, already_sent):
    seen_ids: set = set()
    pending: dict = {}        # sender_id → member dict (passed basic filters)
    is_commercial: dict = {}  # sender_id → bool (has ≥1 commercial message)

    group_title = getattr(group, "title", str(group.id))
    group_username = getattr(group, "username", "")

    admin_ids = await _get_admin_ids(client, group)
    if admin_ids:
        logger.debug(f"  {len(admin_ids)} admins will be skipped in '{group_title}'")

    cutoff_date = datetime.now(tz=timezone.utc) - timedelta(days=_SCAN_DAYS)

    try:
        async for message in client.iter_messages(group, limit=_MAX_MESSAGES):
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < cutoff_date:
                break

            sender = message.sender
            if not isinstance(sender, User):
                continue

            if sender.id in seen_ids:
                # Already assessed this sender's basic filters.
                # If they passed, check if this extra message adds commercial signal.
                if sender.id in pending and not is_commercial.get(sender.id):
                    is_commercial[sender.id] = _has_commercial_intent(message.text or "")
                continue

            # ── First time we see this sender ──
            seen_ids.add(sender.id)

            if sender.id in admin_ids:
                continue
            if sender.bot:
                continue
            if sender.deleted:
                continue
            if not sender.username:
                continue
            dedup_key = sender.username.lower()
            if dedup_key in already_sent:
                continue
            if _is_prohibited(sender):
                logger.debug(f"  SKIP @{sender.username}: prohibited keyword in profile")
                continue

            first = sender.first_name or ""
            last = sender.last_name or ""
            pending[sender.id] = {
                "username": sender.username or "",
                "user_id": sender.id,
                "access_hash": sender.access_hash,
                "full_name": (first + " " + last).strip(),
                "group_title": group_title,
                "group_username": group_username,
            }
            is_commercial[sender.id] = _has_commercial_intent(message.text or "")

    except Exception as e:
        logger.error(f"Error parsing '{group_title}': {e}", exc_info=True)

    # Include only senders who demonstrated commercial intent in at least one message.
    members = [
        pending[sid] for sid in pending
        if is_commercial.get(sid, False)
    ]

    total_passed_basic = len(pending)
    logger.info(
        f"  '{group_title}': {total_passed_basic} passed basic filters, "
        f"{len(members)} have commercial intent"
    )
    return members
