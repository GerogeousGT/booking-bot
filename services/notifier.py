"""
Фоновый цикл напоминаний. Проверяет каждые 5 минут записи,
которым нужно отправить уведомление за 24 ч или за 1 ч до сессии.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytz
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    ADMIN_LINK_PROMPT_MIN, ADMIN_LINK_RETRY_MIN, ADMIN_TELEGRAM_ID, TIMEZONE,
)
from database import (
    get_bookings_needing_link, get_pending_reminders, mark_link_prompt_sent,
    mark_reminder_sent,
)

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)

# Чтобы падающий внешний сервис не превратился в поток одинаковых сообщений админу
ADMIN_ALERT_COOLDOWN_SEC = 30 * 60
_last_admin_alert: dict[str, float] = {}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def format_slot(dt: datetime) -> str:
    day = WEEKDAYS_RU[dt.weekday()]
    return f"{dt.strftime('%d.%m')} ({day}) в {dt.strftime('%H:00')}"


async def notify_admin_error(bot: Bot, kind: str, text: str) -> None:
    """Сообщает админу о поломке внешнего сервиса.

    Заведено после 2026-08-17: провайдер снял модель, бот три дня отвечал клиентам
    «не понял запрос», и узнали об этом только от клиента. Молчаливых отказов в
    воронке записи быть не должно. `kind` — ключ троттлинга, чтобы одна и та же
    поломка не спамила каждым сообщением клиента."""
    if not ADMIN_TELEGRAM_ID:
        return
    import time as _time
    now = _time.monotonic()
    if now - _last_admin_alert.get(kind, 0) < ADMIN_ALERT_COOLDOWN_SEC:
        return
    _last_admin_alert[kind] = now
    try:
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=f"⚠️ Booking bot\n\n{text}")
    except Exception as e:
        logger.error(f"Не удалось уведомить админа ({kind}): {e}")


async def _send_reminder(bot: Bot, telegram_id: int, booking_id: int, slot_start: datetime, when: str):
    label = "Завтра" if when == "24h" else "Через час"
    text = (
        f"⏰ Напоминание о консультации\n\n"
        f"{label} — {format_slot(slot_start)} МСК\n\n"
        f"Если нужно отменить или перенести — напишите /cancel"
    )
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        await mark_reminder_sent(booking_id, when)
        logger.info(f"Напоминание {when} отправлено booking_id={booking_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания booking_id={booking_id}: {e}")


async def _send_5min_reminder(bot: Bot, telegram_id: int, booking_id: int, slot_start: datetime, meet_url: str):
    text = (
        f"🔔 Через 5 минут ваша консультация!\n\n"
        f"Время: {format_slot(slot_start)} МСК\n\n"
    )
    if meet_url:
        # Дублируем ссылку, даже если клиент уже получал её при записи — чтобы не
        # искать её в переписке недельной давности за минуту до начала
        text += f"Ссылка для подключения:\n{meet_url}"
    else:
        text += "Ссылку на подключение пришлю сюда с минуты на минуту."
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        await mark_reminder_sent(booking_id, "5min")
        logger.info(f"Напоминание 5min отправлено booking_id={booking_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки 5min напоминания booking_id={booking_id}: {e}")


def link_prompt_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Отправить ссылку клиенту",
                              callback_data=f"sendlink:{booking_id}")],
    ])


async def _prompt_admin_for_link(bot: Bot, booking, minutes_left: int, urgent: bool) -> None:
    """Просит психолога прислать ссылку на встречу.

    Ссылка больше не генерится автоматически — психолог создаёт звонок сам и
    отправляет через бота. Отправленная ссылка сохраняется на клиенте, так что
    этот запрос приходит только на первую запись каждого клиента."""
    if not ADMIN_TELEGRAM_ID:
        return
    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)
    head = "‼️ Ссылки всё ещё нет" if urgent else "🔗 Нужна ссылка на встречу"
    try:
        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                f"{head}\n\n"
                f"Через {minutes_left} мин — {booking['name']}, {format_slot(slot_start)} МСК\n"
                f"Запись #{booking['id']}\n\n"
                f"Создай звонок и пришли ссылку — я передам клиенту и запомню её "
                f"для следующих записей."
            ),
            reply_markup=link_prompt_keyboard(booking["id"]),
        )
    except Exception as e:
        logger.error(f"Не удалось запросить ссылку по booking_id={booking['id']}: {e}")
        return
    await mark_link_prompt_sent(booking["id"], "retry" if urgent else "prompt")


async def _check_missing_links(bot: Bot) -> None:
    now = datetime.now(MOSCOW_TZ)
    for b in await get_bookings_needing_link():
        slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
        minutes_left = (slot_start - now).total_seconds() / 60
        if minutes_left <= 0:
            continue
        if not b["admin_link_retry_sent"] and minutes_left <= ADMIN_LINK_RETRY_MIN:
            await _prompt_admin_for_link(bot, b, ADMIN_LINK_RETRY_MIN, urgent=True)
        elif not b["admin_link_prompt_sent"] and minutes_left <= ADMIN_LINK_PROMPT_MIN:
            await _prompt_admin_for_link(bot, b, ADMIN_LINK_PROMPT_MIN, urgent=False)


async def reminder_loop(bot: Bot):
    """Фоновая задача — проверяет напоминания раз в минуту.

    Раньше цикл был раз в 5 минут; с ним окно «пнуть психолога за 3 минуты до
    начала» можно было целиком проскочить между итерациями. Проверки — это
    только чтение из SQLite, минутный интервал ничего не стоит."""
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            await _check_missing_links(bot)
            bookings = await get_pending_reminders()

            for b in bookings:
                slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
                delta = slot_start - now

                # Напоминание за 24 часа (окно: от 25ч до 23ч до начала)
                if not b["reminder_24h_sent"] and timedelta(hours=23) <= delta <= timedelta(hours=25):
                    await _send_reminder(bot, b["telegram_id"], b["id"], slot_start, "24h")

                # Напоминание за 1 час (окно: от 80 до 50 минут до начала)
                if not b["reminder_1h_sent"] and timedelta(minutes=50) <= delta <= timedelta(minutes=80):
                    await _send_reminder(bot, b["telegram_id"], b["id"], slot_start, "1h")

                # Напоминание за 5 минут (окно: от 7 до 3 минут до начала)
                if not b["reminder_5min_sent"] and timedelta(minutes=3) <= delta <= timedelta(minutes=7):
                    await _send_5min_reminder(bot, b["telegram_id"], b["id"], slot_start, b["meet_url"])

        except Exception as e:
            logger.error(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(60)
