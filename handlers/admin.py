"""
Команды для психолога: просмотр записей, подтверждение нестандартных запросов.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, date

import pytz
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import ADMIN_TELEGRAM_ID, TIMEZONE, SLOT_DURATION_MIN
from database import (
    create_booking, get_booking, get_client_meet_url, get_upcoming_bookings,
    set_booking_meet_url, set_client_meet_url, upsert_client,
    get_pending_custom, delete_pending_custom,
)
from services.calendar_service import create_event, set_event_meet_url
from services.date_parser import parse_user_input

router = Router()
logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_slot(dt: datetime) -> str:
    day = WEEKDAYS_RU[dt.weekday()]
    return f"{dt.strftime('%d.%m')} ({day}) {dt.strftime('%H:00')} МСК"


# ─────────────────────── ссылка на встречу ────────────────────────
#
# Автогенерация убрана: публичный meet.jit.si в РФ открывает комнату, но медиа не
# доходит — клиент видит пустой экран. Ссылку создаёт психолог в любом удобном
# сервисе и отправляет через бота. Она сохраняется на клиенте, поэтому запрос
# приходит только на первую запись каждого клиента, дальше подставляется сама.

_URL_RE = re.compile(r"https?://\S+")


class AdminLinkState(StatesGroup):
    waiting_for_link = State()


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)»\"'") if m else None


def link_actions_keyboard(booking_id: int, has_link: bool) -> InlineKeyboardMarkup:
    if has_link:
        rows = [
            [InlineKeyboardButton(text="📤 Прислать повторно", callback_data=f"resend:{booking_id}")],
            [InlineKeyboardButton(text="🔄 Заменить ссылку", callback_data=f"relink:{booking_id}")],
        ]
    else:
        rows = [[InlineKeyboardButton(text="📎 Отправить ссылку",
                                      callback_data=f"sendlink:{booking_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "/link")
async def cmd_link(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    bookings = await get_upcoming_bookings()
    if not bookings:
        await message.answer("Предстоящих записей нет.")
        return

    await message.answer("🔗 Ссылки на предстоящие встречи:")
    for b in bookings:
        slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
        link = b["meet_url"] or ""
        status = f"Ссылка: {link}" if link else "⚠️ Ссылки нет"
        await message.answer(
            f"#{b['id']} — {b['name']}\n{fmt_slot(slot_start)}\n{status}",
            reply_markup=link_actions_keyboard(b["id"], bool(link)),
        )


@router.callback_query(F.data.startswith("sendlink:"))
@router.callback_query(F.data.startswith("relink:"))
async def handle_link_request(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer()
        return

    action, raw_id = callback.data.split(":", 1)
    booking_id = int(raw_id)
    booking = await get_booking(booking_id)
    if not booking or booking["status"] != "confirmed":
        await callback.message.answer("Записи уже нет — возможно, её отменили.")
        await callback.answer()
        return

    await state.set_state(AdminLinkState.waiting_for_link)
    await state.update_data(booking_id=booking_id, is_replace=(action == "relink"))

    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)
    what = "новую ссылку" if action == "relink" else "ссылку"
    await callback.message.answer(
        f"Пришли {what} на встречу с {booking['name']} ({fmt_slot(slot_start)}).\n\n"
        f"Просто вставь её сообщением. Отменить — /cancel"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("resend:"))
async def handle_link_resend(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer()
        return

    booking_id = int(callback.data.split(":", 1)[1])
    booking = await get_booking(booking_id)
    if not booking or not booking["meet_url"]:
        await callback.message.answer("По этой записи ссылки пока нет.")
        await callback.answer()
        return

    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)
    sent = await _deliver_link(bot, booking["telegram_id"], slot_start, booking["meet_url"],
                               replaced=False)
    await callback.message.answer(
        f"📤 Ссылка отправлена повторно: {booking['name']}" if sent
        else f"Не удалось отправить — клиент мог заблокировать бота. Свяжись напрямую: {booking['contact']}"
    )
    await callback.answer()


@router.message(AdminLinkState.waiting_for_link, F.text.in_({"/cancel", "отмена", "Отмена"}))
async def handle_link_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменил. Ссылку можно отправить позже: /link")


@router.message(AdminLinkState.waiting_for_link)
async def handle_link_input(message: Message, state: FSMContext, bot: Bot):
    url = _extract_url(message.text or "")
    if not url:
        await message.answer(
            "Это не похоже на ссылку — нужна строка, начинающаяся с http:// или https://\n"
            "Пришли ещё раз или отмени: /cancel"
        )
        return

    data = await state.get_data()
    booking_id = data["booking_id"]
    is_replace = data.get("is_replace", False)
    await state.clear()

    booking = await get_booking(booking_id)
    if not booking:
        await message.answer("Запись пропала — ссылку отправлять некому.")
        return

    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)

    await set_booking_meet_url(booking_id, url)
    # Главное: ссылка ложится на клиента, а не на запись — следующая запись этого
    # клиента получит её автоматически, без ручного шага
    await set_client_meet_url(booking["telegram_id"], url)
    set_event_meet_url(booking["google_event_id"], url)

    sent = await _deliver_link(bot, booking["telegram_id"], slot_start, url, replaced=is_replace)
    if sent:
        await message.answer(
            f"✅ Отправлено: {booking['name']}, {fmt_slot(slot_start)}\n\n"
            f"Ссылка сохранена — на следующую запись этого клиента уйдёт автоматически."
        )
    else:
        await message.answer(
            f"Ссылку сохранил, но доставить не смог — клиент мог заблокировать бота.\n"
            f"Свяжись напрямую: {booking['contact']}"
        )


async def _deliver_link(bot: Bot, telegram_id: int, slot_start: datetime,
                        url: str, replaced: bool) -> bool:
    if replaced:
        text = (
            f"🔄 Ссылка на встречу изменилась.\n\n"
            f"Время: {fmt_slot(slot_start)}\n"
            f"Подключайтесь по новой ссылке:\n{url}\n\n"
            f"Старая больше не работает."
        )
    else:
        text = (
            f"🔗 Ссылка на видеовстречу\n\n"
            f"Время: {fmt_slot(slot_start)}\n"
            f"Подключайтесь по ссылке к началу:\n{url}"
        )
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        return True
    except Exception as e:
        logger.error(f"Не удалось доставить ссылку клиенту {telegram_id}: {e}")
        return False


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
    # Постоянная ссылка клиента, если он у нас уже был; иначе пусто — запросим позже
    meet_url = await get_client_meet_url(client_id)

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
        f"Время: {fmt_slot(slot_start)}\n"
        + (f"Ссылка: {meet_url} (сохранённая, отправлена клиенту)"
           if meet_url else "⚠️ Ссылки нет — попрошу прислать перед встречей")
    )

    link_line = (
        f"Ссылка для подключения:\n{meet_url}\n\n" if meet_url
        else "Ссылку на видеовстречу пришлю сюда за 10–15 минут до начала.\n\n"
    )
    await bot.send_message(
        chat_id=client_id,
        text=(
            f"✅ Ваша запись подтверждена!\n\n"
            f"Время: {fmt_slot(slot_start)}\n"
            f"{link_line}"
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
