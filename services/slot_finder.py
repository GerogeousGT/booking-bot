"""
Генерация и фильтрация свободных слотов.
Слоты — строго на начало часа, 55 минут, Пн–Пт 9:00–17:00 МСК.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
import pytz

from config import WORK_HOUR_START, WORK_HOUR_END, MIN_HOURS_BEFORE, SLOT_DURATION_MIN, TIMEZONE

MOSCOW_TZ = pytz.timezone(TIMEZONE)
SLOT_DURATION = timedelta(minutes=SLOT_DURATION_MIN)


# Границы админского режима: психолог может поставить консультацию когда угодно,
# но предлагать слоты в 4 утра бессмысленно — это опечатка, а не намерение.
ADMIN_HOUR_START = 7
ADMIN_HOUR_END = 23  # последний слот 22:00


def generate_candidate_slots(
    from_dt: datetime,
    to_dt: datetime,
    period: Optional[tuple[int, int]] = None,
    unrestricted: bool = False,
) -> list[datetime]:
    """
    Возвращает кандидатов слотов (начало часа).

    По умолчанию — только будни и рабочие часы: это самозапись клиента.
    `unrestricted=True` — режим психолога: любой день недели и расширенные часы
    07:00–22:00. Он сам решает, когда работать, и договаривается голосом о
    времени, которого в обычной сетке нет.
    """
    slots: list[datetime] = []

    hour_start = ADMIN_HOUR_START if unrestricted else WORK_HOUR_START
    hour_end = ADMIN_HOUR_END if unrestricted else WORK_HOUR_END

    # округляем from_dt вверх до начала следующего часа
    current = from_dt.replace(minute=0, second=0, microsecond=0)
    if current < from_dt:
        current += timedelta(hours=1)

    while current <= to_dt:
        if unrestricted or current.weekday() < 5:  # Пн–Пт только для клиента
            hour = current.hour
            # последний слот — за час до конца (17:00 у клиента, 22:00 у психолога)
            if hour_start <= hour <= hour_end - 1:
                if period is None or (period[0] <= hour <= period[1]):
                    slots.append(current)
        current += timedelta(hours=1)

    return slots


def filter_free_slots(
    candidates: list[datetime],
    busy_intervals: list[tuple[datetime, datetime]],
) -> list[datetime]:
    """Убирает слоты, пересекающиеся с занятыми интервалами."""
    free: list[datetime] = []
    for slot in candidates:
        slot_end = slot + SLOT_DURATION
        occupied = any(
            slot < b_end and slot_end > b_start
            for b_start, b_end in busy_intervals
        )
        if not occupied:
            free.append(slot)
    return free


def build_search_range(
    parsed_dt: datetime,
    period: Optional[tuple[int, int]],
    unrestricted: bool = False,
) -> tuple[datetime, datetime]:
    """
    Определяет диапазон поиска слотов по дате и периоду.
    Возвращает (range_start, range_end) с учётом минимального буфера.

    `unrestricted` — режим психолога: день целиком 07:00–22:00 и без буфера
    «не раньше чем через 2 часа». Договорился на 20:00, когда уже 19:00, — можно.
    """
    now = datetime.now(MOSCOW_TZ)
    min_start = now if unrestricted else now + timedelta(hours=MIN_HOURS_BEFORE)

    hour_start = ADMIN_HOUR_START if unrestricted else WORK_HOUR_START
    hour_end = ADMIN_HOUR_END if unrestricted else WORK_HOUR_END

    if period:
        # ищем в конкретный день в рамках периода
        start = parsed_dt.replace(hour=period[0], minute=0, second=0, microsecond=0)
        end = parsed_dt.replace(hour=min(period[1], hour_end - 1), minute=59, second=59, microsecond=0)
    else:
        # весь день целиком
        start = parsed_dt.replace(hour=hour_start, minute=0, second=0, microsecond=0)
        end = parsed_dt.replace(hour=hour_end - 1, minute=59, second=59, microsecond=0)

    if start < min_start:
        start = min_start

    # Если минимальный буфер сдвинул start за конец диапазона — слотов нет
    if start > end:
        return start, start  # пустой диапазон, generate_candidate_slots вернёт []

    return start, end


def find_days_with_hour(
    hour: int,
    week_start: datetime,
    week_end: datetime,
    busy_intervals: list[tuple[datetime, datetime]],
) -> list[datetime]:
    """
    Ищет рабочие дни в диапазоне где слот на заданный час свободен.
    Возвращает список datetime (начало слота) для каждого подходящего дня.
    """
    import pytz
    from config import TIMEZONE, WORK_HOUR_START, WORK_HOUR_END
    MOSCOW_TZ = pytz.timezone(TIMEZONE)

    now = datetime.now(MOSCOW_TZ)
    result = []
    current = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    while current <= week_end:
        if current.weekday() < 5 and WORK_HOUR_START <= hour <= WORK_HOUR_END - 1:
            slot = current.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot > now + timedelta(hours=2):
                slot_end = slot + SLOT_DURATION
                occupied = any(
                    slot < b_end and slot_end > b_start
                    for b_start, b_end in busy_intervals
                )
                if not occupied:
                    result.append(slot)
        current += timedelta(days=1)

    return result
