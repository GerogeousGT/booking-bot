# booking-bot

Telegram-бот для онлайн-записи на консультацию. Заменяет форму на сайте и ручную переписку: клиент пишет в свободной форме — бот понимает, предлагает слоты, создаёт событие в календаре и присылает ссылку на видеозвонок. Специалист получает уведомление и может управлять расписанием прямо из Telegram.

Работает в продакшне на реальном трафике.

## Возможности

**Для клиента:**
- Запись в свободной форме на русском: "в понедельник после обеда", "через 2 недели в 11", "есть ли окна в среду?"
- Выбор слота из доступных в Google Calendar
- Подтверждение записи; ссылку на видеовстречу присылает психолог через бота — она сохраняется на клиенте и со второй записи подставляется автоматически
- Напоминания за 24 часа, 1 час и 5 минут до сессии
- Перенос и отмена записи
- Запоминание данных — при повторной записи не нужно вводить имя снова
- Соответствие ФЗ-152: экран согласия, /privacy, /deletedata

**Для специалиста:**
- Уведомление о каждой новой записи
- Умный хендлер расписания: "что у меня завтра?", "записи на следующей неделе", "есть ли кто в пятницу?"
- Нестандартные запросы (вне рабочих часов) — три варианта: принять как есть,
  записать на другое время (если договорились голосом), отклонить
- `/add` — записать клиента самому: выбрать из базы, назвать время свободным текстом,
  выбрать слот. Клиенту уходит уведомление, запись попадает в календарь
- Можно записать вне рабочих часов и в выходные — бот пометит «вне расписания»
- `/link` — управление ссылками на встречи: отправить, прислать повторно, заменить
- `/bookings` — полный список предстоящих сессий

Записать можно только того, кто хоть раз писал боту: Bot API не даёт найти
пользователя по @username и не позволяет боту написать первым.

## Расписание

- Пн–Пт, 9:00–18:00 МСК
- Слоты: 55 минут, на начало часа (9:00, 10:00, … 17:00)
- Минимум за 2 часа до начала
- Записи на 7 дней вперёд

## Стек

- [aiogram 3.x](https://docs.aiogram.dev/) — Telegram Bot API
- LLM для парсинга свободного текста — ProxyAPI (`gpt-4o-mini`) по умолчанию, Groq как запасной
  вариант; провайдер переключается через `LLM_PROVIDER` в `.env` (см. `services/llm_provider.py`).
  Сначала работает детерминированный фолбэк-парсер, LLM подключается только на нестандартных формулировках
- [Google Calendar API](https://developers.google.com/calendar) — Service Account, без OAuth
- [aiosqlite](https://aiosqlite.omnilib.dev/) — SQLite: bookings, clients, consents, pending_custom
- Видеосервис не зашит в код: психолог создаёт звонок где угодно (VK Звонки, Контур.Толк,
  СберДжаз) и отправляет ссылку командой `/link` или по запросу бота перед сессией.
  Автогенерация `meet.jit.si` убрана — публичный инстанс в РФ не пропускает медиа

## Структура

```
booking-bot/
├── bot.py                  # точка входа, регистрация хендлеров, фоновый цикл напоминаний
├── config.py               # настройки из .env
├── database.py             # SQLite: создание/получение/отмена записей, напоминания
├── requirements.txt
├── .env / .env.example
├── service_account.json    # Google Service Account (не в git!)
├── handlers/
│   ├── booking.py          # FSM-флоу: время → слот → имя → контакт → подтверждение
│   └── admin.py            # /add, /link, /bookings, умный хендлер, нестандартное время
├── services/
│   ├── calendar_service.py # Google Calendar API: freebusy + create + delete
│   ├── slot_finder.py      # генерация свободных слотов с учётом рабочих часов
│   ├── date_parser.py      # парсинг свободного текста → datetime (фолбэк + LLM)
│   ├── llm_provider.py     # единая точка выбора LLM-провайдера
│   ├── availability.py     # свободные слоты поверх Google Calendar
│   └── notifier.py         # asyncio-цикл напоминаний (24h / 1h / 5min)
└── tests/
    ├── test_slot_finder.py
    └── test_date_parser.py
```

## Быстрый старт

```bash
git clone <repo>
cd booking-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# заполнить .env: токен бота, GROQ_API_KEY, ADMIN_TELEGRAM_ID, GOOGLE_CALENDAR_ID
python bot.py
```

## Деплой на VPS (Ubuntu)

```bash
# Залить файлы
scp -r booking-bot/ vps:/home/deploy/bots/

# На VPS
cd /home/deploy/bots/booking-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
sudo systemctl start booking-bot
```

## Systemd-сервис

Файл: `/etc/systemd/system/booking-bot.service`

```ini
[Unit]
Description=Booking Bot for Psychology Consultations
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/home/deploy/bots/booking-bot
EnvironmentFile=/home/deploy/bots/booking-bot/.env
ExecStart=/home/deploy/bots/booking-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Google Calendar

Service Account (не OAuth) — без авторизации через браузер.  
`service_account.json` лежит в корне проекта, в git не входит.  
Календарь должен быть расшарен на email service account с правами "Вносить изменения в мероприятия".
