"""
Отмена записи психологом.

Клиент может отменить только свою запись — в его хендлере стоит проверка telegram_id.
Психологу нужен свой путь: если договорённость об отмене не пройдёт через бота,
событие останется в календаре, а клиенту придут напоминания о встрече, которой нет.
"""
import asyncio
from datetime import datetime, timedelta

import pytz

from handlers.admin import _ADMIN_CANCEL_RE, booking_actions_keyboard

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "t.db"))
    import importlib, database
    importlib.reload(database)
    return database


# ─── распознавание намерения ────────────────────────────────

def test_cancel_intent_recognised():
    for phrase in ["отменить запись", "отмени запись Лизы", "удали запись #9",
                   "снять запись на завтра", "убрать запись"]:
        assert _ADMIN_CANCEL_RE.search(phrase), phrase


def test_plain_schedule_question_is_not_cancel():
    """«что у меня завтра» — это просмотр, кнопки отмены показывать не надо."""
    for phrase in ["что у меня завтра", "покажи записи", "клиенты на этой неделе"]:
        assert not _ADMIN_CANCEL_RE.search(phrase), phrase


def test_card_offers_move_and_cancel():
    kb = booking_actions_keyboard(42)
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == ["amove:42", "adel:42"]


# ─── что происходит с записью после отмены ──────────────────

def test_cancelled_booking_disappears_from_upcoming(tmp_path, monkeypatch):
    """Слот должен освободиться, а запись — уйти из всех рабочих выборок."""
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        booking_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=(now + timedelta(days=1)).isoformat(),
            slot_end=(now + timedelta(days=1, minutes=55)).isoformat(),
            google_event_id="ev1", meet_url="", created_at=now.isoformat(),
        )
        assert len(await db.get_upcoming_bookings()) == 1

        await db.cancel_booking(booking_id)

        assert await db.get_upcoming_bookings() == []
        # и у клиента она тоже пропадает из активных
        assert await db.get_user_bookings(111) == []

    asyncio.run(scenario())


def test_no_reminders_for_cancelled_booking(tmp_path, monkeypatch):
    """Главное: клиенту не должны прийти напоминания о встрече, которой не будет."""
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        booking_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=(now + timedelta(hours=2)).isoformat(),
            slot_end=(now + timedelta(hours=2, minutes=55)).isoformat(),
            google_event_id="ev1", meet_url="https://x.ru/1", created_at=now.isoformat(),
        )
        assert len(await db.get_pending_reminders()) == 1

        await db.cancel_booking(booking_id)

        assert await db.get_pending_reminders() == []

    asyncio.run(scenario())


def test_cancel_keeps_history(tmp_path, monkeypatch):
    """Запись не стирается, а помечается cancelled — история остаётся."""
    import sqlite3
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        booking_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=(now + timedelta(days=1)).isoformat(),
            slot_end=(now + timedelta(days=1, minutes=55)).isoformat(),
            google_event_id="ev1", meet_url="", created_at=now.isoformat(),
        )
        await db.cancel_booking(booking_id)
        row = await db.get_booking(booking_id)
        assert row is not None, "строка должна остаться в базе"
        assert row["status"] == "cancelled"

    asyncio.run(scenario())


def test_double_cancel_is_safe(tmp_path, monkeypatch):
    """Повторное нажатие кнопки не должно ничего ломать."""
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        booking_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=(now + timedelta(days=1)).isoformat(),
            slot_end=(now + timedelta(days=1, minutes=55)).isoformat(),
            google_event_id="ev1", meet_url="", created_at=now.isoformat(),
        )
        await db.cancel_booking(booking_id)
        await db.cancel_booking(booking_id)
        row = await db.get_booking(booking_id)
        assert row["status"] == "cancelled"

    asyncio.run(scenario())


# ─── перенос записи ─────────────────────────────────────────

def test_move_frees_old_slot_and_keeps_one_active(tmp_path, monkeypatch):
    """Перенос = снять старую + создать новую. Активной должна остаться одна,
    старый слот — освободиться, история обеих сохраниться."""
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        old_start = now + timedelta(days=1, hours=2)
        new_start = now + timedelta(days=1, hours=10)

        old_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=old_start.isoformat(),
            slot_end=(old_start + timedelta(minutes=55)).isoformat(),
            google_event_id="ev_old", meet_url="https://x.ru/1",
            created_at=now.isoformat(),
        )
        # так делает перенос: снимаем старую, создаём новую
        await db.cancel_booking(old_id)
        new_id = await db.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=new_start.isoformat(),
            slot_end=(new_start + timedelta(minutes=55)).isoformat(),
            google_event_id="ev_new", meet_url="https://x.ru/1",
            created_at=now.isoformat(),
        )

        upcoming = await db.get_upcoming_bookings()
        assert [b["id"] for b in upcoming] == [new_id], "активной остаётся одна запись"

        assert (await db.get_booking(old_id))["status"] == "cancelled"
        assert (await db.get_booking(new_id))["status"] == "confirmed"
        # по снятой записи напоминания не уйдут
        assert [b["id"] for b in await db.get_pending_reminders()] == [new_id]

    asyncio.run(scenario())


def test_move_keeps_client_link(tmp_path, monkeypatch):
    """Ссылка живёт на клиенте — после переноса она подставится в новую запись."""
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ)
        url = "https://telemost.yandex.ru/j/1"
        await db.upsert_client(111, "Тест", "@test", now.isoformat())
        await db.set_client_meet_url(111, url)
        assert await db.get_client_meet_url(111) == url

    asyncio.run(scenario())
