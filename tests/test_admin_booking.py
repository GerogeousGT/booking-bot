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
