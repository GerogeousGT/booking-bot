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

# ─── LLM для парсинга свободного текста ("завтра в 15", "в пятницу утром") ───
# Провайдер переключается одной переменной, см. services/llm_provider.py.
# groq был бесплатным, но 2026-08-17 модель llama-3.1-8b-instant сняли без
# предупреждения — воронка записи молча встала. Теперь основной — proxyapi.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "proxyapi")  # proxyapi | groq
LLM_MODEL: str = os.getenv("LLM_MODEL", "")  # пусто — берётся дефолт провайдера

PROXYAPI_KEY: str = os.getenv("PROXYAPI_KEY", "")
PROXYAPI_BASE_URL: str = os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/openai/v1")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

# ─── Ссылка на видеовстречу ───
# Автогенерация убрана (публичный meet.jit.si в РФ не работает: комната открывается,
# медиа не идёт). Ссылку присылает психолог через бота — она сохраняется на клиенте
# и со второй записи подставляется автоматически.
# За сколько минут до сессии напомнить психологу о встрече (со ссылкой,
# а если ссылки ещё нет — с просьбой прислать):
ADMIN_LINK_PROMPT_MIN: int = int(os.getenv("ADMIN_LINK_PROMPT_MIN", "15"))
# И за сколько минут пнуть повторно, если ссылки всё ещё нет:
ADMIN_LINK_RETRY_MIN: int = int(os.getenv("ADMIN_LINK_RETRY_MIN", "3"))

DB_PATH: str = os.getenv("DB_PATH", "bookings.db")
