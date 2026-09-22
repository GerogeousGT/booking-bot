"""
Тесты парсинга пользовательского ввода.

Отдельно проверяется, что фолбэк-слой разбирает частые формулировки БЕЗ обращения
к LLM: 2026-08-17 провайдер снял модель и вся воронка записи встала, потому что
других слоёв парсинга не было. Тест `test_no_llm_calls_on_common_phrases` — страховка
от повторения: если кто-то снова заведёт всё на сеть, он упадёт.
"""
from datetime import date, datetime, timedelta

import pytest
import pytz

from services.date_parser import (
    LLMUnavailable, _fallback_parse, detect_period, is_outside_work_hours,
    parse_user_input,
)

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def make_dt(weekday_offset=0, hour=10) -> datetime:
    """Создаёт datetime в МСК. weekday_offset=0 — ближайший понедельник."""
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


# ─── фолбэк-парсер (без сети) ───────────────────────────────

def test_fallback_tomorrow_with_time():
    today = datetime.now(MOSCOW_TZ).date()
    out = _fallback_parse("завтра в 15:00")
    assert out["date"] == (today + timedelta(days=1)).isoformat()
    assert out["time"] == "15:00"


def test_fallback_tomorrow_bare_hour():
    out = _fallback_parse("завтра в 10")
    assert out["time"] == "10:00"


def test_fallback_weekday_with_period():
    out = _fallback_parse("в пятницу утром")
    assert out["weekday"] == "friday"
    assert out["period"] == "morning"
    assert out["time"] is None


def test_fallback_day_and_month_not_read_as_time():
    """«15 июля» — это дата, а не 15:00. Раньше на этом легко было споткнуться."""
    out = _fallback_parse("15 июля")
    assert out["date"].endswith("-07-15")
    assert out["time"] is None


def test_fallback_day_month_with_time():
    out = _fallback_parse("15 июля в 14:00")
    assert out["date"].endswith("-07-15")
    assert out["time"] == "14:00"


def test_fallback_dot_date():
    out = _fallback_parse("20.09 в 11:30")
    assert out["date"].endswith("-09-20")
    assert out["time"] == "11:30"


def test_fallback_next_week():
    out = _fallback_parse("на следующей неделе в 14:00")
    assert out["date_range"] == "next_week"
    assert out["time"] == "14:00"


def test_fallback_past_date_rolls_to_next_year():
    """Дата, которая уже прошла в этом году, уезжает на следующий, а не в прошлое."""
    today = datetime.now(MOSCOW_TZ).date()
    yesterday = today - timedelta(days=1)
    out = _fallback_parse(f"{yesterday.day}.{yesterday.month:02d}")
    assert date.fromisoformat(out["date"]) >= today


def test_fallback_gives_up_on_nonsense():
    assert _fallback_parse("хочу к психологу") is None
    assert _fallback_parse("") is None


def test_no_llm_calls_on_common_phrases(monkeypatch):
    """Частые формулировки должны разбираться без единого обращения к провайдеру."""
    def explode(*args, **kwargs):
        raise AssertionError("фолбэк не сработал — ушли в LLM")

    monkeypatch.setattr("services.date_parser._call_llm", explode)

    for phrase in [
        "завтра в 15:00", "в пятницу утром", "сегодня после обеда",
        "15 июля в 14:00", "20.09 в 11:30", "на следующей неделе в 14:00",
        "послезавтра в 12", "в среду вечером",
    ]:
        assert parse_user_input(phrase) is not None, phrase


def test_llm_failure_raises_llm_unavailable(monkeypatch):
    """Отказ провайдера — отдельное исключение, чтобы хендлер дёрнул админа,
    а не ответил клиенту «не понял» и не замолчал на три дня."""
    def explode(*args, **kwargs):
        raise RuntimeError("model_not_found")

    monkeypatch.setattr("services.date_parser._call_llm", explode)

    with pytest.raises(LLMUnavailable):
        parse_user_input("когда-нибудь на неделе, как получится")


# ─── форматы времени, на которых бот спотыкался ─────────────

@pytest.mark.parametrize("text,expected", [
    ("сегодня 20-00", "20:00"),      # дефис вместо двоеточия — так пишут чаще всего
    ("сегодня в 20-00", "20:00"),
    ("сегодня 20:00", "20:00"),
    ("сегодня 20.00", "20:00"),      # точка: раньше съедалась как дата «20 число»
    ("сегодня 20", "20:00"),         # голое число без предлога «в»
    ("завтра 15", "15:00"),
    ("завтра в 15", "15:00"),
])
def test_time_formats_are_understood(text, expected):
    """Реальный случай: «сегодня 20-00» терялось целиком, и бот уходил в дневную сетку."""
    out = _fallback_parse(text)
    assert out is not None and out["time"] == expected, text


@pytest.mark.parametrize("text,expected", [
    ("сегодня в 8 вечера", "20:00"),
    ("завтра в 3 дня", "15:00"),
    ("сегодня в 9 утра", "09:00"),   # «сегодня» не должно читаться как «дня» и давать 21:00
    ("завтра в 10 утра", "10:00"),
])
def test_daypart_hints_shift_hour(text, expected):
    out = _fallback_parse(text)
    assert out is not None and out["time"] == expected, text


def test_date_still_wins_over_time():
    """«15 июля» — дата, а не 15:00. Голое число не должно это ломать."""
    out = _fallback_parse("15 июля")
    assert out["date"].endswith("-07-15")
    assert out["time"] is None


def test_dotted_date_still_parsed_as_date():
    """«15.08» остаётся датой: второе число — валидный месяц."""
    out = _fallback_parse("15.08")
    assert out["date"].endswith("-08-15")
    assert out["time"] is None


def test_admin_evening_is_real_evening():
    """Для клиента «вечер» = последний рабочий час, для психолога — настоящий вечер."""
    from services.date_parser import PERIOD_HOURS, widen_period
    assert widen_period(PERIOD_HOURS["evening"]) == (17, 22)
    assert widen_period(None) is None
