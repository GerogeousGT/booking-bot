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
    create_booking, get_booking, get_bookable_people, get_client_meet_url, get_person,
    get_upcoming_bookings, set_booking_meet_url, set_client_meet_url, upsert_client,
    get_pending_custom, delete_pending_custom,
)
from services.availability import find_free_slots, find_nearest_free_slots, is_slot_free
from services.calendar_service import create_event, set_event_meet_url
from services.date_parser import LLMUnavailable, is_outside_work_hours, parse_user_input
# Клавиатура ссылки живёт в notifier: одни и те же кнопки в /link и в напоминании
from services.notifier import link_actions_keyboard

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


# ─────────────────────── запись, которую делает психолог ────────────────────────
#
# Одно ядро на два входа: команда /add и кнопка «Записать на другое время» на
# карточке нестандартного запроса. Смысл — чтобы любая договорённость шла через
# бота и попадала в календарь, а не жила отдельно в голове и в ручной записи.
#
# Записать можно только того, чей telegram_id у нас есть (человек писал боту):
# Bot API не умеет искать пользователя по @username и не даёт написать первым.

MAX_ADMIN_SLOTS = 6


class AdminBookState(StatesGroup):
    waiting_for_time = State()
    waiting_for_slot = State()


def _admin_slots_keyboard(slots: list[datetime], note: str = "") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=(note or "") + fmt_slot(s),
                              callback_data=f"addslot:{s.isoformat()}")]
        for s in slots
    ]
    rows.append([InlineKeyboardButton(text="↩ Другое время", callback_data="addslot:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_admin_for_time(message: Message, state: FSMContext, hint: str = "") -> None:
    await state.set_state(AdminBookState.waiting_for_time)
    await message.answer(
        (hint + "\n\n" if hint else "")
        + "На какое время записываем?\n"
        "Пиши свободно: «завтра в 15», «в субботу в 19:00», «в пятницу утром».\n"
        "Вне рабочих часов и в выходные — тоже можно, предупрежу.\n\n"
        "Отменить — /cancel"
    )


@router.message(F.text == "/add")
async def cmd_add(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    people = await get_bookable_people()
    if not people:
        await message.answer(
            "Пока некого записывать.\n\n"
            "Записать можно только того, кто хоть раз писал боту и дал согласие на "
            "обработку данных: Telegram не даёт боту найти человека по @username "
            "и не даёт написать первым."
        )
        return

    # Пометка отделяет тех, у кого уже были записи, от тех, кто только стартовал бота
    rows = [
        [InlineKeyboardButton(
            text=("👤 " if p["is_client"] else "🆕 ") + f"{p['name']} ({p['contact']})",
            callback_data=f"addcl:{p['telegram_id']}",
        )]
        for p in people
    ]
    await message.answer(
        "Кого записываем?\n👤 — уже были записи, 🆕 — стартовал бота, но не записывался",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("addcl:"))
async def handle_add_client(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer()
        return

    client_id = int(callback.data.split(":", 1)[1])
    person = await get_person(client_id)
    if not person:
        await callback.message.answer("Человек не найден в базе.")
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(
        client_id=client_id, name=person["name"], contact=person["contact"], pending_id=None
    )
    await _ask_admin_for_time(
        callback.message, state,
        hint=f"Записываем: {person['name']} ({person['contact']})",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cother:"))
async def handle_custom_other_time(callback: CallbackQuery, state: FSMContext):
    """Вход из карточки нестандартного запроса: договорились на другое время."""
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer()
        return

    pending_id = int(callback.data.split(":", 1)[1])
    pending = await get_pending_custom(pending_id)
    if not pending:
        await callback.answer("Запрос не найден — возможно уже обработан.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(
        client_id=pending["telegram_id"], name=pending["name"],
        contact=pending["contact"], pending_id=pending_id,
    )
    await _ask_admin_for_time(
        callback.message, state,
        hint=f"Записываем: {pending['name']}\nПросил(а): {pending['custom_request']}",
    )
    await callback.answer()


@router.message(AdminBookState.waiting_for_time, F.text.in_({"/cancel", "отмена", "Отмена"}))
async def handle_admin_book_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменил. Запись не создана.")


@router.message(AdminBookState.waiting_for_time)
async def handle_admin_time(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    try:
        result = parse_user_input(text, user_id=message.from_user.id)
    except LLMUnavailable as e:
        logger.error(f"LLM недоступен при админской записи: {e}")
        result = None

    if result is None:
        slots = find_nearest_free_slots(MAX_ADMIN_SLOTS)
        if not slots:
            await message.answer("Не понял время, и свободных слотов рядом нет. Напиши иначе.")
            return
        await state.set_state(AdminBookState.waiting_for_slot)
        await message.answer("Не понял время. Ближайшие свободные:",
                             reply_markup=_admin_slots_keyboard(slots))
        return

    parsed_dt = result["dt"]
    period = result["period"]

    # Точное время: психолог мог договориться на выходной или на вечер — для него
    # сетка рабочих часов не ограничение, в отличие от самозаписи клиента
    exact = parsed_dt.hour != 0 and not result["date_range"]
    if exact:
        try:
            free = is_slot_free(parsed_dt)
        except Exception as e:
            logger.error(f"Ошибка Calendar API: {e}")
            await message.answer("Не удалось проверить календарь. Попробуй ещё раз.")
            return

        if not free:
            slots = find_free_slots(parsed_dt, None)[:MAX_ADMIN_SLOTS]
            await state.set_state(AdminBookState.waiting_for_slot)
            await message.answer(
                f"{fmt_slot(parsed_dt)} — занято." +
                ("\n\nСвободное в этот день:" if slots else "\n\nВ этот день свободного нет."),
                reply_markup=_admin_slots_keyboard(slots),
            )
            return

        note = "⚠️ вне расписания: " if is_outside_work_hours(parsed_dt, period) else ""
        await state.set_state(AdminBookState.waiting_for_slot)
        await message.answer("Подтверди время:", reply_markup=_admin_slots_keyboard([parsed_dt], note))
        return

    try:
        slots = find_free_slots(parsed_dt, period)[:MAX_ADMIN_SLOTS]
    except Exception as e:
        logger.error(f"Ошибка Calendar API: {e}")
        await message.answer("Не удалось загрузить расписание. Попробуй ещё раз.")
        return

    if not slots:
        await message.answer("В это время свободного нет. Назови другое время или точный час.")
        return

    await state.set_state(AdminBookState.waiting_for_slot)
    await message.answer("Выбери слот:", reply_markup=_admin_slots_keyboard(slots))


@router.callback_query(AdminBookState.waiting_for_slot, F.data.startswith("addslot:"))
async def handle_admin_slot(callback: CallbackQuery, state: FSMContext, bot: Bot):
    value = callback.data[len("addslot:"):]
    await callback.message.edit_reply_markup(reply_markup=None)

    if value == "other":
        await _ask_admin_for_time(callback.message, state)
        await callback.answer()
        return

    slot_start = datetime.fromisoformat(value)
    data = await state.get_data()
    await state.clear()

    try:
        if not is_slot_free(slot_start):
            await callback.message.answer("Это время только что заняли. Начни заново: /add")
            await callback.answer()
            return
    except Exception as e:
        logger.warning(f"Не удалось проверить занятость перед созданием: {e}")

    await _create_booking_by_admin(
        bot=bot,
        admin_message=callback.message,
        client_id=data["client_id"],
        name=data["name"],
        contact=data["contact"],
        slot_start=slot_start,
        pending_id=data.get("pending_id"),
    )
    await callback.answer()


async def _create_booking_by_admin(bot: Bot, admin_message: Message, client_id: int,
                                   name: str, contact: str, slot_start: datetime,
                                   pending_id: int | None) -> None:
    """Ядро: создаёт запись от лица психолога и уведомляет клиента.

    Общее для /add и для карточки нестандартного запроса — чтобы договорённость
    в любом случае попала в календарь и получила напоминания."""
    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MIN)
    meet_url = await get_client_meet_url(client_id)

    try:
        event_id = create_event(name, contact, slot_start, slot_end, meet_url)
    except Exception as e:
        logger.error(f"Ошибка создания события: {e}")
        await admin_message.answer(f"Ошибка при создании события в Calendar: {e}")
        return

    now = datetime.now(MOSCOW_TZ)
    booking_id = await create_booking(
        telegram_id=client_id, name=name, contact=contact,
        slot_start=slot_start.isoformat(), slot_end=slot_end.isoformat(),
        google_event_id=event_id, meet_url=meet_url, created_at=now.isoformat(),
    )
    await upsert_client(client_id, name, contact, now.isoformat())
    if pending_id:
        await delete_pending_custom(pending_id)

    link_line = (
        f"Подключайтесь по ссылке к началу:\n{meet_url}\n\n" if meet_url
        else "Ссылку на видеовстречу пришлю сюда за 10–15 минут до начала.\n\n"
    )
    delivered = await _notify_client(
        bot, client_id,
        f"✅ Вы записаны на консультацию\n\n"
        f"Время: {fmt_slot(slot_start)}\n"
        f"{link_line}"
        f"Пришлю напоминание за сутки и за час до начала.\n"
        f"Чтобы отменить — /cancel",
    )

    status = "уведомление отправлено" if delivered else "⚠️ клиент недоступен (мог заблокировать бота)"
    await admin_message.answer(
        f"✅ Запись создана #{booking_id}\n"
        f"Клиент: {name} ({contact})\n"
        f"Время: {fmt_slot(slot_start)}\n"
        f"{status}"
        + ("" if meet_url else "\n\nСсылки у клиента ещё нет — можно приложить сразу:"),
        reply_markup=link_actions_keyboard(booking_id, bool(meet_url)),
    )


async def _notify_client(bot: Bot, client_id: int, text: str) -> bool:
    """Пишет клиенту. False — не дошло (заблокировал бота и т.п.).

    Бот не может написать первым, но клиент, который однажды стартовал бота, всё
    ещё может его заблокировать — тогда психолог должен об этом узнать, а не гадать."""
    try:
        await bot.send_message(chat_id=client_id, text=text)
        return True
    except Exception as e:
        logger.error(f"Не удалось уведомить клиента {client_id}: {e}")
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
async def handle_custom_accept(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Принять нестандартный запрос как есть — на то время, которое просил клиент."""
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer()
        return

    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending_custom(pending_id)

    if not pending:
        await callback.answer("Запрос не найден — возможно уже обработан.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(
        client_id=pending["telegram_id"], name=pending["name"],
        contact=pending["contact"], pending_id=pending_id,
    )

    # Время из запроса клиента. Раньше при неопределённом часе молча подставлялось
    # 10:00 — клиент просил «в субботу вечером», а получал подтверждение на утро.
    # Теперь спрашиваем, а не угадываем.
    try:
        slot_start = datetime.fromisoformat(pending["requested_dt"]).astimezone(MOSCOW_TZ)
    except Exception:
        await _ask_admin_for_time(
            callback.message, state,
            hint=f"Не смог определить время из запроса: «{pending['custom_request']}»",
        )
        await callback.answer()
        return

    if slot_start.hour == 0:
        await _ask_admin_for_time(
            callback.message, state,
            hint=(f"Клиент назвал день, но не час: «{pending['custom_request']}»\n"
                  f"Дата: {slot_start.strftime('%d.%m')}"),
        )
        await callback.answer()
        return

    # Раньше проверки не было — два нестандартных запроса могли лечь на один час
    try:
        if not is_slot_free(slot_start):
            slots = find_free_slots(slot_start, None)[:MAX_ADMIN_SLOTS]
            await state.set_state(AdminBookState.waiting_for_slot)
            await callback.message.answer(
                f"{fmt_slot(slot_start)} уже занято." +
                ("\n\nСвободное в этот день:" if slots else "\n\nВ этот день свободного нет."),
                reply_markup=_admin_slots_keyboard(slots),
            )
            await callback.answer()
            return
    except Exception as e:
        logger.warning(f"Не удалось проверить занятость: {e}")

    await state.clear()
    await _create_booking_by_admin(
        bot=bot, admin_message=callback.message,
        client_id=pending["telegram_id"], name=pending["name"],
        contact=pending["contact"], slot_start=slot_start, pending_id=pending_id,
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
        delivered = await _notify_client(
            bot, client_id,
            "К сожалению, запрошенное время недоступно.\n\n"
            "Георгий свяжется с вами в ближайшее время, чтобы согласовать удобный вариант.\n\n"
            "Если хотите выбрать время самостоятельно — нажмите «Записаться» в меню.",
        )
        if not delivered:
            await callback.message.answer(
                f"⚠️ Уведомление до клиента не дошло (мог заблокировать бота). "
                f"Свяжись напрямую: {pending['contact'] if pending else name}"
            )

    # Удаляем заявку в любом случае: раньше падение отправки клиенту обрывало
    # хендлер здесь, заявка зависала в базе и кнопки жались повторно
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
