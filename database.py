import aiosqlite
from config import DB_PATH


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS consents (
                telegram_id INTEGER PRIMARY KEY,
                given_at    TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                telegram_id   INTEGER PRIMARY KEY,
                name          TEXT NOT NULL,
                contact       TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                consent_given INTEGER NOT NULL DEFAULT 0,
                consent_at    TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id      INTEGER NOT NULL,
                name             TEXT NOT NULL,
                contact          TEXT NOT NULL,
                slot_start       TEXT NOT NULL,
                slot_end         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'confirmed',
                google_event_id   TEXT,
                meet_url          TEXT,
                created_at        TEXT NOT NULL,
                reminder_24h_sent INTEGER NOT NULL DEFAULT 0,
                reminder_1h_sent  INTEGER NOT NULL DEFAULT 0,
                reminder_5min_sent INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()


async def create_booking(
    telegram_id: int, name: str, contact: str,
    slot_start: str, slot_end: str,
    google_event_id: str, meet_url: str, created_at: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO bookings
               (telegram_id, name, contact, slot_start, slot_end, google_event_id, meet_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, name, contact, slot_start, slot_end, google_event_id, meet_url, created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_booking(booking_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)) as cur:
            return await cur.fetchone()


async def get_upcoming_bookings():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bookings WHERE status = 'confirmed' AND slot_start > datetime('now') ORDER BY slot_start"
        ) as cur:
            return await cur.fetchall()


async def get_user_bookings(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM bookings
               WHERE telegram_id = ? AND status = 'confirmed' AND slot_start > datetime('now')
               ORDER BY slot_start""",
            (telegram_id,),
        ) as cur:
            return await cur.fetchall()


async def cancel_booking(booking_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        await db.commit()


async def get_pending_reminders():
    """Записи, которым нужно отправить напоминание (ещё не отправляли)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM bookings
               WHERE status = 'confirmed'
               AND slot_start > datetime('now')
               AND (reminder_24h_sent = 0 OR reminder_1h_sent = 0 OR reminder_5min_sent = 0)"""
        ) as cur:
            return await cur.fetchall()


async def mark_reminder_sent(booking_id: int, reminder_type: str):
    field = {"24h": "reminder_24h_sent", "1h": "reminder_1h_sent", "5min": "reminder_5min_sent"}.get(reminder_type, "reminder_1h_sent")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE bookings SET {field} = 1 WHERE id = ?", (booking_id,))
        await db.commit()


async def get_client(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)) as cur:
            return await cur.fetchone()


async def upsert_client(telegram_id: int, name: str, contact: str, updated_at: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO clients (telegram_id, name, contact, updated_at, consent_given, consent_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 name=excluded.name, contact=excluded.contact, updated_at=excluded.updated_at""",
            (telegram_id, name, contact, updated_at, updated_at),
        )
        await db.commit()


async def create_pending_custom(telegram_id: int, name: str, contact: str,
                                requested_dt: str, custom_request: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_custom (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                contact TEXT NOT NULL,
                requested_dt TEXT NOT NULL,
                custom_request TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        cursor = await db.execute(
            "INSERT INTO pending_custom (telegram_id, name, contact, requested_dt, custom_request, created_at) VALUES (?,?,?,?,?,?)",
            (telegram_id, name, contact, requested_dt, custom_request, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_custom(pending_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pending_custom WHERE id = ?", (pending_id,)) as cur:
            return await cur.fetchone()


async def delete_pending_custom(pending_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_custom WHERE id = ?", (pending_id,))
        await db.commit()


async def has_consent(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM consents WHERE telegram_id = ?", (telegram_id,)) as cur:
            return await cur.fetchone() is not None


async def save_consent(telegram_id: int, given_at: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO consents (telegram_id, given_at) VALUES (?, ?)",
            (telegram_id, given_at),
        )
        await db.commit()


async def delete_client(telegram_id: int):
    """Удаляет все данные клиента (право на забвение по ФЗ-152)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM consents WHERE telegram_id = ?", (telegram_id,))
        await db.execute("DELETE FROM clients WHERE telegram_id = ?", (telegram_id,))
        await db.execute(
            "UPDATE bookings SET name='[удалено]', contact='[удалено]' WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
