"""
Дедупликация параллельных запросов к переводчикам.

Если несколько пользователей отправляют одинаковый текст одновременно,
выполняется только один API-запрос, а остальные пользователи дожидаются
результата и используют его. Для небольшого сервера важно также не
накапливать бесконечный кэш результатов — поэтому есть TTL и контроль размера.
"""

import asyncio
import time
from collections import OrderedDict
from typing import Optional, Tuple


class RequestDeduplicator:
    """Ограниченный TTL-кэш для параллельных запросов."""

    def __init__(self, ttl_seconds: float = 30.0, max_items: int = 2048):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._in_flight: dict[str, asyncio.Event] = {}
        self._results: dict[str, Tuple[str, Optional[str], str]] = {}
        self._expires_at: dict[str, float] = {}
        self._order: OrderedDict[str, None] = OrderedDict()

    def _make_key(self, mode: str, tone: str, text: str) -> str:
        return f"{mode}:{tone}:{text}"

    def _cleanup(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        expired_keys = [k for k, expires in self._expires_at.items() if expires <= now]
        for key in expired_keys:
            self._results.pop(key, None)
            self._expires_at.pop(key, None)
            self._order.pop(key, None)

        while len(self._results) > self.max_items:
            oldest_key, _ = self._order.popitem(last=False)
            self._results.pop(oldest_key, None)
            self._expires_at.pop(oldest_key, None)

    async def wait_or_execute(
        self,
        mode: str,
        tone: str,
        text: str,
        execute_func,
    ) -> Tuple[str, Optional[str], str]:
        key = self._make_key(mode, tone, text)
        self._cleanup()

        if key in self._results:
            self._order.move_to_end(key)
            return self._results[key]

        if key in self._in_flight:
            event = self._in_flight[key]
            await event.wait()
            self._cleanup()
            return self._results[key]

        event = asyncio.Event()
        self._in_flight[key] = event

        try:
            result = await execute_func()
            self._results[key] = result
            self._expires_at[key] = time.monotonic() + self.ttl_seconds
            self._order[key] = None
            self._order.move_to_end(key)
            self._cleanup()
            event.set()
            return result
        except Exception:
            self._in_flight.pop(key, None)
            raise
        finally:
            self._in_flight.pop(key, None)
            event.set()


# Глобальный экземпляр дедупликатора
_deduplicator = RequestDeduplicator(ttl_seconds=30.0, max_items=2048)


async def deduplicate_translation_request(
    mode: str,
    tone: str,
    text: str,
    execute_func,
) -> Tuple[str, Optional[str], str]:
    """Использует дедупликатор для параллельных запросов."""
    return await _deduplicator.wait_or_execute(mode, tone, text, execute_func)
