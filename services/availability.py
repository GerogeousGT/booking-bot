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


def find_free_slots(parsed_dt: datetime, period) -> list[datetime]:
    """Свободные слоты в конкретный день (и период дня, если указан)."""
    search_start, search_end = build_search_range(parsed_dt, period)
    busy = get_busy_slots(search_start, search_end + timedelta(hours=1))
    candidates = generate_candidate_slots(search_start, search_end, period)
    return filter_free_slots(candidates, busy)


def find_nearest_free_slots(limit: int = 5) -> list[datetime]:
    """Ближайшие свободные слоты на весь горизонт записи, без привязки к дню."""
    now = datetime.now(MOSCOW_TZ)
    horizon = now + timedelta(days=SLOTS_DAYS_AHEAD)
    busy = get_busy_slots(now, horizon)
    candidates = generate_candidate_slots(now + timedelta(hours=MIN_HOURS_BEFORE), horizon)
    return filter_free_slots(candidates, busy)[:limit]


def is_slot_free(slot_start: datetime) -> bool:
    """Проверка перед самым созданием записи — слот могли занять, пока выбирали."""
    busy = get_busy_slots(slot_start, slot_start + SLOT_DURATION + timedelta(minutes=1))
    return bool(filter_free_slots([slot_start], busy))
