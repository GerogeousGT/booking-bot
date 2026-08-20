"""
Защита от двойной записи на один слот.

Календарь — единственный источник истины: слот занят, если пересекается с любым
событием в нём (включая личные события психолога, не только консультации).
Проверка делается дважды — когда слоты показываются и второй раз прямо перед
созданием записи, потому что между показом и нажатием кнопки проходит время.
"""
from datetime import datetime, timedelta

import pytest
import pytz

import services.availability as availability
from services.slot_finder import SLOT_DURATION, filter_free_slots

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _next_workday(hour: int) -> datetime:
    """Ближайший будний день на заданный час, минимум через сутки."""
    d = datetime.now(MOSCOW_TZ) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


# ─── пересечения слотов ─────────────────────────────────────

def test_slot_taken_by_exact_match():
    slot = _next_workday(14)
    busy = [(slot, slot + SLOT_DURATION)]
    assert filter_free_slots([slot], busy) == []


def test_slot_taken_by_partial_overlap():
    """Событие на 14:30–15:30 должно убирать и слот 14:00, и слот 15:00."""
    slot_14 = _next_workday(14)
    slot_15 = slot_14 + timedelta(hours=1)
    busy = [(slot_14 + timedelta(minutes=30), slot_14 + timedelta(minutes=90))]
    assert filter_free_slots([slot_14, slot_15], busy) == []


def test_adjacent_event_does_not_block():
    """Слот 14:00 длится 55 минут — событие ровно в 15:00 его не задевает."""
    slot = _next_workday(14)
    busy = [(slot + timedelta(hours=1), slot + timedelta(hours=2))]
    assert filter_free_slots([slot], busy) == [slot]


def test_free_slot_stays_free():
    slot = _next_workday(14)
    other_day = slot + timedelta(days=1)
    busy = [(other_day, other_day + SLOT_DURATION)]
    assert filter_free_slots([slot], busy) == [slot]


# ─── is_slot_free: последняя проверка перед созданием записи ───

def test_is_slot_free_false_when_busy(monkeypatch):
    slot = _next_workday(14)
    monkeypatch.setattr(availability, "get_busy_slots",
                        lambda start, end: [(slot, slot + SLOT_DURATION)])
    assert availability.is_slot_free(slot) is False


def test_is_slot_free_true_when_calendar_empty(monkeypatch):
    slot = _next_workday(14)
    monkeypatch.setattr(availability, "get_busy_slots", lambda start, end: [])
    assert availability.is_slot_free(slot) is True


def test_is_slot_free_blocked_by_personal_event(monkeypatch):
    """В календаре не только консультации — любое личное событие тоже занимает слот."""
    slot = _next_workday(14)
    monkeypatch.setattr(availability, "get_busy_slots",
                        lambda start, end: [(slot - timedelta(minutes=30),
                                             slot + timedelta(minutes=30))])
    assert availability.is_slot_free(slot) is False


def test_busy_slots_not_offered(monkeypatch):
    """Занятый час не должен попасть в список предложенных слотов."""
    slot = _next_workday(14)
    monkeypatch.setattr(availability, "get_busy_slots",
                        lambda start, end: [(slot, slot + SLOT_DURATION)])
    offered = availability.find_free_slots(slot, None)
    assert slot not in offered
    assert all(s != slot for s in offered)
