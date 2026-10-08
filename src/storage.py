"""Хранилище диалогов.
- APP_ENV=prod  -> RedisStorage
- APP_ENV=local -> FileStorage
"""

import asyncio
import json
import os
from typing import List, Optional

import redis.asyncio as aioredis

import config


class Storage:

    async def get(self, key: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    async def set(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def rpush(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def lpush(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def fetch_and_clear(self, key: str) -> List[str]:  # pragma: no cover
        """Атомарно читает список и удаляет его."""
        raise NotImplementedError

    async def sadd(self, key: str, value: str) -> int:  # pragma: no cover
        """Добавляет элемент в множество, возвращает 1, если добавлен впервые."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def reset(self) -> None:  # pragma: no cover
        """Пересоздаёт соединение (для retry-логики в bot_handler)."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class RedisStorage(Storage):
    """Redis-хранилище (продуктовый режим)."""

    # Атомарное чтение и очистка очереди (Lua-скрипт, без гонок)
    _FETCH_AND_CLEAR = """
local msgs = redis.call('LRANGE', KEYS[1], 0, -1)
if #msgs > 0 then
    redis.call('DEL', KEYS[1])
end
return msgs
"""

    def __init__(self) -> None:
        self._client = aioredis.Redis(
            host=config.REDIS_HOST,
            port=int(config.REDIS_PORT),
            db=0,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )

    async def get(self, key: str) -> Optional[str]:
        return await self._client.get(key)

    async def set(self, key: str, value: str) -> None:
        await self._client.set(key, value)

    async def rpush(self, key: str, value: str) -> None:
        await self._client.rpush(key, value)

    async def lpush(self, key: str, value: str) -> None:
        await self._client.lpush(key, value)

    async def fetch_and_clear(self, key: str) -> List[str]:
        return await self._client.eval(self._FETCH_AND_CLEAR, 1, key)

    async def sadd(self, key: str, value: str) -> int:
        return await self._client.sadd(key, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def reset(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = aioredis.Redis(
            host=config.REDIS_HOST,
            port=int(config.REDIS_PORT),
            db=0,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )

    async def close(self) -> None:
        await self._client.aclose()


class FileStorage(Storage):
    """Файловое хранилище (локальный режим) с той же логикой, что и Redis."""

    def __init__(self, path: str = config.STORAGE_FILE) -> None:
        self.path = path
        self._data: dict = {"strings": {}, "lists": {}, "sets": {}}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            self._save()
        for section in ("strings", "lists", "sets"):
            self._data.setdefault(section, {})

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    async def get(self, key: str) -> Optional[str]:
        return self._data["strings"].get(key)

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._data["strings"][key] = value
            self._save()

    async def rpush(self, key: str, value: str) -> None:
        async with self._lock:
            self._data["lists"].setdefault(key, []).append(value)
            self._save()

    async def lpush(self, key: str, value: str) -> None:
        async with self._lock:
            self._data["lists"].setdefault(key, []).insert(0, value)
            self._save()

    async def fetch_and_clear(self, key: str) -> List[str]:
        async with self._lock:
            items = self._data["lists"].pop(key, [])
            self._save()
            return items

    async def sadd(self, key: str, value: str) -> int:
        async with self._lock:
            members = self._data["sets"].setdefault(key, [])
            if value in members:
                return 0
            members.append(value)
            self._save()
            return 1

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data["strings"].pop(key, None)
            self._data["lists"].pop(key, None)
            self._data["sets"].pop(key, None)
            self._save()

    async def close(self) -> None:
        pass


_storage: Optional[Storage] = None
_storage_lock = asyncio.Lock()


async def get_storage(force_reconnect: bool = False) -> Storage:
    """Возвращает синглтон-хранилище. Если force_reconnect=True — пересоздаёт."""
    global _storage
    async with _storage_lock:
        if force_reconnect and _storage is not None:
            # Пересоздаём соединение (для retry-логики в bot_handler)
            try:
                await _storage.reset()
            except Exception:
                _storage = None
        if _storage is None:
            _storage = FileStorage() if config.is_local() else RedisStorage()
        return _storage


async def close_storage() -> None:
    global _storage
    if _storage is not None:
        await _storage.close()
        _storage = None