import asyncio
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Dict

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

import config
from llm import get_llm
from storage import close_storage, get_storage

os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)

file_handler = RotatingFileHandler(
    config.LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[file_handler, logging.StreamHandler()],
)

# --- Ключи хранилища (префикс сохранён из актуальной версии) ---
LIMIT_KEY = config.REDIS_LIMIT_KEY
COUNT_KEY = config.REDIS_COUNT_KEY
DIALOGS_KEY = config.REDIS_DIALOGS_KEY
HISTORY_PREFIX = config.REDIS_HISTORY_PREFIX
PENDING_PREFIX = config.REDIS_PENDING_PREFIX


# --- Утилиты ---
async def get_dialog_history(storage, dialog_id: str) -> list:
    history_json = await storage.get(f"{HISTORY_PREFIX}:{dialog_id}")
    if history_json:
        return json.loads(history_json)
    return []


async def save_dialog_history(storage, dialog_id: str, messages: list) -> None:
    await storage.set(f"{HISTORY_PREFIX}:{dialog_id}", json.dumps(messages, ensure_ascii=False))


def has_phone_number(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    phone_patterns = [
        r"\+7\s*\(?[0-9]{3}\)?\s*[0-9]{3}[\s-]?[0-9]{2}[\s-]?[0-9]{2}",
        r"8\s*\(?[0-9]{3}\)?\s*[0-9]{3}[\s-]?[0-9]{2}[\s-]?[0-9]{2}",
        r"[0-9]{11}",
        r"[0-9]{10}",
        r"\+7[0-9]{10}",
    ]
    for pattern in phone_patterns:
        if re.search(pattern, text):
            logging.info("Найден номер телефона по шаблону: %s", pattern)
            return True
    return False


async def transfer_to_operator(dialog_id: str, reason: str = "auto") -> bool:
    """Переводит диалог openlines на оператора (USER_ID из config.OPERATOR_ID)."""
    if not config.OPERATOR_ID:
        logging.error(
            "[Диалог %s] OPERATOR_ID не задан, перевод невозможен (причина: %s)",
            dialog_id, reason,
        )
        return False
    try:
        logging.info(
            "[Диалог %s] Инициирован перевод на оператора. Причина: %s", dialog_id, reason
        )
        # Удаляем префикс 'chat' и получаем только числовой ID
        clean_chat_id = dialog_id.lstrip("chat")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                config.BITRIX_WEBHOOK + "imopenlines.bot.session.transfer.json",
                data={"CHAT_ID": clean_chat_id, "USER_ID": config.OPERATOR_ID, "LEAVE": "Y"},
            )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("result"):
                logging.info("[Диалог %s] Успешно переведен на оператора.", dialog_id)
                return True
            logging.warning("[Диалог %s] Ошибка перевода: %s", dialog_id, result)
            return False
        logging.warning(
            "[Диалог %s] HTTP ошибка: %s - %s", dialog_id, resp.status_code, resp.text[:200]
        )
        return False
    except Exception as exc:
        logging.error("[Диалог %s] Исключение при переводе: %s", dialog_id, exc, exc_info=True)
        return False


async def send_to_bitrix(dialog_id: str, message: str) -> None:
    for attempt in range(3):
        try:
            if config.HUMANIZE_MODE:
                await asyncio.sleep(0.5 + random.uniform(0, 1.5))
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    config.BITRIX_WEBHOOK + "imbot.message.add.json",
                    data={
                        "DIALOG_ID": dialog_id,
                        "MESSAGE": message,
                        "BOT_ID": config.BOT_ID,
                        "CLIENT_ID": config.CLIENT_ID,
                    },
                )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("result"):
                    logging.info("[Диалог %s] Ответ отправлен в Bitrix", dialog_id)
                    return
                logging.warning("[Диалог %s] Bitrix отклонил сообщение: %s", dialog_id, result)
            else:
                logging.warning(
                    "[Диалог %s] Bitrix HTTP %s: %s", dialog_id, resp.status_code, resp.text[:200]
                )
        except httpx.TimeoutException:
            logging.warning("[Диалог %s] Bitrix таймаут (попытка %d)", dialog_id, attempt + 1)
        except Exception as exc:
            logging.error("[Диалог %s] Ошибка Bitrix (попытка %d): %s", dialog_id, attempt + 1, exc)
        if attempt < 2:
            wait_time = 2**attempt
            logging.info("[Диалог %s] Ждем %dс перед повтором Bitrix", dialog_id, wait_time)
            await asyncio.sleep(wait_time)
    logging.error("[Диалог %s] Не удалось отправить в Bitrix после 3 попыток", dialog_id)


async def process_messages_safely(storage, dialog_id: str, user_name: str, messages: list) -> None:
    """Обработка пачки текстовых сообщений через LLM.

    При ошибке LLM сообщения возвращаются в очередь и исключение пробрасывается
    воркеру, чтобы обработка повторилась (вместо простой заглушки).
    """
    try:
        dialog_history = await get_dialog_history(storage, dialog_id)
        if len(messages) == 1:
            dialog_history.append({"role": "user", "content": messages[0]})
        elif config.COMBINE_MULTIPLE_MESSAGES:
            combined = "\n".join(f"{i + 1}. {msg}" for i, msg in enumerate(messages))
            dialog_history.append({"role": "user", "content": combined})
        else:
            for m in messages:
                dialog_history.append({"role": "user", "content": m})

        max_messages = config.MAX_HISTORY_PAIRS * 2
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]

        system_prompt = config.PROMPT.replace("{имя}", user_name or "клиент")
        messages_for_gpt = [{"role": "system", "content": system_prompt}] + dialog_history

        logging.info("[Диалог %s] Отправляем в LLM %d сообщений", dialog_id, len(messages))

        # При неуспехе complete() бросает исключение -> попадём в except ниже
        answer = await get_llm().complete(messages_for_gpt)

        dialog_history.append({"role": "assistant", "content": answer})
        if len(dialog_history) > max_messages:
            dialog_history = dialog_history[-max_messages:]
        await save_dialog_history(storage, dialog_id, dialog_history)

        await send_to_bitrix(dialog_id, answer)
        logging.info("[Диалог %s] Бот: %s", dialog_id, answer)
        logging.info("[Диалог %s] Сообщения обработаны успешно", dialog_id)
    except Exception:
        # Возвращаем сообщения в очередь, чтобы воркер повторил обработку
        try:
            for msg in reversed(messages):
                await storage.lpush(f"{PENDING_PREFIX}:{dialog_id}", msg)
            logging.info("[Диалог %s] Сообщения возвращены в очередь", dialog_id)
        except Exception as return_error:
            logging.error("[Диалог %s] Ошибка при возврате сообщений в очередь: %s", dialog_id, return_error)
        raise


async def dialog_worker(storage, dialog_id: str, user_name: str) -> None:
    """Фоновая обработка сообщений диалога со сбором пачек. Завершается после неактивности."""
    loop = asyncio.get_running_loop()
    last_active = loop.time()
    consecutive_errors = 0

    try:
        while True:
            try:
                initial_messages = await storage.fetch_and_clear(f"{PENDING_PREFIX}:{dialog_id}")
                if not initial_messages:
                    if loop.time() - last_active > config.WORKER_TIMEOUT:
                        logging.info("[Диалог %s] Воркер завершён по таймауту", dialog_id)
                        break
                    await asyncio.sleep(config.MESSAGE_CHECK_INTERVAL)
                    continue

                consecutive_errors = 0
                last_active = loop.time()
                current_batch = list(initial_messages)
                logging.info("[Диалог %s] Первая пачка: %d сообщений", dialog_id, len(current_batch))

                # Собираем пачку: ждём окно тишины, пока приходят новые сообщения
                collection_start = time.monotonic()
                absolute_start = time.monotonic()
                while True:
                    await asyncio.sleep(config.MESSAGE_CHECK_INTERVAL)
                    additional = await storage.fetch_and_clear(f"{PENDING_PREFIX}:{dialog_id}")
                    if additional:
                        current_batch.extend(additional)
                        logging.info(
                            "[Диалог %s] Добавлено %d сообщений. Всего: %d",
                            dialog_id, len(additional), len(current_batch),
                        )
                        collection_start = time.monotonic()
                    if (
                        time.monotonic() - collection_start >= config.MESSAGE_COLLECTION_WINDOW
                        or time.monotonic() - absolute_start >= config.MAX_COLLECTION_WINDOW
                    ):
                        break

                await process_messages_safely(storage, dialog_id, user_name, current_batch)
            except asyncio.CancelledError:
                logging.info("[Диалог %s] Воркер отменен", dialog_id)
                break
            except Exception as exc:
                consecutive_errors += 1
                logging.error(
                    "[Диалог %s] Ошибка в воркере (#%d): %s",
                    dialog_id, consecutive_errors, exc, exc_info=True,
                )
                if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    logging.critical("[Диалог %s] Слишком много ошибок подряд, завершаем воркер", dialog_id)
                    break
                wait_time = min(2**consecutive_errors, 60)
                logging.info("[Диалог %s] Ждем %dс перед повтором", dialog_id, wait_time)
                await asyncio.sleep(wait_time)
    finally:
        workers.pop(dialog_id, None)
        async with workers_lock:
            worker_creation_locks.pop(dialog_id, None)
        logging.info("[Диалог %s] Воркер завершён", dialog_id)


workers: Dict[str, asyncio.Task] = {}
workers_lock = asyncio.Lock()
worker_creation_locks: Dict[str, asyncio.Lock] = {}
transfer_tasks: Dict[str, asyncio.Task] = {}
transfer_tasks_lock = asyncio.Lock()


async def ensure_worker_running(storage, dialog_id: str, user_name: str) -> None:
    if dialog_id in workers and not workers[dialog_id].done():
        return
    async with workers_lock:
        if dialog_id not in worker_creation_locks:
            worker_creation_locks[dialog_id] = asyncio.Lock()
        dialog_lock = worker_creation_locks[dialog_id]
    async with dialog_lock:
        if dialog_id not in workers or workers[dialog_id].done():
            if dialog_id in workers:
                workers.pop(dialog_id)
                logging.info("[Диалог %s] Удален завершенный воркер", dialog_id)
            workers[dialog_id] = asyncio.create_task(dialog_worker(storage, dialog_id, user_name))
            logging.info("[Диалог %s] Запущен новый воркер", dialog_id)


async def schedule_transfer(dialog_id: str) -> None:
    """Отложенный перевод диалога на оператора (например, после получения телефона)."""
    delay_seconds = config.PHONE_TRANSFER_DELAY
    try:
        logging.info("[Диалог %s] Запланирован отложенный перевод через %d секунд.", dialog_id, delay_seconds)
        await asyncio.sleep(delay_seconds)
        if await transfer_to_operator(dialog_id, "delayed_transfer"):
            logging.info("[Диалог %s] Отложенный перевод выполнен.", dialog_id)
        else:
            logging.warning("[Диалог %s] Отложенный перевод не удался.", dialog_id)
    except asyncio.CancelledError:
        logging.info("[Диалог %s] Отложенная задача на перевод отменена.", dialog_id)
    except Exception as exc:
        logging.error("[Диалог %s] Ошибка в задаче отложенного перевода: %s", dialog_id, exc, exc_info=True)
    finally:
        async with transfer_tasks_lock:
            transfer_tasks.pop(dialog_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    logging.info("Закрытие воркеров, переводов и хранилища...")
    async with transfer_tasks_lock:
        tasks_to_cancel = list(transfer_tasks.values())
        transfer_tasks.clear()
    async with workers_lock:
        tasks_to_cancel += [t for t in workers.values() if not t.done()]
    for task in tasks_to_cancel:
        task.cancel()
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    await close_storage()
    logging.info("Завершение работы")


app = FastAPI(lifespan=lifespan)


@app.post("/bot")
async def bot_handler(
    request: Request,
    event: str = Form(None),
    dialog_id: str = Form(None, alias="data[PARAMS][DIALOG_ID]"),
    user_message: str = Form(None, alias="data[PARAMS][MESSAGE]"),
    user_name: str = Form(None, alias="data[USER][FIRST_NAME]"),
    message_type: str = Form(None, alias="data[PARAMS][MESSAGE_TYPE]"),
):
    try:
        # fallback на JSON
        if event is None or dialog_id is None or user_message is None:
            try:
                data = await request.json()
                event = data.get("event", event)
                dialog_params = data.get("data", {}).get("PARAMS", {})
                dialog_id = dialog_params.get("DIALOG_ID", dialog_id)
                user_message = dialog_params.get("MESSAGE", user_message)
                message_type = dialog_params.get("MESSAGE_TYPE", message_type)
                user_name = data.get("data", {}).get("USER", {}).get("FIRST_NAME", user_name)
            except Exception as json_error:
                logging.warning("Ошибка парсинга JSON: %s", json_error)

        logging.info(
            "RAW: event=%s, dialog_id=%s, msg=%s, type=%s", event, dialog_id, user_message, message_type
        )

        if not dialog_id:
            logging.error("Нет dialog_id в запросе")
            return JSONResponse({"status": "error", "message": "Missing dialog_id"}, status_code=400)

        if event == "ONIMBOTJOINCHAT" and not user_message:
            logging.info("[Диалог %s] Вход в чат без сообщения. Игнорируем.", dialog_id)
            return JSONResponse({"status": "ignored", "message": "Join event without message"})

        # --- Retry-логика для хранилища (пересоздаём соединение при сбое) ---
        max_retries = 3
        chat_limit = 0
        chat_count = 0

        for attempt in range(max_retries):
            try:
                force_reset = attempt > 0
                r = await get_storage(force_reconnect=force_reset)

                chat_limit = int(await r.get(LIMIT_KEY) or 0)
                chat_count = int(await r.get(COUNT_KEY) or 0)

                # Проверяем: это новый диалог?
                is_new_chat = await r.sadd(DIALOGS_KEY, dialog_id)
                if is_new_chat:
                    chat_count += 1
                    await r.set(COUNT_KEY, chat_count)
                    logging.info("[Диалог %s] Новый диалог! Счетчик: %d", dialog_id, chat_count)
                else:
                    logging.info("[Диалог %s] Существующий диалог, счетчик не изменяется", dialog_id)

                break
            except Exception as redis_err:
                logging.warning("[Storage Retry] Попытка %d/%d не удалась: %s", attempt + 1, max_retries, redis_err)
                if attempt == max_retries - 1:
                    logging.error("Хранилище недоступно после всех попыток.")
                    return JSONResponse({"status": "error", "message": "Storage unavailable"}, status_code=500)
                await asyncio.sleep(0.2)

        if chat_limit > 0 and chat_count > chat_limit:
            logging.info("[Диалог %s] Лимит исчерпан (%d/%d), переводим на оператора.", dialog_id, chat_count, chat_limit)
            if await transfer_to_operator(dialog_id, "limit_reached"):
                return JSONResponse(
                    {"status": "limit_reached", "message": "Диалог переведен на оператора - лимит исчерпан"}
                )
            return JSONResponse({"status": "transfer_failed", "message": "Не удалось перевести на оператора"})

        if chat_limit == 0:
            logging.info("[Диалог %s] Лимиты не установлены, бот работает без ограничений", dialog_id)

        # Фильтр на пустые сообщения
        if event == "ONIMBOTMESSAGEADD" and (not user_message or not user_message.strip()):
            logging.info("[Диалог %s] Пустое сообщение, переводим на оператора.", dialog_id)
            if await transfer_to_operator(dialog_id, "empty_message"):
                return JSONResponse(
                    {"status": "transferred_to_operator", "message": "Пустое сообщение - переведено на оператора"}
                )
            return JSONResponse({"status": "transfer_failed", "message": "Не удалось перевести на оператора"})

        # Отложенный перевод при обнаружении номера телефона
        if has_phone_number(user_message):
            logging.info("[Диалог %s] Обнаружен номер телефона, планируем отложенный перевод.", dialog_id)
            async with transfer_tasks_lock:
                if dialog_id not in transfer_tasks or transfer_tasks[dialog_id].done():
                    transfer_tasks[dialog_id] = asyncio.create_task(schedule_transfer(dialog_id))
                else:
                    logging.info("[Диалог %s] Отложенный перевод уже запланирован.", dialog_id)

        # Добавляем сообщение в очередь и запускаем воркер
        if user_message and user_message.strip():
            try:
                await r.rpush(f"{PENDING_PREFIX}:{dialog_id}", user_message.strip())
                await ensure_worker_running(r, dialog_id, user_name or "клиент")
            except Exception as exc:
                logging.error("[Диалог %s] Ошибка добавления в очередь: %s", dialog_id, exc)
                return JSONResponse({"status": "error", "message": "Failed to queue message"}, status_code=500)
        else:
            logging.info("[Диалог %s] Сообщение пустое или None, пропускаем обработку GPT.", dialog_id)

        return JSONResponse({
            "status": "queued",
            "message": "Сообщение добавлено в очередь на обработку",
            "limits": {
                "current_limit": chat_limit,
                "used": chat_count,
            },
        })
    except Exception as exc:
        logging.error("Ошибка в bot_handler: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)


@app.get("/status")
async def status():
    try:
        r = await get_storage()
        await r.get(LIMIT_KEY)
        redis_status = "connected"
    except Exception:
        redis_status = "disconnected"
    active_workers = sum(1 for task in workers.values() if not task.done())
    completed_workers = sum(1 for task in workers.values() if task.done())
    return {
        "status": "ok",
        "redis": redis_status,
        "workers": {"active": active_workers, "completed": completed_workers, "total": len(workers)},
        "dialogs": list(workers.keys()),
    }


@app.post("/transfer_to_operator")
async def manual_transfer_to_operator(dialog_id: str):
    try:
        if await transfer_to_operator(dialog_id, "manual"):
            return JSONResponse({"status": "success", "message": "Dialog transferred to operator"})
        return JSONResponse({"status": "error", "message": "Failed to transfer dialog"})
    except Exception as exc:
        logging.error("Ошибка при ручном переводе диалога %s: %s", dialog_id, exc, exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)