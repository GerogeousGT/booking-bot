"""
Тесты парсинга пользовательского ввода.
"""
import pytest
from services.date_parser import detect_period, is_outside_work_hours
from datetime import datetime
import pytz

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def make_dt(weekday_offset=0, hour=10) -> datetime:
    """Создаёт datetime в МСК. weekday_offset=0 — ближайший понедельник."""
    from datetime import date, timedelta
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7  # до ближайшего пн
    monday = today + timedelta(days=days_ahead if days_ahead else 7)
    dt = datetime(monday.year, monday.month, monday.day, hour)
    return MOSCOW_TZ.localize(dt + timedelta(days=weekday_offset))


# ─── detect_period ──────────────────────────────────────────

def test_detect_after_lunch():
    period = detect_period("в понедельник после обеда")
    assert period is not None
    assert period[0] >= 13


def test_detect_morning():
    period = detect_period("в пятницу утром")
    assert period is not None
    assert period[0] == 9


def test_detect_evening():
    period = detect_period("вечером в среду")
    assert period is not None
    assert period[1] <= 17


def test_detect_none_for_specific_time():
    period = detect_period("завтра в 14:00")
    assert period is None


# ─── is_outside_work_hours ──────────────────────────────────

def test_weekend_is_outside():
    saturday = make_dt(weekday_offset=5, hour=10)  # суббота
    assert is_outside_work_hours(saturday, None) is True


def test_workday_inside():
    monday = make_dt(weekday_offset=0, hour=10)
    assert is_outside_work_hours(monday, None) is False


def test_evening_period_outside():
    monday = make_dt(weekday_offset=0, hour=0)
    assert is_outside_work_hours(monday, (19, 21)) is True


def test_morning_period_inside():
    monday = make_dt(weekday_offset=0, hour=0)
    assert is_outside_work_hours(monday, (9, 11)) is False
