"""
Парсинг свободного текста в дату/время консультации.

Два слоя:
  1. `_fallback_parse` — детерминированный разбор частых формулировок
     («завтра в 15», «в пятницу утром», «15.08 в 14:00», «на следующей неделе в 14»).
     Работает без сети, бесплатно, мгновенно.
  2. LLM (см. services/llm_provider.py) — только если первый слой не справился.

Порядок именно такой после инцидента 2026-08-17: провайдер снял модель, единственный
слой парсинга умер, и вместе с ним вся воронка записи. Теперь отказ LLM ломает только
редкие формулировки, а не весь бот.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional, TypedDict

import pytz

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


class LLMUnavailable(RuntimeError):
    """LLM недоступен (сеть, ключ, снятая модель), а фолбэк ввод не распознал.

    Отдельный тип, чтобы хендлер отличил «клиент написал ерунду» от «у нас сломан
    провайдер» и во втором случае дёрнул админа, а не молча ответил «не понял»."""


class ParseResult(TypedDict):
    dt: datetime                        # базовая дата (начало диапазона при date_range)
    period: Optional[tuple[int, int]]   # период дня или None
    date_range: Optional[str]           # "current_week" | "next_week" | None
    target_hour: Optional[int]          # конкретный час при поиске по диапазону (напр. 14)


# ─────────────────── слой 1: детерминированный разбор ───────────────────

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_MONTH_RE = r"(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)"

_MONTH_NUM = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

_WEEKDAY_RE = {
    "monday":    r"понедельник\w*|\bпн\b",
    "tuesday":   r"вторник\w*|\bвт\b",
    "wednesday": r"сред[ауые]\w*|\bср\b",
    "thursday":  r"четверг\w*|\bчт\b",
    "friday":    r"пятниц[ауые]\w*|\bпт\b",
    "saturday":  r"суббот[ауые]\w*|\bсб\b",
    "sunday":    r"воскресен\w*|\bвс\b",
}

_PERIOD_RE = {
    "morning":   r"утр[оаому]\w*|с\s+утра",
    "afternoon": r"после\s+обеда|\bднём\b|\bднем\b|\bдня\b|в\s+обед|обеден\w*",
    "evening":   r"вечер\w*",
}

# Время: с двоеточием, со словом «час», либо после предлога («в 15», «к 10»).
_TIME_COLON_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
_TIME_WORD_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*час(?:ов|а|у)?\b", re.IGNORECASE)
_TIME_PREP_RE = re.compile(r"\b(?:в|к|на)\s+([01]?\d|2[0-3])(?!\s*\d)(?!\d)\b", re.IGNORECASE)


def detect_period(text: str) -> Optional[tuple[int, int]]:
    """Период дня из текста. None, если указано конкретное время — оно приоритетнее."""
    if _extract_time(text) is not None:
        return None
    low = text.lower()
    for name, pattern in _PERIOD_RE.items():
        if re.search(pattern, low, re.IGNORECASE):
            return PERIOD_HOURS[name]
    return None


def _extract_time(text: str) -> Optional[tuple[int, int]]:
    """(час, минута) или None. Проверки — от самого однозначного к самому размытому."""
    m = _TIME_COLON_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _TIME_WORD_RE.search(text)
    if m:
        return int(m.group(1)), 0
    m = _TIME_PREP_RE.search(text)
    if m:
        return int(m.group(1)), 0
    return None


def _strip_dates(text: str) -> str:
    """Убирает фрагменты-даты, чтобы «15 июля» не было прочитано как «15:00»."""
    t = re.sub(rf"\b\d{{1,2}}\s*{_MONTH_RE}\w*", " ", text, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", " ", t)
    t = re.sub(r"\b\d{1,2}\s*(?:числа|го)\b", " ", t, flags=re.IGNORECASE)
    return t


def _month_from_stem(stem: str) -> Optional[int]:
    stem = stem.lower()
    for key in sorted(_MONTH_NUM, key=len, reverse=True):
        if stem.startswith(key):
            return _MONTH_NUM[key]
    return None


def _fallback_parse(text: str) -> Optional[dict]:
    """Разбирает частые формулировки без LLM. Возвращает тот же словарь, что и модель, —
    дальше оба слоя идут через общий `_build_result`."""
    low = text.lower().strip()
    if not low:
        return None

    today = datetime.now(MOSCOW_TZ).date()
    out = {"date": None, "time": None, "period": None, "weekday": None, "date_range": None}

    # ── дата ──
    if re.search(r"\bпослезавтра\b", low):
        out["date"] = (today + timedelta(days=2)).isoformat()
    elif re.search(r"\bзавтра\b", low):
        out["date"] = (today + timedelta(days=1)).isoformat()
    elif re.search(r"\bсегодня\b", low):
        out["date"] = today.isoformat()

    if out["date"] is None:
        m = re.search(r"через\s+(\d+)?\s*(день|дня|дней|недел[юия])", low)
        if m:
            n = int(m.group(1)) if m.group(1) else 1
            delta = timedelta(weeks=n) if m.group(2).startswith("недел") else timedelta(days=n)
            out["date"] = (today + delta).isoformat()

    if out["date"] is None:
        m = re.search(rf"\b(\d{{1,2}})\s*{_MONTH_RE}\w*", low, re.IGNORECASE)
        if m:
            day, month = int(m.group(1)), _month_from_stem(m.group(2))
            if month and 1 <= day <= 31:
                out["date"] = _resolve_date(today, month, day, roll_year=True)

    if out["date"] is None:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", low)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                if m.group(3):
                    year = int(m.group(3))
                    year += 2000 if year < 100 else 0
                    try:
                        out["date"] = date(year, month, day).isoformat()
                    except ValueError:
                        pass
                else:
                    out["date"] = _resolve_date(today, month, day, roll_year=True)

    # ── неделя целиком ──
    if out["date"] is None:
        if re.search(r"(следующ|будущ)\w*\s+недел\w*|на\s+той\s+недел\w*", low):
            out["date_range"] = "next_week"
        elif re.search(r"(эт|текущ)\w*\s+недел\w*", low):
            out["date_range"] = "current_week"

    # ── день недели ──
    if out["date"] is None and out["date_range"] is None:
        for name, pattern in _WEEKDAY_RE.items():
            if re.search(pattern, low, re.IGNORECASE):
                out["weekday"] = name
                break

    if out["date"] is None and out["date_range"] is None and out["weekday"] is None:
        return None  # дня нет — отдаём LLM, вдруг там что-то нестандартное

    # ── время и период (по тексту без дат, чтобы не спутать «15 июля» с «15:00») ──
    rest = _strip_dates(low)
    hm = _extract_time(rest)
    if hm:
        out["time"] = f"{hm[0]:02d}:{hm[1]:02d}"
    else:
        for name, pattern in _PERIOD_RE.items():
            if re.search(pattern, rest, re.IGNORECASE):
                out["period"] = name
                break

    logger.info(f"Фолбэк-парсер разобрал ввод без LLM: {out}")
    return out


def _resolve_date(today: date, month: int, day: int, roll_year: bool) -> Optional[str]:
    """«15 июля» без года: текущий год, а если дата уже прошла — следующий."""
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if roll_year and d < today:
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d.isoformat()


# ─────────────────── слой 2: LLM ───────────────────

def _preprocess(text: str) -> str:
    """Нормализует ввод перед отправкой в LLM."""
    t = text

    # "15.07" или "15.07.2026" → "15 июля" / "15 июля 2026"
    def replace_dot_date(m):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            yr = m.group(3)
            return f"{d} {MONTHS_RU[mo-1]}{(' ' + yr) if yr else ''}"
        return m.group(0)
    t = re.sub(r'\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b', replace_dot_date, t)

    # "через 2 недели" / "через неделю" → конкретные даты
    now = date.today()
    t = re.sub(r'через\s+(\d+)\s+недел[ию]',
               lambda m: (now + timedelta(weeks=int(m.group(1)))).isoformat(),
               t, flags=re.IGNORECASE)
    t = re.sub(r'через\s+неделю',
               lambda _: (now + timedelta(weeks=1)).isoformat(), t, flags=re.IGNORECASE)

    # "в середине июля" → "15 июля"
    for month in MONTHS_RU:
        t = re.sub(rf'в?\s*середин[ае]\s+{month}', f'15 {month}', t, flags=re.IGNORECASE)
    return t


def _build_prompt() -> str:
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


def _call_llm(text: str) -> dict:
    """Один запрос к провайдеру. Всё, что пошло не так, бросается наружу."""
    from services.llm_provider import MODEL, get_client

    client = get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _build_prompt()},
            {"role": "user",   "content": _preprocess(text)[:200]},
        ],
        temperature=0,
        max_tokens=150,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content.strip())


# ─────────────────── общая сборка результата ───────────────────

def _get_week_bounds(offset: int) -> tuple[datetime, datetime]:
    """Возвращает (пн, пт) нужной недели. offset=0 текущая, 1 следующая."""
    now = datetime.now(MOSCOW_TZ)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    monday += timedelta(weeks=offset)
    friday = monday + timedelta(days=4, hours=23, minutes=59)
    return monday, friday


def _build_result(data: dict) -> Optional[ParseResult]:
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
    """Разбирает ввод. None — не поняли. LLMUnavailable — провайдер лежит."""
    fallback = _fallback_parse(text)
    if fallback:
        result = _build_result(fallback)
        if result:
            return result

    # Лимит только на обращения к LLM — бесплатный фолбэк выше он не трогает
    now_ts = time.monotonic()
    if user_id and now_ts - _last_call.get(user_id, 0) < RATE_LIMIT_SEC:
        logger.warning(f"Rate limit user_id={user_id}")
        return None
    if user_id:
        _last_call[user_id] = now_ts

    try:
        data = _call_llm(text)
    except json.JSONDecodeError as e:
        logger.error(f"LLM вернул невалидный JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM недоступен: {e}")
        raise LLMUnavailable(str(e)) from e

    return _build_result(data)


def is_outside_work_hours(dt: datetime, period: Optional[tuple[int, int]]) -> bool:
    from config import WORK_HOUR_START, WORK_HOUR_END
    if dt.weekday() >= 5:
        return True
    if period:
        return period[1] < WORK_HOUR_START or period[0] >= WORK_HOUR_END
    if dt.hour != 0 and not (WORK_HOUR_START <= dt.hour < WORK_HOUR_END):
        return True
    return False
