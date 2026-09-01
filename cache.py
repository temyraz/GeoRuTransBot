"""
Кэширование переводов с TTL (time-to-live) для ускорения повторных запросов.

Хранит переводы по ключу (mode, tone, original_text) с автоматическим
истечением записей через TTL_SECONDS. Кэш ограничен по размеру.
"""

import asyncio
import hashlib
import time
from typing import NamedTuple, Optional, Tuple

MAX_CACHE_SIZE = 5000  # максимум записей в кэше
TTL_SECONDS = 3600  # кэш живёт 1 час


class CacheEntry(NamedTuple):
    """Запись в кэше с временем истечения."""
    translated: str
    mkhedruli: Optional[str]  # только для режима GEO_RU
    provider: str  # какой провайдер использовался: "google_translate", "gemini" и т.п.
    expires_at: float  # время, когда запись станет невалидной


class TranslationCache:
    """
    Простой в памяти кэш для переводов. Удаляет старые записи по TTL.
    Ключ: (mode, tone, original_text) или хэш для больших текстов.
    """

    def __init__(self):
        self._cache: dict = {}
        self._access_order: list = []  # для удаления старейшей записи при переполнении

    def _make_key(self, mode: str, tone: str, text: str) -> str:
        """Создаёт ключ кэша, используя хэш для больших текстов."""
        key_base = f"{mode}:{tone}:{text}"
        # Для текстов > 100 символов используем хэш, чтобы не раздувать память
        if len(key_base) > 100:
            return hashlib.md5(key_base.encode()).hexdigest()
        return key_base

    def _cleanup_expired(self) -> None:
        """Удаляет истёкшие записи."""
        current_time = time.time()
        expired_keys = [k for k, v in self._cache.items() if v.expires_at < current_time]
        for k in expired_keys:
            del self._cache[k]
            if k in self._access_order:
                self._access_order.remove(k)

    def get(self, mode: str, tone: str, text: str) -> Optional[Tuple[str, Optional[str], str]]:
        """
        Получает перевод из кэша, если он существует и не истёк.
        Возвращает (translated, mkhedruli, provider) или None.
        """
        self._cleanup_expired()
        key = self._make_key(mode, tone, text)
        entry = self._cache.get(key)
        if entry is None or entry.expires_at < time.time():
            return None
        return entry.translated, entry.mkhedruli, entry.provider

    def set(self, mode: str, tone: str, text: str, translated: str, 
            mkhedruli: Optional[str], provider: str) -> None:
        """Сохраняет перевод в кэш с TTL."""
        self._cleanup_expired()
        key = self._make_key(mode, tone, text)

        # Удаляем старейшую запись, если кэш переполнен
        if len(self._cache) >= MAX_CACHE_SIZE and key not in self._cache:
            if self._access_order:
                oldest_key = self._access_order.pop(0)
                del self._cache[oldest_key]

        expires_at = time.time() + TTL_SECONDS
        self._cache[key] = CacheEntry(translated, mkhedruli, provider, expires_at)

        # Отслеживаем порядок доступа для удаления старейших при переполнении
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

    def clear(self) -> None:
        """Полностью очищает кэш."""
        self._cache.clear()
        self._access_order.clear()


# Глобальный экземпляр кэша
_translation_cache = TranslationCache()


def get_cached_translation(mode: str, tone: str, text: str) -> Optional[Tuple[str, Optional[str], str]]:
    """Получает перевод из глобального кэша."""
    return _translation_cache.get(mode, tone, text)


def cache_translation(mode: str, tone: str, text: str, translated: str, 
                     mkhedruli: Optional[str], provider: str) -> None:
    """Сохраняет перевод в глобальный кэш."""
    _translation_cache.set(mode, tone, text, translated, mkhedruli, provider)
