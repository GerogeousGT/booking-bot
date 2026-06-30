"""
Парсинг свободного текста через Groq (llama-3.1-8b-instant).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional, TypedDict

import pytz
from groq import Groq

from config import TIMEZONE

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)

WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

PERIOD_HOURS = {
    "morning":   (9, 11),
    "afternoon": (13, 16),
    "evening":   (16, 17),
}

_last_call: dict[int, float] = {}
RATE_LIMIT_SEC = 4


class ParseResult(TypedDict):
    dt: datetime                        # базовая дата (начало диапазона при date_range)
    period: Optional[tuple[int, int]]   # период дня или None
    date_range: Optional[str]           # "current_week" | "next_week" | None
    target_hour: Optional[int]          # конкретный час при поиске по диапазону (напр. 14)


def _get_client() -> Groq:
    from config import GROQ_API_KEY
    return Groq(api_key=GROQ_API_KEY)


def _preprocess(text: str) -> str:
    """Нормализует ввод перед отправкой в Groq."""
    import re
    t = text
    # "15.07" или "15.07.2026" → "15 июля" / "15 июля 2026"
    months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
    def replace_dot_date(m):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            yr = m.group(3)
            return f"{d} {months[mo-1]}{(' ' + yr) if yr else ''}"
        return m.group(0)
    t = re.sub(r'\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b', replace_dot_date, t)
    # "через 2 недели" / "через неделю" → конкретные даты
    now = date.today()
    def replace_weeks(m):
        n = int(m.group(1)) if m.group(1) else 1
        target = now + timedelta(weeks=n)
        return target.strftime("%-d %B").lower() if hasattr(now, 'strftime') else str(target)
    t = re.sub(r'через\s+(\d+)\s+недел[ию]', replace_weeks, t, flags=re.IGNORECASE)
    t = re.sub(r'через\s+неделю', lambda _: (now + timedelta(weeks=1)).isoformat(), t, flags=re.IGNORECASE)
    # "в середине июля" → "15 июля"
    mid_months = {"января":15,"февраля":14,"марта":15,"апреля":15,"мая":15,"июня":15,
                  "июля":15,"августа":15,"сентября":15,"октября":15,"ноября":15,"декабря":15}
    for month, day in mid_months.items():
        t = re.sub(rf'в?\s*середин[ае]\s+{month}', f'{day} {month}', t, flags=re.IGNORECASE)
    return t


def _build_prompt() -> str:
    from datetime import timedelta
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    day_after = (today + timedelta(days=2)).isoformat()
    today_str = today.isoformat()
    in_2_weeks = (today + timedelta(weeks=2)).isoformat()
    in_3_weeks = (today + timedelta(weeks=3)).isoformat()
    return f"""Извлеки из сообщения дату/время консультации. Сегодня {today_str}, часовой пояс МСК.
Ответь строго JSON без markdown:
{{"date": "YYYY-MM-DD или null", "time": "HH:MM или null", "period": "morning|afternoon|evening|null", "weekday": "monday|tuesday|wednesday|thursday|friday|saturday|sunday или null", "date_range": "current_week|next_week|null"}}

Правила:
- "сегодня" → date="{today_str}", "завтра" → date="{tomorrow}", "послезавтра" → date="{day_after}".
- "через 2 недели" → date="{in_2_weeks}", "через 3 недели" → date="{in_3_weeks}".
- Даты вида "15 июля", "3 августа" — конвертируй в YYYY-MM-DD текущего или следующего года.
- Всегда заполняй date если день можно хоть как-то определить.
- period: morning=утро 9-12, afternoon=после обеда 13-17, evening=вечер 16-18. Если указано конкретное время — period=null.
- Если period и time одновременно — приоритет у time, period=null.
- Если только день недели без даты — date=null, заполни weekday.
- Если "на следующей неделе" или "на этой неделе" — заполни date_range, date=null.
- date_range: current_week=текущая пн-пт, next_week=следующая пн-пт.
- Если ничего конкретного нет — все поля null.
- Игнорируй опечатки и старайся понять смысл."""


def _get_week_bounds(offset: int) -> tuple[datetime, datetime]:
    """Возвращает (пн, пт) нужной недели. offset=0 текущая, 1 следующая."""
    now = datetime.now(MOSCOW_TZ)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    monday += timedelta(weeks=offset)
    friday = monday + timedelta(days=4, hours=23, minutes=59)
    return monday, friday


def _parse_groq_response(data: dict) -> Optional[ParseResult]:
    now = datetime.now(MOSCOW_TZ)

    raw_date    = data.get("date")    or None
    raw_time    = data.get("time")    or None
    raw_period  = data.get("period")  or None
    raw_weekday = data.get("weekday") or None
    raw_range   = data.get("date_range") or None

    # Нормализуем строки "null"
    for val in ["null"]:
        if raw_date    == val: raw_date    = None
        if raw_time    == val: raw_time    = None
        if raw_period  == val: raw_period  = None
        if raw_weekday == val: raw_weekday = None
        if raw_range   == val: raw_range   = None

    has_time = bool(raw_time)
    period = None if has_time else (PERIOD_HOURS.get(raw_period) if raw_period else None)

    # --- Случай: диапазон недели ---
    if raw_range in ("current_week", "next_week"):
        offset = 0 if raw_range == "current_week" else 1
        week_start, _ = _get_week_bounds(offset)
        # Если неделя уже в прошлом — берём следующую
        if week_start.date() < now.date():
            week_start, _ = _get_week_bounds(1)

        target_hour = None
        if raw_time:
            try:
                target_hour = int(raw_time.split(":")[0])
            except ValueError:
                pass

        return ParseResult(
            dt=week_start,
            period=period,
            date_range=raw_range,
            target_hour=target_hour,
        )

    # --- Случай: конкретная дата или день недели ---
    base_dt: Optional[datetime] = None

    if raw_date:
        try:
            d = date.fromisoformat(raw_date)
            base_dt = MOSCOW_TZ.localize(datetime(d.year, d.month, d.day))
        except ValueError:
            pass

    if base_dt is None and raw_weekday and raw_weekday in WEEKDAY_MAP:
        target_wd = WEEKDAY_MAP[raw_weekday]
        days_ahead = (target_wd - now.weekday()) % 7 or 7
        target = now.date() + timedelta(days=days_ahead)
        base_dt = MOSCOW_TZ.localize(datetime(target.year, target.month, target.day))

    if base_dt is None:
        return None

    if has_time:
        try:
            h, m = map(int, raw_time.split(":"))
            base_dt = base_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            pass
    elif period:
        base_dt = base_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    if base_dt.date() < now.date():
        base_dt += timedelta(weeks=1)

    return ParseResult(dt=base_dt, period=period, date_range=None, target_hour=None)


def parse_user_input(text: str, user_id: int = 0) -> Optional[ParseResult]:
    now_ts = time.monotonic()
    if user_id and now_ts - _last_call.get(user_id, 0) < RATE_LIMIT_SEC:
        logger.warning(f"Rate limit user_id={user_id}")
        return None
    if user_id:
        _last_call[user_id] = now_ts

    try:
        client = _get_client()
        processed = _preprocess(text)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _build_prompt()},
                {"role": "user",   "content": processed[:200]},
            ],
            temperature=0,
            max_tokens=150,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        return _parse_groq_response(data)
    except json.JSONDecodeError:
        logger.error(f"Groq вернул невалидный JSON: {raw!r}")
        return None
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return None


def is_outside_work_hours(dt: datetime, period: Optional[tuple[int, int]]) -> bool:
    from config import WORK_HOUR_START, WORK_HOUR_END
    if dt.weekday() >= 5:
        return True
    if period:
        return period[1] < WORK_HOUR_START or period[0] >= WORK_HOUR_END
    if dt.hour != 0 and not (WORK_HOUR_START <= dt.hour < WORK_HOUR_END):
        return True
    return False
