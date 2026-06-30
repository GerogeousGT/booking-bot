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

from config import TIMEZONE
from database import get_pending_reminders, mark_reminder_sent

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def format_slot(dt: datetime) -> str:
    day = WEEKDAYS_RU[dt.weekday()]
    return f"{dt.strftime('%d.%m')} ({day}) в {dt.strftime('%H:00')}"


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
        text += f"Ссылка для подключения:\n{meet_url}"
    else:
        text += "Специалист свяжется с вами."
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        await mark_reminder_sent(booking_id, "5min")
        logger.info(f"Напоминание 5min отправлено booking_id={booking_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки 5min напоминания booking_id={booking_id}: {e}")


async def reminder_loop(bot: Bot):
    """Фоновая задача — проверяет напоминания каждые 5 минут."""
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
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

        await asyncio.sleep(5 * 60)  # каждые 5 минут
