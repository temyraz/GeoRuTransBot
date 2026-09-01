"""
Дедупликация параллельных запросов к переводчикам.

Если несколько пользователей отправляют одинаковый текст одновременно,
выполняется только один API-запрос, а остальные пользователи дожидаются
результата и используют его. Это снижает нагрузку на Gemini и Google API.
"""

import asyncio
from typing import Optional, Tuple


class RequestDeduplicator:
    """
    Отслеживает текущие (в полёте) запросы и позволяет нескольким корутинам
    дожидаться одного и того же результата без дублирования запроса.
    """

    def __init__(self):
        # Ключ: (mode, tone, original_text)
        # Значение: asyncio.Event, который сигнализирует о готовности результата
        self._in_flight: dict[str, asyncio.Event] = {}
        # Ключ: (mode, tone, original_text)
        # Значение: результат перевода (translated, mkhedruli, provider)
        self._results: dict[str, Tuple[str, Optional[str], str]] = {}

    def _make_key(self, mode: str, tone: str, text: str) -> str:
        """Создаёт уникальный ключ для комбинации параметров."""
        # Для больших текстов можно использовать хэш, но для простоты
        # и учитывая, что обычно тексты небольшие, используем прямую конкатенацию
        return f"{mode}:{tone}:{text}"

    async def wait_or_execute(
        self,
        mode: str,
        tone: str,
        text: str,
        execute_func,  # async callable, возвращает (translated, mkhedruli, provider)
    ) -> Tuple[str, Optional[str], str]:
        """
        Если этот запрос уже в полёте, дожидаемся результата.
        Если нет, выполняем execute_func и сообщаем остальным о результате.
        """
        key = self._make_key(mode, tone, text)

        # Проверяем, может быть уже есть результат
        if key in self._results:
            return self._results[key]

        # Проверяем, может быть запрос уже в полёте
        if key in self._in_flight:
            event = self._in_flight[key]
            await event.wait()
            return self._results[key]

        # Это первый запрос с таким параметром — выполняем его
        event = asyncio.Event()
        self._in_flight[key] = event

        try:
            result = await execute_func()
            self._results[key] = result
            event.set()
            return result
        except Exception:
            # При ошибке удаляем ключ из in_flight, чтобы следующий
            # запрос мог попытаться снова
            del self._in_flight[key]
            raise
        finally:
            # Очищаем in_flight после завершения
            self._in_flight.pop(key, None)


# Глобальный экземпляр дедупликатора
_deduplicator = RequestDeduplicator()


async def deduplicate_translation_request(
    mode: str,
    tone: str,
    text: str,
    execute_func,
) -> Tuple[str, Optional[str], str]:
    """
    Использует дедупликатор для параллельных запросов.
    """
    return await _deduplicator.wait_or_execute(mode, tone, text, execute_func)
