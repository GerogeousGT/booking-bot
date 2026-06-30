"""
Работа с Google Calendar через Service Account.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pytz

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_CALENDAR_ID, TIMEZONE, SLOT_DURATION_MIN
from datetime import timedelta

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone(TIMEZONE)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# colorId 10 = Basil (тёмно-зелёный) в Google Calendar
CONSULTATION_COLOR_ID = "10"


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_busy_slots(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Возвращает список занятых интервалов из Google Calendar."""
    service = _get_service()
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    result = service.freebusy().query(body=body).execute()
    busy_raw = result.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])

    intervals: list[tuple[datetime, datetime]] = []
    for b in busy_raw:
        b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(MOSCOW_TZ)
        b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(MOSCOW_TZ)
        intervals.append((b_start, b_end))

    return intervals


def create_event(name: str, contact: str, slot_start: datetime, slot_end: datetime, meet_url: str = "", tg_username: str = "") -> str:
    """Создаёт событие в Google Calendar. Возвращает event_id."""
    service = _get_service()
    description = f"Контакт: {contact}"
    if tg_username:
        description += f"\nTelegram: @{tg_username}"
    if meet_url:
        description += f"\nТелемост: {meet_url}"
    event = {
        "summary": f"Консультация — {name}",
        "description": description,
        "start": {"dateTime": slot_start.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": slot_end.isoformat(),   "timeZone": TIMEZONE},
        "colorId": CONSULTATION_COLOR_ID,
    }
    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    logger.info(f"Создано событие {created['id']} для {name} на {slot_start}")
    return created["id"]


def delete_event(event_id: str) -> bool:
    """Удаляет событие. Возвращает True если успешно."""
    try:
        service = _get_service()
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        logger.info(f"Удалено событие {event_id}")
        return True
    except HttpError as e:
        logger.error(f"Ошибка удаления события {event_id}: {e}")
        return False
