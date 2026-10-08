"""Интерфейс LLM и его реализации.

- (prod): обращение к OpenAI (напрямую или через прокси-сервис PROXY_SERVICE_URL).
- (local): фиксированная фраза + эхо последнего сообщения пользователя.
"""

import asyncio
import logging
import random
from typing import List, Optional

from openai import OpenAI

import config


class LLMClient:
    async def complete(self, messages: List[dict]) -> str:  # pragma: no cover
        raise NotImplementedError


class OpenAIClient(LLMClient):
    """Клиент OpenAI. Прокси-сервис задаётся через PROXY_SERVICE_URL (base_url)."""

    def __init__(self) -> None:
        self._client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.PROXY_SERVICE_URL,  # None -> официальное API
        )

    async def complete(self, messages: List[dict]) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(config.OPENAI_RETRIES):
            try:
                if config.HUMANIZE_MODE and attempt == 0:
                    # Имитация времени "на обдумывание" живым менеджером
                    await asyncio.sleep(1 + random.uniform(0, 3))

                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: self._client.chat.completions.create(
                            model=config.GPT_MODEL, messages=messages
                        )
                    ),
                    timeout=config.OPENAI_TIMEOUT,
                )
                content = response.choices[0].message.content
                if content:
                    return content
                logging.warning("LLM вернул пустой ответ (попытка %d/%d)", attempt + 1, config.OPENAI_RETRIES)
                last_error = last_error or RuntimeError("LLM вернул пустой ответ")
            except asyncio.TimeoutError as exc:
                logging.warning("LLM таймаут (попытка %d/%d)", attempt + 1, config.OPENAI_RETRIES)
                last_error = exc
            except Exception as exc:
                logging.error("LLM ошибка (попытка %d): %s", attempt + 1, exc)
                last_error = exc

            if attempt < config.OPENAI_RETRIES - 1:
                wait_time = 2**attempt
                logging.info("Ждём %dс перед повтором LLM", wait_time)
                await asyncio.sleep(wait_time)

        raise last_error


class MockClient(LLMClient):
    """Локальный мок: фикс-фраза + эхо последнего сообщения. Без внешних вызовов."""

    FALLBACK = "Ответим вам в ближайшее время"

    async def complete(self, messages: List[dict]) -> str:
        user_messages = [m["content"] for m in messages if m["role"] == "user"]
        last = user_messages[-1] if user_messages else ""
        return f"[Mock] {self.FALLBACK}. Вы написали: {last}"


_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = MockClient() if config.is_local() else OpenAIClient()
    return _llm