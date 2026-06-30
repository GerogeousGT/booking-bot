import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

GOOGLE_SERVICE_ACCOUNT_FILE: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_CALENDAR_ID: str = os.getenv("GOOGLE_CALENDAR_ID", "")

TIMEZONE: str = "Europe/Moscow"
WORK_HOUR_START: int = 9   # первый возможный слот
WORK_HOUR_END: int = 18    # после этого часа слотов нет (последний слот 17:00)

SLOT_DURATION_MIN: int = 55
MIN_HOURS_BEFORE: int = 2  # минимальный буфер до записи
SLOTS_DAYS_AHEAD: int = 7  # показываем слоты на 7 дней вперёд

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

# Постоянная ссылка для видеоконсультаций
MEET_URL: str = os.getenv("MEET_URL", "")

DB_PATH: str = os.getenv("DB_PATH", "bookings.db")
