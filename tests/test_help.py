"""
Справка по командам.

Смысл теста не в тексте, а в том, чтобы шпаргалка не разошлась с кодом: команду
добавили, в справку забыли — и её снова придётся вспоминать наизусть.
"""
import re

from handlers.admin import ADMIN_HELP, CLIENT_HELP


def _commands_in(text: str) -> set[str]:
    return set(re.findall(r"/([a-z]+)", text))


def test_admin_help_lists_every_admin_command():
    """Все команды психолога должны быть в его справке."""
    assert {"add", "bookings", "link"} <= _commands_in(ADMIN_HELP)


def test_admin_help_mentions_text_commands():
    """Половина функций вызывается свободным текстом — про это легче всего забыть."""
    low = ADMIN_HELP.lower()
    assert "перенести" in low
    assert "отменить запись" in low


def test_admin_help_explains_automatic_notifications():
    """Что бот делает сам — тоже часть картины: иначе кажется, что он молчит."""
    low = ADMIN_HELP.lower()
    assert "15 мин" in low
    assert "за сутки" in low


def test_client_help_has_no_admin_commands():
    """Клиенту админские команды не показываем — они ему всё равно недоступны."""
    client_cmds = _commands_in(CLIENT_HELP)
    assert "add" not in client_cmds
    assert "bookings" not in client_cmds
    assert "link" not in client_cmds


def test_client_help_lists_client_commands():
    assert {"cancel", "privacy", "deletedata", "start"} <= _commands_in(CLIENT_HELP)


def test_help_fits_single_telegram_message():
    """Лимит Telegram — 4096 символов на сообщение."""
    assert len(ADMIN_HELP) < 4096
    assert len(CLIENT_HELP) < 4096
