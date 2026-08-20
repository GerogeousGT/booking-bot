"""
FSM-флоу записи на консультацию.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytz
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from config import ADMIN_TELEGRAM_ID, MIN_HOURS_BEFORE, SLOTS_DAYS_AHEAD, TIMEZONE
from database import create_booking, get_booking, get_user_bookings, cancel_booking, get_client, get_client_meet_url, upsert_client, save_consent, delete_client, has_consent, create_pending_custom
from services.calendar_service import create_event, delete_event, get_busy_slots
from services.date_parser import LLMUnavailable, is_outside_work_hours, parse_user_input
from services.availability import find_free_slots, find_nearest_free_slots, is_slot_free
from services.notifier import notify_admin_error
from services.slot_finder import (
    SLOT_DURATION, build_search_range, filter_free_slots, generate_candidate_slots,
    find_days_with_hour,
)

router = Router()
logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
MAX_SLOTS_SHOWN = 5

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Записаться на консультацию")],
        [KeyboardButton(text="📋 Мои записи")],
    ],
    resize_keyboard=True,
)


class BookingState(StatesGroup):
    waiting_for_time = State()
    waiting_for_period = State()
    waiting_for_slot_choice = State()
    waiting_for_known_client_confirm = State()
    waiting_for_name = State()
    waiting_for_contact = State()
    waiting_for_confirm = State()


# ─────────────────────── helpers ────────────────────────

def fmt_slot(dt: datetime) -> str:
    day = WEEKDAYS_RU[dt.weekday()]
    return f"{dt.strftime('%d.%m')} ({day}) в {dt.strftime('%H:00')} МСК"


PERIOD_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🌅 До 13:00", callback_data="period:morning"),
        InlineKeyboardButton(text="☀️ После 13:00", callback_data="period:afternoon"),
    ],
    [InlineKeyboardButton(text="↩ Другое время", callback_data="period:other")],
])

PERIOD_RANGES = {
    "morning":   (9, 12),
    "afternoon": (13, 17),
}


def slots_keyboard(slots: list[datetime]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=fmt_slot(s), callback_data=f"slot:{s.isoformat()}")]
        for s in slots
    ]
    rows.append([InlineKeyboardButton(text="↩ Другое время", callback_data="slot:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _find_slots(parsed_dt: datetime, period) -> list[datetime]:
    return find_free_slots(parsed_dt, period)


async def _find_nearest_slots(limit: int = MAX_SLOTS_SHOWN) -> list[datetime]:
    return find_nearest_free_slots(limit)


# Запасной выход, когда разобрать текст не получилось: не выкидываем клиента из
# сценария, а даём нажать кнопку. До 2026-08-17 тут был state.clear() — человек
# после неудачной формулировки просто оказывался в начале.
NEAREST_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📅 Показать ближайшие свободные слоты", callback_data="nearest")],
])


async def _show_nearest_slots(message: Message, state: FSMContext, intro: str) -> None:
    try:
        slots = await _find_nearest_slots()
    except Exception as e:
        logger.error(f"Ошибка Calendar API: {e}")
        await message.answer("Не удалось загрузить расписание. Напишите напрямую: @SlammG")
        return

    if not slots:
        await message.answer(
            f"На ближайшие {SLOTS_DAYS_AHEAD} дней свободных слотов нет.\n"
            "Напишите напрямую: @SlammG"
        )
        return

    await message.answer(intro, reply_markup=slots_keyboard(slots))
    await state.update_data(is_custom=False)
    await state.set_state(BookingState.waiting_for_slot_choice)


# ─────────────────────── /start ────────────────────────

CONSENT_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📄 Открыть политику конфиденциальности", callback_data="consent_screen:policy")],
    [InlineKeyboardButton(text="✅ Согласен на обработку данных", callback_data="consent_screen:agree")],
])


@router.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if await has_consent(message.from_user.id):
        await message.answer(
            "Привет! Я помогу записаться на консультацию.",
            reply_markup=MAIN_KB,
        )
        return

    await message.answer(
        "Привет! Я помогу записаться на консультацию к психологу Тайгильдину Георгию Андреевичу.\n\n"
        "Продолжая использование бота и нажимая кнопку «Согласен», вы даёте согласие на обработку "
        "ваших персональных данных в соответствии с Политикой конфиденциальности.",
        reply_markup=CONSENT_KB,
    )


@router.callback_query(F.data.startswith("consent_screen:"))
async def handle_consent_screen(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "policy":
        await callback.message.answer(PRIVACY_TEXT)
        await callback.answer()
        return

    # agree
    now = datetime.now(MOSCOW_TZ)
    await save_consent(callback.from_user.id, now.isoformat())
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Спасибо! Согласие зафиксировано.\n\nТеперь вы можете записаться на консультацию.",
        reply_markup=MAIN_KB,
    )
    await callback.answer()


# ─────────────────────── запуск флоу ────────────────────────

@router.message(F.text == "📅 Записаться на консультацию")
async def start_booking(message: Message, state: FSMContext):
    if not await has_consent(message.from_user.id):
        await message.answer(
            "Сначала необходимо ваше согласие на обработку персональных данных.",
            reply_markup=CONSENT_KB,
        )
        return

    await state.set_state(BookingState.waiting_for_time)
    await message.answer(
        "Когда вам удобно?\n\n"
        "Напишите в свободной форме:\n"
        "• «в понедельник после обеда»\n"
        "• «завтра в 10»\n"
        "• «в пятницу утром»\n"
        "• «3 июля в 15:00»",
        reply_markup=ReplyKeyboardRemove(),
    )


# ─────────────────────── ввод времени ────────────────────────

@router.message(BookingState.waiting_for_time)
async def handle_time_input(message: Message, state: FSMContext):
    text = message.text.strip()
    now = datetime.now(MOSCOW_TZ)

    # Перехватываем намерения про записи и отмену — не отдаём в Groq
    if _MYBOOKINGS_RE.search(text) or _re.search(r"я\s+записан|есть\s+запись|мои\s+запис|посмотреть\s+запис|активн", text, _re.IGNORECASE):
        await state.clear()
        await cmd_my_bookings(message)
        return
    if _CANCEL_RE.search(text):
        await state.clear()
        await cmd_cancel(message)
        return

    try:
        result = parse_user_input(text, user_id=message.from_user.id)
    except LLMUnavailable as e:
        logger.error(f"LLM недоступен на вводе {text!r}: {e}")
        await notify_admin_error(
            message.bot,
            kind="llm_down",
            text=(
                "Разбор свободного текста не работает — LLM-провайдер недоступен.\n\n"
                f"Ошибка: {e}\n\n"
                "Клиентам сейчас показываются ближайшие слоты кнопками, запись работает. "
                "Проверь LLM_PROVIDER / ключ."
            ),
        )
        await _show_nearest_slots(
            message, state,
            "Не удалось разобрать время автоматически. Вот ближайшие свободные слоты — "
            "выберите подходящий:",
        )
        return

    if result is None:
        # Состояние НЕ сбрасываем: клиент остаётся в сценарии и может переформулировать
        await message.answer(
            "Не понял запрос. Напишите иначе:\n"
            "• «в понедельник после обеда»\n"
            "• «завтра в 10»\n"
            "• «на следующей неделе в 14:00»\n"
            "• «15 июля в 14:00»",
            reply_markup=NEAREST_KB,
        )
        return

    parsed_dt = result["dt"]
    period     = result["period"]
    date_range = result["date_range"]
    target_hour = result["target_hour"]

    # ── Поиск по диапазону недели ────────────────────────────────
    if date_range:
        from datetime import timedelta as _td
        week_start = parsed_dt
        week_end   = week_start + _td(days=4, hours=23, minutes=59)
        range_label = "следующей" if "next" in date_range else "этой"

        if target_hour is None:
            await message.answer(
                "Уточните время — в какой час ищем? Например: «на следующей неделе в 14:00»"
            )
            return

        try:
            busy = get_busy_slots(week_start, week_end)
        except Exception as e:
            logger.error(f"Ошибка Calendar API: {e}")
            await message.answer("Не удалось загрузить расписание. Попробуйте позже.")
            return

        days = find_days_with_hour(target_hour, week_start, week_end, busy)

        if not days:
            await message.answer(
                f"На {range_label} неделе {target_hour}:00 везде занято.\n"
                "Попробуйте другое время или напишите напрямую: @SlammG"
            )
            return

        days_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{d.day} {MONTHS_RU[d.month-1]} ({WEEKDAYS_RU[d.weekday()]}) в {target_hour}:00",
                callback_data=f"slot:{d.isoformat()}",
            )]
            for d in days
        ] + [[InlineKeyboardButton(text="↩ Другое время", callback_data="slot:other")]])

        await message.answer(
            f"На {range_label} неделе {target_hour}:00 свободно:",
            reply_markup=days_kb,
        )
        await state.update_data(is_custom=False)
        await state.set_state(BookingState.waiting_for_slot_choice)
        return

    # Нестандартное время — передаём психологу
    if is_outside_work_hours(parsed_dt, period):
        await state.update_data(custom_request=text, is_custom=True)
        await state.set_state(BookingState.waiting_for_name)
        await message.answer(
            "Это время вне обычного расписания (пн–пт, 9:00–18:00).\n\n"
            "Передам ваш запрос — специалист свяжется и согласует удобное время.\n\n"
            "Как вас зовут?"
        )
        return

    # Ищем слоты
    try:
        free_slots = await _find_slots(parsed_dt, period)
    except Exception as e:
        logger.error(f"Ошибка Calendar API: {e}")
        await message.answer(
            "Не удалось загрузить расписание. Попробуйте позже или напишите: @SlammG"
        )
        return

    day_label = f"{parsed_dt.day} {MONTHS_RU[parsed_dt.month - 1]}"
    weekday_label = WEEKDAYS_RU[parsed_dt.weekday()]
    has_specific_time = parsed_dt.hour != 0

    # Если указан день без времени и без периода — проверяем обе половины и спрашиваем
    if not has_specific_time and period is None:
        try:
            morning_slots = await _find_slots(parsed_dt, PERIOD_RANGES["morning"])
            afternoon_slots = await _find_slots(parsed_dt, PERIOD_RANGES["afternoon"])
        except Exception as e:
            logger.error(f"Ошибка Calendar API: {e}")
            await message.answer("Не удалось загрузить расписание. Попробуйте позже или напишите: @SlammG")
            return

        has_morning = bool(morning_slots)
        has_afternoon = bool(afternoon_slots)

        if not has_morning and not has_afternoon:
            await message.answer(
                f"На {day_label} ({weekday_label}) свободных слотов нет.\n"
                "Выберите другой день:"
            )
            return

        # Если есть только одна половина — сразу показываем без вопроса
        if has_morning and not has_afternoon:
            await message.answer(
                f"После обеда в {day_label} занято. Свободные слоты до обеда:",
                reply_markup=slots_keyboard(morning_slots),
            )
            await state.update_data(is_custom=False)
            await state.set_state(BookingState.waiting_for_slot_choice)
            return

        if has_afternoon and not has_morning:
            await message.answer(
                f"До обеда в {day_label} занято. Свободные слоты после обеда:",
                reply_markup=slots_keyboard(afternoon_slots),
            )
            await state.update_data(is_custom=False)
            await state.set_state(BookingState.waiting_for_slot_choice)
            return

        # Обе половины свободны — задаём вопрос
        await state.update_data(parsed_dt=parsed_dt.isoformat(), is_custom=False)
        await state.set_state(BookingState.waiting_for_period)
        await message.answer(
            f"{day_label} ({weekday_label}) — вам удобнее до обеда или после?",
            reply_markup=PERIOD_KB,
        )
        return

    # Ищем слоты
    try:
        free_slots = await _find_slots(parsed_dt, period)
    except Exception as e:
        logger.error(f"Ошибка Calendar API: {e}")
        await message.answer(
            "Не удалось загрузить расписание. Попробуйте позже или напишите: @SlammG"
        )
        return

    if not free_slots:
        # Если был указан конкретный период — сообщаем что именно в нём нет мест
        if period:
            period_labels = {(9,11): "утром", (13,16): "после обеда", (16,17): "вечером",
                             (11,15): "днём", (12,13): "в обед", (9,12): "до обеда", (13,17): "после обеда"}
            pl = period_labels.get(period, "в это время")
            await message.answer(
                f"На {day_label} {pl} свободных мест нет.\n\n"
                f"Выбрать другое время на {day_label}?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📅 Да, показать весь день", callback_data=f"show_day:{parsed_dt.date().isoformat()}"),
                    InlineKeyboardButton(text="↩ Другой день", callback_data="slot:other"),
                ]]),
            )
            await state.update_data(is_custom=False)
            await state.set_state(BookingState.waiting_for_slot_choice)
            return

        try:
            full_end = now + timedelta(days=SLOTS_DAYS_AHEAD)
            busy_full = get_busy_slots(now, full_end)
            all_candidates = generate_candidate_slots(
                now + timedelta(hours=MIN_HOURS_BEFORE), full_end
            )
            free_slots = filter_free_slots(all_candidates, busy_full)[:MAX_SLOTS_SHOWN]
        except Exception:
            free_slots = []

        if not free_slots:
            await message.answer(
                f"На ближайшие {SLOTS_DAYS_AHEAD} дней свободных слотов нет.\n"
                "Напишите напрямую: @SlammG"
            )
            return

        await message.answer(
            f"На {day_label} свободных слотов нет. Вот ближайшие доступные:",
            reply_markup=slots_keyboard(free_slots),
        )
    else:
        period_labels = {(9,11): " утром", (13,16): " после обеда", (16,17): " вечером", (11,15): " днём", (12,13): " в обед", (9,12): " до обеда", (13,17): " после обеда"}
        period_label = period_labels.get(period, "") if period else ""
        shown = free_slots[:MAX_SLOTS_SHOWN] if has_specific_time else free_slots
        await message.answer(
            f"Свободные слоты на {day_label}{period_label}:",
            reply_markup=slots_keyboard(shown),
        )

    await state.update_data(is_custom=False)
    await state.set_state(BookingState.waiting_for_slot_choice)


# ─────────────────────── ближайшие слоты (после неудачного разбора) ───────

@router.callback_query(F.data == "nearest")
async def handle_nearest(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await _show_nearest_slots(
        callback.message, state, "Ближайшие свободные слоты:"
    )
    await callback.answer()


# ─────────────────────── показать весь день (после "нет слотов в период") ───────

@router.callback_query(BookingState.waiting_for_slot_choice, F.data.startswith("show_day:"))
async def handle_show_day(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data[len("show_day:"):]
    await callback.message.edit_reply_markup(reply_markup=None)
    d = date.fromisoformat(date_str)
    parsed_dt = MOSCOW_TZ.localize(datetime(d.year, d.month, d.day))
    try:
        free_slots = await _find_slots(parsed_dt, None)
    except Exception:
        free_slots = []
    day_label = f"{d.day} {MONTHS_RU[d.month - 1]}"
    if not free_slots:
        await callback.message.answer(f"На {day_label} совсем нет свободных слотов. Выберите другой день:")
    else:
        await callback.message.answer(
            f"Все свободные слоты на {day_label}:",
            reply_markup=slots_keyboard(free_slots),
        )
    await callback.answer()


# ─────────────────────── выбор периода (до/после обеда) ────────────────────────

@router.callback_query(BookingState.waiting_for_period, F.data.startswith("period:"))
async def handle_period_choice(callback: CallbackQuery, state: FSMContext):
    value = callback.data[len("period:"):]

    if value == "other":
        await state.set_state(BookingState.waiting_for_time)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Напишите другое удобное время:")
        await callback.answer()
        return

    data = await state.get_data()
    parsed_dt = datetime.fromisoformat(data["parsed_dt"]).astimezone(MOSCOW_TZ)
    period = PERIOD_RANGES[value]
    now = datetime.now(MOSCOW_TZ)

    try:
        free_slots = await _find_slots(parsed_dt, period)
    except Exception as e:
        logger.error(f"Ошибка Calendar API: {e}")
        await callback.message.answer("Не удалось загрузить расписание. Попробуйте позже.")
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)

    day_label = f"{parsed_dt.day} {MONTHS_RU[parsed_dt.month - 1]}"
    period_label = "до обеда" if value == "morning" else "после обеда"

    if not free_slots:
        await callback.message.answer(
            f"На {day_label} {period_label} свободных слотов нет.\n"
            "Выберите другое время:",
            reply_markup=PERIOD_KB,
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"Свободные слоты на {day_label} {period_label}:",
        reply_markup=slots_keyboard(free_slots),
    )
    await state.update_data(is_custom=False)
    await state.set_state(BookingState.waiting_for_slot_choice)
    await callback.answer()


# ─────────────────────── выбор слота ────────────────────────

@router.message(BookingState.waiting_for_slot_choice)
async def handle_slot_text_input(message: Message, state: FSMContext):
    """Если клиент написал текст вместо нажатия кнопки — переходим к новому запросу времени."""
    await state.set_state(BookingState.waiting_for_time)
    await handle_time_input(message, state)


@router.callback_query(BookingState.waiting_for_slot_choice, F.data.startswith("slot:"))
async def handle_slot_choice(callback: CallbackQuery, state: FSMContext):
    value = callback.data[len("slot:"):]

    if value == "other":
        await state.set_state(BookingState.waiting_for_time)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Напишите другое удобное время:")
        await callback.answer()
        return

    slot_start = datetime.fromisoformat(value)
    slot_end = slot_start + SLOT_DURATION
    await state.update_data(slot_start=slot_start.isoformat(), slot_end=slot_end.isoformat())

    await callback.message.edit_reply_markup(reply_markup=None)

    # Проверяем — знаем ли уже этого клиента
    client = await get_client(callback.from_user.id)

    if client and client["name"]:
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Всё верно", callback_data="known_client:yes"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="known_client:no"),
        ]])
        await callback.message.answer(
            f"Выбрано: {fmt_slot(slot_start)}\n\n"
            f"Записываем на:\n"
            f"Имя: {client['name']}\n"
            f"Контакт: {client['contact']}\n\n"
            f"Всё верно?",
            reply_markup=confirm_kb,
        )
        await state.update_data(
            slot_start=slot_start.isoformat(),
            slot_end=slot_end.isoformat(),
            name=client["name"],
            contact=client["contact"],
        )
        await state.set_state(BookingState.waiting_for_known_client_confirm)
    else:
        await callback.message.answer(f"Выбрано: {fmt_slot(slot_start)}\n\nКак вас зовут?")
        await state.set_state(BookingState.waiting_for_name)

    await callback.answer()


# ─────────────────────── известный клиент ────────────────────────

@router.callback_query(BookingState.waiting_for_known_client_confirm, F.data.startswith("known_client:"))
async def handle_known_client(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":")[1]
    await callback.message.edit_reply_markup(reply_markup=None)

    if choice == "yes":
        # Данные уже в state — переходим сразу к подтверждению
        data = await state.get_data()
        slot_start = datetime.fromisoformat(data["slot_start"])
        await state.update_data(is_custom=False)
        await state.set_state(BookingState.waiting_for_confirm)
        await callback.message.answer(
            f"Проверьте данные:\n\n"
            f"Имя: {data['name']}\n"
            f"Контакт: {data['contact']}\n"
            f"Время: {fmt_slot(slot_start)}\n\n"
            f"Всё верно?",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
    else:
        # Сбрасываем имя/контакт — собираем заново
        await state.update_data(name=None, contact=None)
        await state.set_state(BookingState.waiting_for_name)
        await callback.message.answer("Как вас зовут?")

    await callback.answer()


# ─────────────────────── имя ────────────────────────

@router.message(BookingState.waiting_for_name)
async def handle_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Введите имя (минимум 2 символа):")
        return
    await state.update_data(name=name)

    # Если у клиента есть username — предлагаем его использовать
    username = message.from_user.username
    if username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, @{username}", callback_data=f"use_tg:{username}"),
            InlineKeyboardButton(text="📝 Указать другой", callback_data="use_tg:other"),
        ]])
        await message.answer(
            f"Свяжемся с вами через @{username}?",
            reply_markup=kb,
        )
    else:
        await state.set_state(BookingState.waiting_for_contact)
        await message.answer("Укажите ваш Telegram @username или другой способ связи:")


@router.callback_query(BookingState.waiting_for_name, F.data.startswith("use_tg:"))
async def handle_use_tg(callback: CallbackQuery, state: FSMContext):
    value = callback.data[len("use_tg:"):]
    await callback.message.edit_reply_markup(reply_markup=None)

    if value != "other":
        await state.update_data(contact=f"@{value}")
        await state.set_state(BookingState.waiting_for_contact)
        # Сразу переходим к подтверждению — имитируем ввод контакта
        await _show_confirm(callback.message, state)
    else:
        await state.set_state(BookingState.waiting_for_contact)
        await callback.message.answer("Укажите Telegram @username или другой способ связи:")

    await callback.answer()


async def _show_confirm(message, state: FSMContext):
    """Показывает экран подтверждения записи."""
    data = await state.get_data()
    contact = data.get("contact", "")
    is_custom = data.get("is_custom", True)
    confirm_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    if is_custom:
        await message.answer(
            f"Проверьте данные:\n\nИмя: {data['name']}\nКонтакт: {contact}\nЗапрос: {data.get('custom_request', '')}\n\nОтправить запрос?",
            reply_markup=confirm_kb,
        )
    else:
        slot_start = datetime.fromisoformat(data["slot_start"])
        await message.answer(
            f"Проверьте данные:\n\nИмя: {data['name']}\nКонтакт: {contact}\nВремя: {fmt_slot(slot_start)}\n\nВсё верно?",
            reply_markup=confirm_kb,
        )
    await state.set_state(BookingState.waiting_for_confirm)


# ─────────────────────── контакт ────────────────────────

@router.message(BookingState.waiting_for_contact)
async def handle_contact(message: Message, state: FSMContext):
    contact = message.text.strip()
    if len(contact) < 2:
        await message.answer("Введите @username или другой способ связи:")
        return
    # Добавляем @ если написали username без него
    if not contact.startswith("@") and not contact.startswith("+") and not contact.startswith("http"):
        contact = f"@{contact}"

    data = await state.get_data()
    await state.update_data(contact=contact)
    await _show_confirm(message, state)


# ─────────────────────── подтверждение ────────────────────────

@router.message(BookingState.waiting_for_confirm, F.text == "✅ Подтвердить")
async def handle_confirm(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()

    name = data["name"]
    contact = data["contact"]
    is_custom = data.get("is_custom", True)

    await message.answer("Оформляю...", reply_markup=ReplyKeyboardRemove())

    if is_custom:
        custom_request = data.get("custom_request", "")
        parsed_dt_iso = data.get("parsed_dt", "")

        pending_id = await create_pending_custom(
            telegram_id=message.from_user.id,
            name=name,
            contact=contact,
            requested_dt=parsed_dt_iso,
            custom_request=custom_request,
        )

        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять как есть", callback_data=f"ca:{pending_id}")],
            # Договорились голосом на другое время — проводим запись через бота,
            # чтобы она попала в календарь и получила напоминания со ссылкой
            [InlineKeyboardButton(text="📝 Записать на другое время", callback_data=f"cother:{pending_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"cd:{pending_id}")],
        ])
        tg_link = f"@{message.from_user.username}" if message.from_user.username else f"id:{message.from_user.id}"
        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                f"⚡ Запрос на нестандартное время\n\n"
                f"Имя: {name}\n"
                f"Контакт: {contact}\n"
                f"Просит: {custom_request}\n"
                f"TG: {tg_link}"
            ),
            reply_markup=admin_kb,
        )
        await message.answer(
            "Запрос отправлен! Ожидайте подтверждения — обычно это занимает несколько минут.",
            reply_markup=MAIN_KB,
        )
        return

    # стандартное время
    slot_start = datetime.fromisoformat(data["slot_start"])
    slot_end = datetime.fromisoformat(data["slot_end"])

    # финальная проверка на race condition
    try:
        if not is_slot_free(slot_start):
            await message.answer(
                "Этот слот только что заняли. Выберите другое время:"
            )
            await start_booking(message, state)
            return
    except Exception as e:
        logger.warning(f"Не удалось проверить занятость перед созданием: {e}")

    # Ссылка больше не генерится: постоянная ссылка клиента подставляется, если он
    # уже был; для нового клиента остаётся пустой — психолог пришлёт её через бота
    # перед встречей, и она сохранится на клиенте для следующих записей.
    meet_url = await get_client_meet_url(message.from_user.id)

    try:
        tg_username = message.from_user.username or ""
        event_id = create_event(name, contact, slot_start, slot_end, meet_url, tg_username)
    except Exception as e:
        logger.error(f"Ошибка создания события: {e}")
        await message.answer(
            "Ошибка при записи. Попробуйте позже или напишите: @SlammG"
        )
        return

    now = datetime.now(MOSCOW_TZ)
    await upsert_client(message.from_user.id, name, contact, now.isoformat())
    booking_id = await create_booking(
        telegram_id=message.from_user.id,
        name=name,
        contact=contact,
        slot_start=slot_start.isoformat(),
        slot_end=slot_end.isoformat(),
        google_event_id=event_id,
        meet_url=meet_url,
        created_at=now.isoformat(),
    )

    tg_link = f"@{message.from_user.username}" if message.from_user.username else f"id:{message.from_user.id}"
    await bot.send_message(
        chat_id=ADMIN_TELEGRAM_ID,
        text=(
            f"✅ Новая запись #{booking_id}\n\n"
            f"Имя: {name}\n"
            f"Контакт: {contact}\n"
            f"TG: {tg_link}\n"
            f"Время: {fmt_slot(slot_start)}\n"
            + (f"Ссылка: {meet_url} (сохранённая, отправлена клиенту)"
               if meet_url else "⚠️ Ссылки нет — попрошу прислать перед встречей")
        ),
    )

    if meet_url:
        link_line = f"Подключайтесь по ссылке к началу:\n{meet_url}\n\n"
    else:
        link_line = "Ссылку на видеовстречу пришлю сюда за 10–15 минут до начала.\n\n"

    await message.answer(
        f"Готово! Вы записаны на {fmt_slot(slot_start)}.\n\n"
        f"{link_line}"
        f"Пришлю напоминание за сутки и за час до начала.\n"
        f"Чтобы отменить запись — /cancel",
        reply_markup=MAIN_KB,
    )


@router.message(BookingState.waiting_for_confirm, F.text == "❌ Отмена")
async def handle_confirm_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Запись отменена.", reply_markup=MAIN_KB)


# ─────────────────────── /cancel ────────────────────────

import re as _re
_CANCEL_RE = _re.compile(r"отмен|перенес|перенос|cancel", _re.IGNORECASE)
_MYBOOKINGS_RE = _re.compile(r"мои запис|моя запись|мое расписание|моё расписание", _re.IGNORECASE)
_TIME_INTENT_RE = _re.compile(r"во сколько|когда можно|есть ли время|свободн|запишит|хочу записат|можно записат|есть окно|есть слот|когда.{0,20}записат|записат.{0,20}когда", _re.IGNORECASE)


@router.message(F.text == "📋 Мои записи")
@router.message(F.func(lambda m: bool(_MYBOOKINGS_RE.search(m.text or ""))))
async def cmd_my_bookings(message: Message):
    bookings = await get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас нет предстоящих записей.", reply_markup=MAIN_KB)
        return
    lines = ["📋 Ваши предстоящие записи:\n"]
    for b in bookings:
        slot_start = datetime.fromisoformat(b["slot_start"]).astimezone(MOSCOW_TZ)
        lines.append(f"• {fmt_slot(slot_start)}")
        if b["meet_url"]:
            lines.append(f"  Ссылка: {b['meet_url']}")
    await message.answer("\n".join(lines), reply_markup=MAIN_KB)


@router.message(F.text == "/cancel")
@router.message(F.func(lambda m: bool(_CANCEL_RE.search(m.text or ""))))
async def cmd_cancel(message: Message):
    bookings = await get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас нет активных записей.")
        return

    rows = [
        [InlineKeyboardButton(
            text=fmt_slot(datetime.fromisoformat(b['slot_start']).astimezone(MOSCOW_TZ)),
            callback_data=f"cancel_ask:{b['id']}",
        )]
        for b in bookings
    ]
    await message.answer("Выберите запись:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cancel_ask:"))
async def handle_cancel_ask(callback: CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    booking = await get_booking(booking_id)
    if not booking or booking["telegram_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Перенести на другое время", callback_data=f"reschedule:{booking_id}")],
        [InlineKeyboardButton(text="❌ Отменить совсем", callback_data=f"cancel_booking:{booking_id}")],
        [InlineKeyboardButton(text="↩ Назад", callback_data="cancel_back")],
    ])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Запись на {fmt_slot(slot_start)}\n\nЧто хотите сделать?",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_back")
async def handle_cancel_back(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.callback_query(F.data.startswith("reschedule:"))
async def handle_reschedule(callback: CallbackQuery, state: FSMContext):
    booking_id = int(callback.data.split(":")[1])
    booking = await get_booking(booking_id)
    if not booking:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Отменяем старую запись
    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)
    if booking["google_event_id"]:
        delete_event(booking["google_event_id"])
    await cancel_booking(booking_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Запись на {fmt_slot(slot_start)} отменена.\n\n"
        f"На какое время перенести? Напишите удобное время:",
    )
    # Запускаем флоу выбора нового времени
    await state.update_data(name=booking["name"], contact=booking["contact"], is_custom=False)
    await state.set_state(BookingState.waiting_for_time)
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_booking:"))
async def handle_cancel_booking(callback: CallbackQuery, bot: Bot):
    booking_id = int(callback.data.split(":")[1])
    booking = await get_booking(booking_id)

    if not booking or booking["telegram_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    slot_start = datetime.fromisoformat(booking["slot_start"]).astimezone(MOSCOW_TZ)

    if booking["google_event_id"]:
        delete_event(booking["google_event_id"])

    await cancel_booking(booking_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Запись на {fmt_slot(slot_start)} отменена.", reply_markup=MAIN_KB)

    await bot.send_message(
        chat_id=ADMIN_TELEGRAM_ID,
        text=f"❌ Отмена записи #{booking_id}\n\nКлиент: {booking['name']}\nВремя: {fmt_slot(slot_start)}",
    )
    await callback.answer()


# ─────────────────────── /privacy ────────────────────────

PRIVACY_TEXT = """🔒 Политика конфиденциальности

Бот используется для записи на консультацию к психологу Тайгильдину Георгию Андреевичу.

Мы обрабатываем только имя, username в Telegram, дату и время записи.

Данные используются только для оформления записи и отправки напоминаний о консультации за 1 час и за 5 минут до начала.

Данные хранятся в базе данных SQLite на сервере и не используются в рекламных целях, не передаются третьим лицам, кроме случаев, предусмотренных законом.

Вы можете в любой момент удалить свои данные командой /deletedata.

Контакт по вопросам персональных данных: @slammg, gorgeousgt@yandex.ru"""


@router.message(F.text == "/privacy")
async def cmd_privacy(message: Message):
    await message.answer(PRIVACY_TEXT)


# ─────────────────────── /deletedata ────────────────────────

@router.message(F.text == "/deletedata")
async def cmd_deletedata(message: Message):
    client = await get_client(message.from_user.id)
    consent = await has_consent(message.from_user.id)
    if not client and not consent:
        await message.answer("У нас нет ваших данных.")
        return

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data="deletedata:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="deletedata:no"),
    ]])
    await message.answer(
        "Удалить ваши персональные данные?\n\n"
        "Имя и контакт будут стёрты. Активные записи отменены не будут, "
        "но ваши данные в них заменятся на «[удалено]».",
        reply_markup=confirm_kb,
    )


@router.callback_query(F.data.startswith("deletedata:"))
async def handle_deletedata(callback: CallbackQuery):
    choice = callback.data.split(":")[1]
    await callback.message.edit_reply_markup(reply_markup=None)

    if choice == "no":
        await callback.message.answer("Отменено, данные сохранены.")
        await callback.answer()
        return

    await delete_client(callback.from_user.id)
    await callback.message.answer(
        "Ваши данные удалены.\n\n"
        "Если захотите записаться снова — просто нажмите «Записаться».",
        reply_markup=MAIN_KB,
    )
    await callback.answer()


# ─────────────────────── catch-all (должен быть последним) ────────────────────────

@router.message()
async def handle_unknown(message: Message, state: FSMContext):
    """Любой текст вне FSM — пробуем как запрос времени через Groq."""
    current = await state.get_state()
    if current is not None:
        return
    text = message.text or ""
    if text.startswith("/"):
        return
    if not await has_consent(message.from_user.id):
        await message.answer(
            "Сначала необходимо ваше согласие на обработку персональных данных.",
            reply_markup=CONSENT_KB,
        )
        return
    await state.set_state(BookingState.waiting_for_time)
    await handle_time_input(message, state)
