"""
Точка входа booking-bot.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_BOT_TOKEN
from database import init_db
from services.notifier import reminder_loop
import handlers.admin as admin
import handlers.booking as booking

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


async def main():
    await init_db()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # admin роутер первым — его callback-query не должны перехватываться booking роутером
    dp.include_router(admin.router)
    dp.include_router(booking.router)

    # фоновый цикл напоминаний
    asyncio.create_task(reminder_loop(bot))

    print("✅ Booking bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
