"""
Точка входа booking-bot.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
)

from config import ADMIN_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from database import init_db
from services.notifier import reminder_loop
import handlers.admin as admin
import handlers.booking as booking

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


async def setup_commands(bot: Bot) -> None:
    """Регистрирует команды в меню Telegram — чтобы они всплывали при вводе «/»,
    а не вспоминались. Психолог видит свой набор, клиенты — свой.

    Telegram принимает в меню только [a-z0-9_], поэтому «/?» туда не попадает —
    он работает как алиас к /help, набранный вручную."""
    client_commands = [
        BotCommand(command="start", description="Начать сначала"),
        BotCommand(command="cancel", description="Отменить или перенести запись"),
        BotCommand(command="help", description="Что я умею"),
        BotCommand(command="privacy", description="Политика конфиденциальности"),
        BotCommand(command="deletedata", description="Удалить мои данные"),
    ]
    await bot.set_my_commands(client_commands, scope=BotCommandScopeAllPrivateChats())

    if not ADMIN_TELEGRAM_ID:
        return

    admin_commands = [
        BotCommand(command="add", description="Записать клиента"),
        BotCommand(command="bookings", description="Записи: перенести, отменить"),
        BotCommand(command="link", description="Ссылки на встречи"),
        BotCommand(command="help", description="Шпаргалка по командам"),
        BotCommand(command="start", description="Начать сначала"),
        BotCommand(command="cancel", description="Свои записи"),
    ]
    try:
        await bot.set_my_commands(
            admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_TELEGRAM_ID)
        )
    except Exception as e:
        # Психолог мог ещё не открыть чат с ботом — не повод не стартовать
        logging.getLogger(__name__).warning(f"Не удалось задать команды админа: {e}")


async def main():
    await init_db()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # admin роутер первым — его callback-query не должны перехватываться booking роутером
    dp.include_router(admin.router)
    dp.include_router(booking.router)

    await setup_commands(bot)

    # фоновый цикл напоминаний
    asyncio.create_task(reminder_loop(bot))

    print("✅ Booking bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
