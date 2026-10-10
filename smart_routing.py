"""
Смарт-маршрутизация для режима GEO Translit -> RU:
    - is_simple_text(): дешёвая эвристика "простой/сложный текст" (без ИИ);
    - translate_via_google(): бесплатный перевод через Google Translate
      (библиотека deep_translator) для простых фраз и как фоллбэк, если
      Gemini недоступен.

Сама оркестрация (что когда вызывать, фоллбэк в обе стороны) — в main.py
(translate_geo_to_ru_smart), так как ей нужен клиент Gemini, который
создаётся там же.
"""

import asyncio
import logging
import re
from functools import lru_cache
from typing import Optional

from transliteration import transliterate_to_mkhedruli

logger = logging.getLogger("georgian_translator_bot.smart_routing")

_SHARED_TEMPLATE_TRANSLATIONS: dict[str, str] = {}
_SHARED_TEMPLATE_TRANSLATIONS_MKHEDRULI: dict[str, str] = {}


def set_shared_library_translations(entries: dict[str, str]) -> None:
    """Обновляет кэш общей библиотеки для всех пользователей."""
    _SHARED_TEMPLATE_TRANSLATIONS.clear()
    _SHARED_TEMPLATE_TRANSLATIONS_MKHEDRULI.clear()

    for raw_key, value in entries.items():
        normalized = _normalize(raw_key)
        if not normalized:
            continue
        _SHARED_TEMPLATE_TRANSLATIONS[normalized] = value
        _SHARED_TEMPLATE_TRANSLATIONS_MKHEDRULI[_normalize(transliterate_to_mkhedruli(raw_key))] = value


async def refresh_shared_library_cache() -> None:
    from db import load_shared_phrases

    entries = await load_shared_phrases()
    set_shared_library_translations(entries)

# --------------------------------------------------------------------------- #
# Оценка сложности текста
# --------------------------------------------------------------------------- #

# Частые бытовые выражения — заданы латиницей (как их печатают пользователи) и
# переведены в мхедрули той же функцией транслитерации, что и в основном
# пайплайне — единый источник правды, исключает ручные опечатки в написании.
_SIMPLE_PHRASES_LATIN = [
    "gamarjoba",
    "gagimarjos",
    "gaumarjos",
    "madloba",
    "didi madloba",
    "gmadlobt",
    "rogor xar",
    "rogor khar",
    "rogor brdzandebit",
    "ras akhali ambavi",
    "ras shvebi",
    "ki",
    "ara",
    "diakh",
    "ho",
    "nakhvamdis",
    "dila mshvidobisa",
    "ghame mshvidobisa",
    "kargad",
    "cudad var",
    "ver gavige",
    "sad khar",
    "sad xar",
]


def _normalize(text: str) -> str:
    """Схлопывает пробелы и обрезает завершающую пунктуацию для сравнения."""
    collapsed = " ".join(text.split())
    return collapsed.strip(" !?.,;:").lower()


_SIMPLE_PHRASES_MKHEDRULI = {_normalize(transliterate_to_mkhedruli(p)) for p in _SIMPLE_PHRASES_LATIN}

# Частые фразы с фиксированным переводом: сначала проверяем их, чтобы
# не делать лишние HTTP-запросы к Google Translate и тем более Gemini.
_TEMPLATE_TRANSLATIONS = {
    "gamarjoba": "Привет",
    "gamarjoba!": "Привет",
    "gamarjobat": "Привет",
    "gagimarjos": "Здравствуйте",
    "gagimarjos!": "Здравствуйте",
    "gaumarjos": "Здравствуйте",
    "madloba": "Спасибо",
    "madlobt": "Спасибо",
    "madloba dzalian": "Большое спасибо",
    "didi madloba": "Большое спасибо",
    "gmadlobt": "Спасибо",
    "rogor xar": "Как дела?",
    "rogor khar": "Как дела?",
    "rogor xar?": "Как дела?",
    "rogor khar?": "Как дела?",
    "rogor brdzandebit": "Как вы?",
    "ras akhali ambavi": "Что нового?",
    "ra akhali ambavi": "Что нового?",
    "ras shvebi": "Что случилось?",
    "ra shvebi": "Что случилось?",
    "ki": "Да",
    "kidev": "Да",
    "ara": "Нет",
    "diakh": "Хорошо / окей",
    "ho": "Да",
    "nakhvamdis": "До свидания",
    "nakhvamdis!": "До свидания",
    "dila mshvidobisa": "Доброе утро",
    "ghame mshvidobisa": "Доброй ночи",
    "sad xar": "Где ты?",
    "sad khar": "Где ты?",
    "sad xar?": "Где ты?",
    "sad khar?": "Где ты?",
    "kargad": "Хорошо",
    "kargad var": "Я в порядке",
    "cudad var": "Мне плохо",
    "ver gavige": "Я не понимаю",
    "ver gavigeb": "Не понимаю",
    "shemodi": "Пожалуйста",
    "mshvidobisa": "Пока",
    "mshvidobisa!": "Пока!",
    "minda": "Я хочу",
    "me var": "Я",
    "me chven": "Мы",
    "minda vicheto": "Хочу узнать",
    "vitsi": "Я устал",
    "vintskhe": "Я голоден",
    "vichuteb": "Я хочу есть",
    "shemchere": "Извините",
    "shemdzli": "Можно?",
    "vidzli": "Я могу",
    "vitsi kargi": "Я хорошо",
    "sualed": "Проблема",
    "sualed?": "Проблема?",
}
_TEMPLATE_TRANSLATIONS_MKHEDRULI = {
    _normalize(transliterate_to_mkhedruli(key)): value for key, value in _TEMPLATE_TRANSLATIONS.items()
}

# Признаки "сложного" текста: даты/номера статей (цифры), официальная
# пунктуация (скобки, кавычки, двоеточие/точка с запятой), составные
# предложения (несколько запятых).
_COMPLEXITY_PUNCTUATION_RE = re.compile(r"[;:()\"«»]")
_DIGIT_RE = re.compile(r"\d")

MAX_SIMPLE_WORD_COUNT = 5


@lru_cache(maxsize=1024)
def is_simple_text(text: str) -> bool:
    """
    Эвристическая (НЕ основанная на ИИ) оценка того, можно ли перевести текст
    бесплатным Google Translate, или нужен Gemini с пониманием контекста.
    Принимает уже сконвертированный в мхедрули текст.

    Простой текст: точное совпадение с частым бытовым выражением, либо
    короткая фраза (до MAX_SIMPLE_WORD_COUNT слов) без цифр, "официальной"
    пунктуации и без множественных запятых (составных предложений).

    Это дешёвая эвристика, а не лингвистический анализ — она может ошибаться
    на неочевидных случаях. Задача — отсеять явно бытовые фразы, чтобы не
    тратить на них квоту Gemini API, а не дать 100%-точную классификацию.
    
    ⚡ Кэшируется с @lru_cache — повторные вызовы одного и того же текста
       мгновенны.
    """
    normalized = _normalize(text)
    if not normalized:
        return False

    if normalized in _SIMPLE_PHRASES_MKHEDRULI:
        return True

    if _DIGIT_RE.search(text):
        return False
    if _COMPLEXITY_PUNCTUATION_RE.search(text):
        return False
    if text.count(",") >= 2:
        return False

    word_count = len(normalized.split())
    return word_count <= MAX_SIMPLE_WORD_COUNT


# --------------------------------------------------------------------------- #
# Google Translate (бесплатно, без ключа API)
# --------------------------------------------------------------------------- #


def translate_from_template(text: str) -> Optional[str]:
    """Возвращает фиксированный перевод для часто встречающихся фраз.

    Сначала проверяем латинский/транслит-вариант пользователя, затем уже
    вариант, переведённый локально в мхедрули, чтобы шаблонные ответы работали
    и для вариаций вроде "rogor xar" / "rogor khar".
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    if normalized in _TEMPLATE_TRANSLATIONS:
        return _TEMPLATE_TRANSLATIONS[normalized]
    if normalized in _SHARED_TEMPLATE_TRANSLATIONS:
        return _SHARED_TEMPLATE_TRANSLATIONS[normalized]

    mkhedruli = _normalize(transliterate_to_mkhedruli(text))
    if mkhedruli in _TEMPLATE_TRANSLATIONS_MKHEDRULI:
        return _TEMPLATE_TRANSLATIONS_MKHEDRULI[mkhedruli]
    if mkhedruli in _SHARED_TEMPLATE_TRANSLATIONS_MKHEDRULI:
        return _SHARED_TEMPLATE_TRANSLATIONS_MKHEDRULI[mkhedruli]

    return None


def _translate_via_google_sync(text: str) -> str:
    # Импорт внутри функции: модуль main.py грузится быстро, а отсутствие
    # библиотеки (если вдруг requirements.txt не установлен полностью)
    # проявится только в момент реального вызова, с понятной ошибкой.
    from deep_translator import GoogleTranslator

    result = GoogleTranslator(source="ka", target="ru").translate(text)
    if not result or not result.strip():
        raise ValueError("Google Translate вернул пустой ответ")
    return result.strip()


async def translate_via_google(text: str) -> str:
    """
    deep_translator делает синхронный HTTP-запрос, поэтому выполняем его в
    отдельном потоке — иначе он заблокирует event loop aiogram на время
    запроса.
    """
    return await asyncio.to_thread(_translate_via_google_sync, text)