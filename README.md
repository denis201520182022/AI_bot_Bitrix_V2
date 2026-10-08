# ИИ-ассистент для открытых линий Bitrix24

Чат-бот, который принимает сообщения из диалогов Bitrix24 (в том числе из открытых линий Авито/сайта), формирует ответ через LLM и отправляет его обратно в чат. Контекст диалогов хранится в хранилище.

## Возможности

- **Сбор пачки сообщений**: бот ждёт, пока клиент допишет, и отвечает на всё сразу.
- **Имитация живого менеджера**: задержки «на обдумывание», объединение сообщений (HUMANIZE).
- **Лимиты диалогов** с авто-переводом на оператора при исчерпании.
- **Обнаружение номера телефона** → отложенный перевод диалога оператору.
- **Перевод на оператора**: по лимиту, на пустом сообщении, вручную через API.
- **Устойчивость**: retry-логика хранилища; при ошибке LLM сообщения возвращаются в очередь для повторной обработки (а не «сгорают» с заглушкой).

## Режимы работы

Переключение задаётся переменной `APP_ENV` в `.env`.

| Режим | `APP_ENV` | LLM | Хранилище |
|-------|-----------|-----|-----------|
| local | `local` | Mock (фикс-фраза + эхо) | JSON-файл `data/storage.json` |
| prod  | `prod`  | OpenAI GPT | Redis |

Для локальной разработки не нужны ни Docker, ни Redis, ни ключ OpenAI.

## Структура

```
└── src/                  # Исходный код
    ├── bot.py            # FastAPI-приложение, воркеры диалогов, лимиты, переводы
    ├── config.py         # Конфигурация из .env
    ├── storage.py        # Абстракция хранилища: Redis (prod) / JSON-файл (local)
    ├── llm.py            # Абстракция LLM: OpenAI (prod) / Mock (local)
    ├── prompt.txt        # Системный промпт (в git не входит, см. ниже)
    └── __init__.py
├── logs/                 # Файлы логов (локальный режим)
├── data/                 # Хранилище диалогов (локальный режим)
├── Dockerfile
├── docker-compose.yml    # Сервисы bot + redis
├── requirements.txt
└── .env.example          # Шаблон конфигурации
```

> **Промпт:** `src/prompt.txt` содержит бизнес-данные и не публикуется в репозитории. Создайте файл локально (шаблон — любой текст с плейсхолдером `{имя}` для обращения к клиенту). Без файла используется встроенный базовый промпт.

## Настройка

1. Скопируйте шаблон:
   ```bash
   cp .env.example .env
   ```

2. Заполните `.env`:

   | Переменная | Назначение |
   |------------|------------|
   | `APP_ENV` | `local` или `prod` |
   | `OPENAI_API_KEY` | Ключ OpenAI (только для `prod`) |
   | `GPT_MODEL` | Модель, по умолчанию `gpt-4o-mini` |
   | `PROXY_SERVICE_URL` | base_url прокси-сервиса OpenAI (пусто — официальное API) |
   | `BITRIX_WEBHOOK` | Вебхук Bitrix24 |
   | `BOT_ID` / `CLIENT_ID` | ID бота и клиента в Bitrix24 |
   | `OPERATOR_ID` | ID оператора для перевода (пусто — перевод отключен) |
   | `PROMPT_FILE` | Путь к промпту, по умолчанию `src/prompt.txt` |
   | `STORAGE_FILE` | Файл-хранилище (`local`), по умолчанию `data/storage.json` |
   | `REDIS_HOST` / `REDIS_PORT` | Адрес Redis (`prod`) |
   | `LOG_FILE` | Путь к логу, по умолчанию `logs/bot.log`; в Docker `/app/logs/bot.log` |
   | `MESSAGE_COLLECTION_WINDOW` | Окно тишины для сбора пачки, сек (20) |
   | `MAX_COLLECTION_WINDOW` | Максимум ожидания пачки, сек (60) |
   | `MESSAGE_CHECK_INTERVAL` | Интервал опроса очереди, сек (0.5) |
   | `COMBINE_MULTIPLE_MESSAGES` | Объединять пачку в одно сообщение (`true`/`false`) |
   | `HUMANIZE_MODE` | Имитация живого менеджера (`true`/`false`) |
   | `OPENAI_TIMEOUT` | Сколько секунд ждать ответ GPT (60) |
   | `OPENAI_RETRIES` | Число повторных запросов к GPT (3) |
   | `PHONE_TRANSFER_DELAY` | Отложенный перевод после телефона, сек (1800) |

## Запуск

### Локально (без Docker)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# в .env задайте APP_ENV=local (по умолчанию)
uvicorn bot:app --app-dir src --host 0.0.0.0 --port 8000
```

### Прод (Docker Compose)

```bash
# APP_ENV=prod и заполненный .env
docker-compose up --build -d
```

Redis доступен только внутри сети compose, наружу порты не публикуются.

## Настройка Bitrix24 и Авито

### Bitrix24

1. Создайте входящий вебхук: **Разработчикам → Другое → Входящий вебхук**. Скопируйте URL вида `https://your-portal.bitrix24.ru/rest/USER_ID/KEY/` в `BITRIX_WEBHOOK`. Права: `imbot`, `imopenlines`.
2. Зарегистрируйте чат-бота (метод `imbot.register` или приложение) — получите `BOT_ID` и `CLIENT_ID`.
3. Подпишите бота на событие `ONIMBOTMESSAGEADD` — оно должно отправляться POST-запросом на URL вашего сервера: `http://your-server:8000/bot` (обработчик `/bot`).
4. Подключите открытую линию (openlines): сообщения Авито/сайта приходят в неё, бот отвечает в чате, а `imopenlines.bot.session.transfer` переводит диалог на оператора (`OPERATOR_ID` — пользователь Bitrix24).
5. Пропишите в `.env`: `BITRIX_WEBHOOK`, `BOT_ID`, `CLIENT_ID`, `OPERATOR_ID`.

### Авито

1. В личном кабинете Авито подключите интеграцию с Bitrix24 (Авито Pro → API-интеграции).
2. Убедитесь, что сообщения с Авито приходят в открытую линию Bitrix24 — дальше их обрабатывает бот.

## Мониторинг и логи

```bash
# Логи бота (в Docker — на хосте через volume)
tail -f logs/bot.log

# HTTP-статус бота
curl http://localhost:8000/status

# Redis (prod, внутри контейнера)
docker exec redis_chat redis-cli ping          # PONG
docker exec redis_chat redis-cli dbsize        # число ключей
docker exec redis_chat redis-cli get bot:bsk_ai_2:limit

# Логи контейнера
docker logs -f gpt_avito_bitrix_bot
```

## Troubleshooting

**Бот не отвечает**
1. `curl http://localhost:8000/status` — доступность и воркеры
2. `tail -f logs/bot.log` — ошибки обработки
3. Проверьте Redis: `docker exec redis_chat redis-cli ping`
4. Проверьте, что событие `ONIMBOTMESSAGEADD` доходит до `/bot` (в логе строка `RAW: event=...`)

**Ошибки OpenAI** — проверьте `OPENAI_API_KEY`, кредиты аккаунта, rate limits и `PROXY_SERVICE_URL` (если используется прокси). При устойчивой ошибке LLM сообщения возвращаются в очередь и обработка повторяется воркером.

**Проблемы с Bitrix24** — проверьте вебхук, активность бота, права (`imbot`, `imopenlines`) и корректность `OPERATOR_ID`.

**Сброс хранилища (осторожно, удалит историю и лимиты)**
```bash
docker exec redis_chat redis-cli flushdb
```

## Деплой без Docker (systemd, альтернатива)

Чтобы использовать локальный режим без Docker, сначала установите Redis нативно (для `prod`) либо используйте `APP_ENV=local` (JSON-файл):

```bash
sudo apt update
sudo apt install redis-server -y
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping   # PONG
```

Пример юнита `/etc/systemd/system/gpt-bot.service`:

```ini
[Unit]
Description=GPT bot for Bitrix24 openlines
After=network.target redis-server.service
Requires=redis-server.service

[Service]
User=root
WorkingDirectory=/opt/gpt_Avito_Bitrix_bot_2
ExecStart=/opt/gpt_Avito_Bitrix_bot_2/venv/bin/uvicorn bot:app --app-dir src --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

> **Важно для деплоя без Docker:** в `.env` задайте `APP_ENV=prod`, `REDIS_HOST=127.0.0.1` (дефолт `redis` рассчитан на сеть compose), `OPERATOR_ID` и все ключи.

После изменений:

```bash
sudo systemctl daemon-reload
sudo systemctl restart gpt-bot.service
sudo systemctl status gpt-bot.service
sudo journalctl -u gpt-bot.service -f
```

## Тестирование

Отправьте сообщение боту:

```bash
curl -X POST http://localhost:8000/bot \
  -F 'event=test' \
  -F 'data[PARAMS][DIALOG_ID]=123' \
  -F 'data[PARAMS][MESSAGE]=Привет' \
  -F 'data[USER][FIRST_NAME]=Денис'
```

В режиме `local` ответ будет сформирован моком (без обращения к OpenAI).

## API

### `POST /bot`

Принимает событие из Bitrix24 (form-data или JSON). Ведёт очередь сообщений и запускает фоновую обработку диалога. При исчерпании лимита или пустом сообщении — переводит диалог на оператора.

### `GET /status`

Состояние хранилища и активных воркеров.

### `POST /transfer_to_operator?dialog_id=chat...`

Ручной перевод указанного диалога на оператора.

## Ключи хранилища

Поле истории: `bot:bsk_ai_2:history:<dialog_id>` (список сообщений в формате OpenAI). Очередь: `bot:bsk_ai_2:pending:<dialog_id>`. Лимиты: `bot:bsk_ai_2:limit`, `bot:bsk_ai_2:count`, `bot:bsk_ai_2:dialogs`.