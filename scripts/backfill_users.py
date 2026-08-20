"""
Разовый бэкфилл таблицы known_users.

Зачем: до 2026-08-20 бот не запоминал тех, кто стартовал его и дал согласие, но
ни разу не довёл запись до конца. Такие люди не появлялись в списке /add, хотя
психологу нужно было записать именно их.

Имя и username восстанавливаются через getChat по telegram_id — Bot API отдаёт их
для пользователей, которые с ботом взаимодействовали (искать по @username он при
этом по-прежнему не умеет).

Запуск:  .venv/bin/python scripts/backfill_users.py
Идемпотентен, можно гонять повторно — заодно обновит сменившиеся username.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402
from aiogram import Bot  # noqa: E402

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TIMEZONE  # noqa: E402
from database import init_db, upsert_known_user  # noqa: E402

MOSCOW_TZ = pytz.timezone(TIMEZONE)


async def _ids_to_backfill() -> list[int]:
    """Все, о ком мы знаем: дали согласие или уже были клиентами."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT telegram_id FROM consents
               UNION
               SELECT telegram_id FROM clients"""
        ) as cur:
            return [row[0] for row in await cur.fetchall()]


async def main() -> None:
    await init_db()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    ids = await _ids_to_backfill()
    print(f"Найдено {len(ids)} известных telegram_id")

    ok = failed = 0
    try:
        for telegram_id in ids:
            try:
                chat = await bot.get_chat(telegram_id)
            except Exception as e:
                print(f"  {telegram_id}: не удалось получить — {e}")
                failed += 1
                continue

            await upsert_known_user(
                telegram_id=telegram_id,
                username=chat.username or "",
                first_name=chat.first_name or "",
                seen_at=datetime.now(MOSCOW_TZ).isoformat(),
            )
            label = f"@{chat.username}" if chat.username else "без username"
            print(f"  {telegram_id}: {chat.first_name or '—'} ({label})")
            ok += 1
    finally:
        await bot.session.close()

    print(f"\nЗаписано: {ok}, не удалось: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
