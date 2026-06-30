"""
Тесты генерации и фильтрации слотов.
"""
import pytest
from datetime import datetime, timedelta
import pytz

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def make_dt(year, month, day, hour=0) -> datetime:
    return MOSCOW_TZ.localize(datetime(year, month, day, hour))


# ─── generate_candidate_slots ───────────────────────────────

def test_slots_only_on_workdays():
    """Суббота и воскресенье не попадают в слоты."""
    from services.slot_finder import generate_candidate_slots
    # 2026-07-04 суббота, 2026-07-05 воскресенье, 2026-07-06 понедельник
    start = make_dt(2026, 7, 4, 9)
    end = make_dt(2026, 7, 6, 17)
    slots = generate_candidate_slots(start, end)
    for s in slots:
        assert s.weekday() < 5, f"Выходной в слотах: {s}"


def test_slots_within_work_hours():
    """Все слоты в диапазоне 9–17."""
    from services.slot_finder import generate_candidate_slots
    start = make_dt(2026, 7, 6, 8)  # понедельник
    end = make_dt(2026, 7, 6, 20)
    slots = generate_candidate_slots(start, end)
    for s in slots:
        assert 9 <= s.hour <= 17, f"Слот вне рабочих часов: {s}"


def test_slots_on_hour_boundary():
    """Слоты строго на начало часа."""
    from services.slot_finder import generate_candidate_slots
    start = make_dt(2026, 7, 6, 9)
    end = make_dt(2026, 7, 6, 17)
    slots = generate_candidate_slots(start, end)
    for s in slots:
        assert s.minute == 0 and s.second == 0


def test_period_filter():
    """Фильтр по периоду дня работает."""
    from services.slot_finder import generate_candidate_slots
    start = make_dt(2026, 7, 6, 9)
    end = make_dt(2026, 7, 6, 17)
    slots = generate_candidate_slots(start, end, period=(13, 16))
    for s in slots:
        assert 13 <= s.hour <= 16


# ─── filter_free_slots ──────────────────────────────────────

def test_busy_slot_excluded():
    """Занятый слот не попадает в результат."""
    from services.slot_finder import filter_free_slots, SLOT_DURATION
    slot = make_dt(2026, 7, 6, 10)
    busy = [(make_dt(2026, 7, 6, 10), make_dt(2026, 7, 6, 11))]
    result = filter_free_slots([slot], busy)
    assert result == []


def test_free_slot_included():
    """Свободный слот остаётся."""
    from services.slot_finder import filter_free_slots
    slot = make_dt(2026, 7, 6, 10)
    busy = [(make_dt(2026, 7, 6, 11), make_dt(2026, 7, 6, 12))]
    result = filter_free_slots([slot], busy)
    assert slot in result


def test_partial_overlap_excluded():
    """Частичное пересечение — слот занят."""
    from services.slot_finder import filter_free_slots, SLOT_DURATION
    slot = make_dt(2026, 7, 6, 10)
    # занятость начинается в 10:30 — пересекается с нашим слотом 10:00–10:55
    busy = [(make_dt(2026, 7, 6, 10, 30) if False else
             MOSCOW_TZ.localize(datetime(2026, 7, 6, 10, 30)),
             make_dt(2026, 7, 6, 11))]
    result = filter_free_slots([slot], busy)
    assert result == []
