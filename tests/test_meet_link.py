"""
Тесты вокруг ссылки на видеовстречу.

Автогенерация `meet.jit.si` убрана: публичный инстанс в РФ открывал комнату, но
медиа не доходило — клиент видел пустой экран. Ссылку присылает психолог через бота,
она сохраняется на клиенте и со следующей записи подставляется сама.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytz

from handlers.admin import _extract_url, link_actions_keyboard

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


# ─── распознавание ссылки ───────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("https://vk.com/call/join/abc-123", "https://vk.com/call/join/abc-123"),
    ("http://talk.kontur.ru/room42", "http://talk.kontur.ru/room42"),
    ("вот ссылка https://jazz.sber.ru/xyz заходи", "https://jazz.sber.ru/xyz"),
    ("https://example.com/room.", "https://example.com/room"),   # точка в конце фразы
    ("ссылка: (https://example.com/r)", "https://example.com/r"),
])
def test_extract_url_ok(text, expected):
    assert _extract_url(text) == expected


@pytest.mark.parametrize("text", [
    "сейчас пришлю",
    "vk.com/call/join/abc",   # без схемы — не принимаем, клиент получит битую ссылку
    "",
    None,
])
def test_extract_url_rejects_non_links(text):
    """Лучше переспросить психолога, чем отправить клиенту мусор."""
    assert _extract_url(text) is None


# ─── клавиатура управления ссылкой ──────────────────────────

def test_keyboard_without_link_offers_send():
    kb = link_actions_keyboard(7, has_link=False)
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == ["sendlink:7"]


def test_keyboard_with_link_offers_resend_and_replace():
    kb = link_actions_keyboard(7, has_link=True)
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == ["resend:7", "relink:7"]


# ─── постоянная ссылка клиента ──────────────────────────────

def test_client_link_is_reused(tmp_path, monkeypatch):
    """Ссылка живёт на клиенте: отправили один раз — следующая запись получает её сама."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("config.DB_PATH", str(db_path))

    import importlib
    import database
    importlib.reload(database)

    async def scenario():
        await database.init_db()
        now = datetime.now(MOSCOW_TZ)

        await database.upsert_client(111, "Тест", "@test", now.isoformat())
        assert await database.get_client_meet_url(111) == ""

        booking_id = await database.create_booking(
            telegram_id=111, name="Тест", contact="@test",
            slot_start=(now + timedelta(days=1)).isoformat(),
            slot_end=(now + timedelta(days=1, minutes=55)).isoformat(),
            google_event_id="ev1", meet_url="", created_at=now.isoformat(),
        )

        # без ссылки психолог получит запрос прислать её
        upcoming = await database.get_upcoming_bookings()
        assert [b["id"] for b in upcoming] == [booking_id]
        assert not upcoming[0]["meet_url"]

        url = "https://vk.com/call/join/abc-123"
        await database.set_booking_meet_url(booking_id, url)
        await database.set_client_meet_url(111, url)

        # запрос прислать ссылку по ней больше не нужен
        upcoming = await database.get_upcoming_bookings()
        assert upcoming[0]["meet_url"] == url
        # а следующая запись этого клиента подставит ссылку сама
        assert await database.get_client_meet_url(111) == url

    asyncio.run(scenario())


def test_migration_adds_columns_to_existing_db(tmp_path, monkeypatch):
    """Живая база на VPS создана до этих колонок — init_db должен их догнать,
    CREATE TABLE IF NOT EXISTS сам по себе этого не делает."""
    import sqlite3
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
        name TEXT NOT NULL, contact TEXT NOT NULL, slot_start TEXT NOT NULL,
        slot_end TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
        google_event_id TEXT, meet_url TEXT, created_at TEXT NOT NULL,
        reminder_24h_sent INTEGER NOT NULL DEFAULT 0,
        reminder_1h_sent INTEGER NOT NULL DEFAULT 0,
        reminder_5min_sent INTEGER NOT NULL DEFAULT 0)""")
    con.execute("""CREATE TABLE clients (
        telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL,
        updated_at TEXT NOT NULL, consent_given INTEGER NOT NULL DEFAULT 0,
        consent_at TEXT)""")
    con.commit()
    con.close()

    monkeypatch.setattr("config.DB_PATH", str(db_path))
    import importlib
    import database
    importlib.reload(database)

    asyncio.run(database.init_db())

    con = sqlite3.connect(db_path)
    bookings_cols = {r[1] for r in con.execute("PRAGMA table_info(bookings)")}
    clients_cols = {r[1] for r in con.execute("PRAGMA table_info(clients)")}
    con.close()

    assert "admin_link_prompt_sent" in bookings_cols
    assert "admin_link_retry_sent" in bookings_cols
    assert "meet_url" in clients_cols

    # повторный запуск не должен падать на уже добавленных колонках
    asyncio.run(database.init_db())


# ─── окна напоминаний психологу ─────────────────────────────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})


def _booking_row(minutes_left: int, prompt_sent=0, retry_sent=0, booking_id=1, meet_url=""):
    slot_start = datetime.now(MOSCOW_TZ) + timedelta(minutes=minutes_left)
    return {
        "id": booking_id, "name": "Тест", "telegram_id": 111,
        "slot_start": slot_start.isoformat(),
        "meet_url": meet_url,
        "admin_link_prompt_sent": prompt_sent,
        "admin_link_retry_sent": retry_sent,
    }


def _run_check(monkeypatch, rows):
    import services.notifier as notifier
    marked = []

    async def fake_needing_link():
        return rows

    async def fake_mark(booking_id, kind):
        marked.append((booking_id, kind))

    monkeypatch.setattr(notifier, "get_upcoming_bookings", fake_needing_link)
    monkeypatch.setattr(notifier, "mark_link_prompt_sent", fake_mark)
    monkeypatch.setattr(notifier, "ADMIN_TELEGRAM_ID", 999)

    bot = _FakeBot()
    asyncio.run(notifier._check_pre_session(bot))
    return bot, marked


def test_admin_pinged_when_link_missing(monkeypatch):
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=10)])
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 999
    assert marked == [(1, "prompt")]
    actions = [b.callback_data for r in bot.sent[0]["markup"].inline_keyboard for b in r]
    assert actions == ["sendlink:1"]


def test_no_ping_while_session_is_far_away(monkeypatch):
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=120)])
    assert bot.sent == []
    assert marked == []


def test_ping_sent_once(monkeypatch):
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=10, prompt_sent=1)])
    assert bot.sent == []


def test_urgent_ping_close_to_start(monkeypatch):
    """За 3 минуты до начала — повторный пинг, даже если первый уже был."""
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=2, prompt_sent=1)])
    assert len(bot.sent) == 1
    assert "‼️" in bot.sent[0]["text"]
    assert marked == [(1, "retry")]


def test_no_ping_after_session_started(monkeypatch):
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=-5)])
    assert bot.sent == []


def test_admin_reminded_even_when_link_exists(monkeypatch):
    """Главное: раньше при готовой ссылке психолог не получал вообще ничего
    и мог просто забыть про консультацию."""
    url = "https://telemost.yandex.ru/j/123"
    bot, marked = _run_check(monkeypatch, [_booking_row(minutes_left=10, meet_url=url)])
    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "Скоро консультация" in text
    assert url in text, "ссылка должна быть прямо в напоминании"
    assert marked == [(1, "prompt")]


def test_reminder_with_link_offers_resend_and_replace(monkeypatch):
    bot, _ = _run_check(monkeypatch, [_booking_row(minutes_left=10, meet_url="https://x.ru/1")])
    actions = [b.callback_data for r in bot.sent[0]["markup"].inline_keyboard for b in r]
    assert actions == ["resend:1", "relink:1"]


def test_no_second_ping_when_link_is_in_place(monkeypatch):
    """Повторный пинг за пару минут нужен только когда ссылки нет."""
    bot, marked = _run_check(monkeypatch, [
        _booking_row(minutes_left=2, prompt_sent=1, meet_url="https://x.ru/1")
    ])
    assert bot.sent == []
    assert marked == []


def test_reminder_sent_once_per_booking(monkeypatch):
    bot, _ = _run_check(monkeypatch, [
        _booking_row(minutes_left=10, prompt_sent=1, meet_url="https://x.ru/1")
    ])
    assert bot.sent == []
