"""
Запись, которую делает психолог: /add и «Записать на другое время».

Оба входа ведут в одно ядро — смысл в том, чтобы любая договорённость попадала
в календарь через бота, а не оставалась ручной записью в голове.
"""
from datetime import datetime, timedelta

import pytz

from handlers.admin import _admin_slots_keyboard

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _slot(hour: int = 14) -> datetime:
    d = datetime.now(MOSCOW_TZ) + timedelta(days=1)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


def test_slots_keyboard_carries_exact_time():
    slot = _slot()
    kb = _admin_slots_keyboard([slot])
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == [f"addslot:{slot.isoformat()}", "addslot:other"]


def test_slots_keyboard_always_offers_another_time():
    kb = _admin_slots_keyboard([])
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == ["addslot:other"]


def test_out_of_hours_slot_is_marked():
    """Психолог может записать на выходной или вечер, но должен это видеть."""
    slot = _slot(19)
    kb = _admin_slots_keyboard([slot], note="⚠️ вне расписания: ")
    label = kb.inline_keyboard[0][0].text
    assert label.startswith("⚠️ вне расписания: ")


def test_slot_roundtrip_survives_keyboard():
    """Время уезжает в callback_data строкой и должно вернуться тем же моментом."""
    slot = _slot(9)
    kb = _admin_slots_keyboard([slot])
    payload = kb.inline_keyboard[0][0].callback_data[len("addslot:"):]
    assert datetime.fromisoformat(payload) == slot


# ─── кого показывает /add ───────────────────────────────────

def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "t.db"))
    import importlib, database
    importlib.reload(database)
    return database


def test_add_shows_people_who_never_booked(tmp_path, monkeypatch):
    """Главный баг: человек стартовал бота и дал согласие, но не записывался —
    и был невидим для /add, потому что список брался из таблицы clients."""
    import asyncio
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ).isoformat()

        # довёл запись до конца — попал в clients
        await db.save_consent(111, now)
        await db.upsert_known_user(111, "client_one", "Клиент", now)
        await db.upsert_client(111, "Клиент", "@client_one", now)

        # только стартовал бота и дал согласие
        await db.save_consent(222, now)
        await db.upsert_known_user(222, "vikhorishko", "Vilena", now)

        people = await db.get_bookable_people()
        by_id = {p["telegram_id"]: p for p in people}

        assert 222 in by_id, "человек без записей должен быть доступен для /add"
        assert by_id[222]["name"] == "Vilena"
        assert by_id[222]["contact"] == "@vikhorishko"
        assert by_id[222]["is_client"] is False
        assert by_id[111]["is_client"] is True
        # клиенты с записями идут первыми
        assert people[0]["telegram_id"] == 111

    asyncio.run(scenario())


def test_add_hides_people_without_consent(tmp_path, monkeypatch):
    """Записать человека без согласия на обработку данных нельзя."""
    import asyncio
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ).isoformat()
        await db.upsert_known_user(333, "nocons", "Аноним", now)
        assert await db.get_bookable_people() == []

    asyncio.run(scenario())


def test_username_change_is_picked_up(tmp_path, monkeypatch):
    import asyncio
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ).isoformat()
        await db.save_consent(444, now)
        await db.upsert_known_user(444, "old_name", "Инна", now)
        await db.upsert_known_user(444, "new_name", "Инна", now)
        people = await db.get_bookable_people()
        assert people[0]["contact"] == "@new_name"

    asyncio.run(scenario())


def test_person_without_username_falls_back_to_id(tmp_path, monkeypatch):
    import asyncio
    db = _fresh_db(tmp_path, monkeypatch)

    async def scenario():
        await db.init_db()
        now = datetime.now(MOSCOW_TZ).isoformat()
        await db.save_consent(555, now)
        await db.upsert_known_user(555, "", "Безымянный", now)
        person = await db.get_person(555)
        assert person["contact"] == "id:555"

    asyncio.run(scenario())
