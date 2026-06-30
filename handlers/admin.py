"""
Команды для психолога: просмотр записей, подтверждение нестандартных запросов.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, date

import pytz
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message

from config import ADMIN_TELEGRAM_ID, TIMEZONE, SLOT_DURATION_MIN
from database import (
    create_booking, get_upcoming_bookings, upsert_client,
    get_pending_custom, delete_pending_custom,
)
from services.calendar_service import create_event
from services.date_parser import parse_user_input

router = Router()
MOSCOW_TZ = pytz.timezone(TIMEZONE)
MOSCOW_TZ = pytz.timezone(TIMEZONE)
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_slot(dt: datetime) -> str:
    day = WEEKDAYS_RU[dt.weekday()]
    return f"{dt.strftime('%d.%m')} ({day}) {dt.strftime('%H:00')} МСК"


# ─────────────────────── /bookings ────────────────────────

@router.message(F.text == "/bookings")
async def cmd_bookings(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    bookings = await get_upcoming_bookings()
    if not bookings:
        await message.answer("Предстоящих записей нет.")
        return

    lines = ["📋 Предстоящие записи:\n"]
    for b in bookings:
        slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
        lines.append(f"• {fmt_slot(slot_start)} — {b['name']} ({b['contact']}) #{b['id']}")

    await message.answer("\n".join(lines))


# ─────────────────────── нестандартное время ────────────────────────

@router.callback_query(F.data.startswith("ca:"))
async def handle_custom_accept(callback: CallbackQuery, bot: Bot):
    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending_custom(pending_id)

    if not pending:
        await callback.answer("Запрос не найден — возможно уже обработан.", show_alert=True)
        return

    client_id = pending["telegram_id"]
    name = pending["name"]
    contact = pending["contact"]
    requested_dt_iso = pending["requested_dt"]

    await callback.message.edit_reply_markup(reply_markup=None)

    # Определяем время записи из запроса клиента
    try:
        slot_start = datetime.fromisoformat(requested_dt_iso).astimezone(MOSCOW_TZ)
        # Если время не указано (час = 0) — ставим 10:00 как дефолт
        if slot_start.hour == 0:
            slot_start = slot_start.replace(hour=10, minute=0)
    except Exception:
        await callback.message.answer(
            f"Не смог определить время из запроса клиента.\n"
            f"Запрос: {pending['custom_request']}\n\n"
            f"Свяжитесь с клиентом напрямую: {contact}"
        )
        await delete_pending_custom(pending_id)
        await callback.answer()
        return

    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MIN)
    meet_url = f"https://meet.jit.si/georg-psych-{uuid.uuid4().hex[:10]}"

    try:
        event_id = create_event(name, contact, slot_start, slot_end, meet_url)
    except Exception as e:
        await callback.message.answer(f"Ошибка создания события в Calendar: {e}")
        await callback.answer()
        return

    now = datetime.now(MOSCOW_TZ)
    booking_id = await create_booking(
        telegram_id=client_id,
        name=name,
        contact=contact,
        slot_start=slot_start.isoformat(),
        slot_end=slot_end.isoformat(),
        google_event_id=event_id,
        meet_url=meet_url,
        created_at=now.isoformat(),
    )
    await upsert_client(client_id, name, contact, now.isoformat())
    await delete_pending_custom(pending_id)

    await callback.message.answer(
        f"✅ Запись создана #{booking_id}\n"
        f"Клиент: {name} ({contact})\n"
        f"Время: {fmt_slot(slot_start)}"
    )

    await bot.send_message(
        chat_id=client_id,
        text=(
            f"✅ Ваша запись подтверждена!\n\n"
            f"Время: {fmt_slot(slot_start)}\n"
            f"Ссылка для подключения: {meet_url}\n\n"
            f"Пришлю напоминание за час и за 5 минут до начала.\n"
            f"Чтобы отменить — /cancel"
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cd:"))
async def handle_custom_decline(callback: CallbackQuery, bot: Bot):
    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending_custom(pending_id)

    client_id = int(pending["telegram_id"]) if pending else None
    name = pending["name"] if pending else "Клиент"

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Запрос отклонён. Свяжитесь с клиентом напрямую.")

    if client_id:
        await bot.send_message(
            chat_id=client_id,
            text=(
                f"К сожалению, запрошенное время недоступно.\n\n"
                f"Георгий свяжется с вами в ближайшее время, чтобы согласовать удобный вариант.\n\n"
                f"Если хотите выбрать время самостоятельно — нажмите «Записаться» в меню."
            ),
        )

    if pending:
        await delete_pending_custom(pending_id)

    await callback.answer()


# ─────────────────────── умный хендлер для психолога ────────────────────────

_BOOKINGS_RE = re.compile(
    r"запис|расписан|клиент|сессия|сеанс|что.{0,10}(сегодня|завтра|неделе|есть)|"
    r"у меня|свободн|занят|покажи|список|schedule",
    re.IGNORECASE,
)


def _format_bookings(bookings, title: str) -> str:
    if not bookings:
        return f"{title}\n\nЗаписей нет."
    lines = [title + "\n"]
    for b in bookings:
        slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
        day = ["пн","вт","ср","чт","пт","сб","вс"][slot_start.weekday()]
        lines.append(
            f"• {slot_start.strftime('%d.%m')} ({day}) {slot_start.strftime('%H:00')} МСК\n"
            f"  {b['name']} · {b['contact']}"
        )
    return "\n".join(lines)


@router.message(F.func(lambda m: m.from_user.id == ADMIN_TELEGRAM_ID and bool(_BOOKINGS_RE.search(m.text or ""))))
async def admin_smart_bookings(message: Message):
    text = message.text.strip()
    now = datetime.now(MOSCOW_TZ)
    bookings = await get_upcoming_bookings()

    # Пытаемся понять временной контекст
    result = parse_user_input(text, user_id=0)

    if result and result["dt"]:
        target_dt = result["dt"]
        date_range = result.get("date_range")

        if date_range in ("current_week", "next_week"):
            offset = 0 if date_range == "current_week" else 1
            monday = now - timedelta(days=now.weekday())
            monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
            monday += timedelta(weeks=offset)
            friday = monday + timedelta(days=4, hours=23, minutes=59)
            filtered = [b for b in bookings
                        if monday <= datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ) <= friday]
            label = "эта неделя" if offset == 0 else "следующая неделя"
            await message.answer(_format_bookings(filtered, f"📋 Записи ({label}):"))

        else:
            # Конкретный день
            target_date = target_dt.date()
            filtered = [b for b in bookings
                        if datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ).date() == target_date]
            months = ["января","февраля","марта","апреля","мая","июня",
                      "июля","августа","сентября","октября","ноября","декабря"]
            day_label = f"{target_date.day} {months[target_date.month-1]}"
            await message.answer(_format_bookings(filtered, f"📋 Записи на {day_label}:"))

    else:
        # Нет конкретной даты — показываем все ближайшие
        await message.answer(_format_bookings(bookings[:10], "📋 Все предстоящие записи:"))
