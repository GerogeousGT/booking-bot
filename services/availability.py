"""
Свободные слоты: связка чистой арифметики из slot_finder с реальным календарём.

Вынесено отдельно, потому что этим пользуются два разных флоу — самозапись клиента
и запись, которую делает психолог. Раньше логика жила приватными функциями внутри
handlers/booking.py, и админский флоу пришлось бы её дублировать.

slot_finder намеренно остаётся чистым (без обращений к Google) — на нём тесты.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from config import MIN_HOURS_BEFORE, SLOTS_DAYS_AHEAD, TIMEZONE
from services.calendar_service import get_busy_slots
from services.slot_finder import (
    SLOT_DURATION, build_search_range, filter_free_slots, generate_candidate_slots,
)

MOSCOW_TZ = pytz.timezone(TIMEZONE)


def find_free_slots(parsed_dt: datetime, period, unrestricted: bool = False) -> list[datetime]:
    """Свободные слоты в конкретный день (и период дня, если указан).

    `unrestricted=True` — режим психолога: любой день недели, часы 07:00–22:00,
    без буфера в 2 часа. Клиентская самозапись остаётся в рамках расписания."""
    search_start, search_end = build_search_range(parsed_dt, period, unrestricted)
    busy = get_busy_slots(search_start, search_end + timedelta(hours=1))
    candidates = generate_candidate_slots(search_start, search_end, period, unrestricted)
    return filter_free_slots(candidates, busy)


def find_nearest_free_slots(limit: int = 5, unrestricted: bool = False) -> list[datetime]:
    """Ближайшие свободные слоты на весь горизонт записи, без привязки к дню."""
    now = datetime.now(MOSCOW_TZ)
    horizon = now + timedelta(days=SLOTS_DAYS_AHEAD)
    busy = get_busy_slots(now, horizon)
    start = now if unrestricted else now + timedelta(hours=MIN_HOURS_BEFORE)
    candidates = generate_candidate_slots(start, horizon, None, unrestricted)
    return filter_free_slots(candidates, busy)[:limit]


def is_slot_free(slot_start: datetime) -> bool:
    """Проверка перед самым созданием записи — слот могли занять, пока выбирали."""
    busy = get_busy_slots(slot_start, slot_start + SLOT_DURATION + timedelta(minutes=1))
    return bool(filter_free_slots([slot_start], busy))
