"""
Telegram-бот для перевода между грузинским чат-транслитом, русским языком
и грузинским алфавитом (мхедрули).

Функционал:
    - 3 режима перевода (/settings): GEO Translit -> RU, RU -> GEO Translit,
      GEO Translit -> ქართული (мхедрули).
    - Тональность перевода (/settings): официальная / разговорная.
    - Режим GEO Translit -> RU использует двухэтапную обработку со смарт-
      маршрутизацией (см. ниже) вместо прямого вызова Gemini.
    - Кнопка "🇬🇪 Исходный текст (Мхедрули)" под переводом (режим 1) —
      мгновенно показывает исходный текст, сконвертированный в мхедрули
      локально (без обращения к какому-либо API).
    - Кнопка "🔍 Разбор" (режим 1) — короткий разбор тона, нюансов и ключевых
      слов фразы.
    - Кнопка "⭐ Сохранить" под любым переводом — сохраняет пару текстов в
      личный словарь пользователя (SQLite). /dictionary — просмотр с
      пагинацией и удалением записей.
    - /alphabet — статичная шпаргалка по "сложным" буквосочетаниям транслита
      (без обращения к Gemini).

Смарт-маршрутизация для GEO Translit -> RU (translate_geo_to_ru_smart ниже):
    Этап 1. Локальная транслитерация латиницы в мхедрули
            (transliteration.transliterate_to_mkhedruli) — без ИИ.
    Этап 2. Оценка сложности текста (smart_routing.is_simple_text) — эвристика,
            не ИИ.
    Этап 3. Выбор провайдера перевода:
            - простой текст -> бесплатный Google Translate (deep_translator);
            - сложный текст -> Gemini (лучше справляется с контекстом,
              сленгом, неоднозначностями транслита и опечатками).
            Если выбранный провайдер недоступен (ошибка сети, лимит 429 и
            т.п.) — автоматический фоллбэк на другой провайдер, в обе стороны.

Код разбит на модули:
    - prompts.py         — тексты системных промтов и шпаргалка
    - modes.py            — конфигурация режимов/тональности, сборка промта
    - transliteration.py  — локальный конвертер латиница -> мхедрули
    - smart_routing.py    — is_simple_text + перевод через Google Translate
    - db.py               — SQLAlchemy + SQLite, таблица vocabulary
    - keyboards.py        — общие клавиатуры (меню, /settings, кнопки под переводом)
    - vocabulary.py       — отрисовка страниц личного словаря
    - main.py (этот файл) — хендлеры aiogram, оркестрация пайплайна, точка входа

Стек: aiogram 3.x, google-genai (Gemini API), deep_translator (Google
Translate), SQLAlchemy (async) + aiosqlite.

Запуск:
    python main.py

Переменные окружения (см. .env.example):
    TELEGRAM_BOT_TOKEN — токен бота, выданный @BotFather
    GEMINI_API_KEY     — ключ Gemini API (https://aistudio.google.com/apikey)
    DATABASE_PATH       — необязательно, путь к файлу SQLite (по умолчанию bot.db)
"""

import asyncio
import html
import logging
import os
import re
import sys
from collections import OrderedDict
from typing import Dict, NamedTuple, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import db
from keyboards import (
    EXPLAIN_CALLBACK_DATA,
    MENU_BUTTON_ALPHABET,
    MENU_BUTTON_DICTIONARY,
    MENU_BUTTON_SETTINGS,
    MHEDRULI_CALLBACK_DATA,
    MODE_CALLBACK_PREFIX,
    NOOP_CALLBACK_DATA,
    SAVE_CALLBACK_DATA,
    TONE_CALLBACK_PREFIX,
    build_main_menu_keyboard,
    build_settings_keyboard,
    build_translation_keyboard,
    render_settings_text,
    replace_button,
)
from modes import DEFAULT_MODE, DEFAULT_TONE, MODE_GEO_RU, MODES, TONES, build_system_prompt
from prompts import ALPHABET_CHEATSHEET_HTML, EXPLAIN_PROMPT
from smart_routing import is_simple_text, translate_via_google
from transliteration import transliterate_to_mkhedruli
from vocabulary import VOCAB_DELETE_CALLBACK_PREFIX, VOCAB_PAGE_CALLBACK_PREFIX, render_vocabulary_page

# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# ВАЖНО: здесь сознательно используется gemini-3.6-flash, а не gemini-2.5-flash.
# gemini-2.5-flash закрыта Google для новых пользователей API (см. README) —
# использование её в качестве "модели для сложного текста" привело бы к тем
# же 404 NOT_FOUND, что мы уже чинили ранее в этом проекте. GEMINI_MODEL —
# единая точка правды для всех вызовов Gemini в боте.
GEMINI_MODEL = "gemini-3.6-flash"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("georgian_translator_bot")

if not TELEGRAM_BOT_TOKEN:
    logger.error("Не задана переменная окружения TELEGRAM_BOT_TOKEN. Проверьте файл .env")
    sys.exit(1)

if not GEMINI_API_KEY:
    logger.error("Не задана переменная окружения GEMINI_API_KEY. Проверьте файл .env")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Состояние в памяти процесса (см. README про ограничения persistence)
# --------------------------------------------------------------------------- #

user_modes: Dict[int, str] = {}
user_tones: Dict[int, str] = {}


class PendingTranslation(NamedTuple):
    """Данные последнего перевода под конкретным сообщением бота — для кнопок
    "Сохранить", "Разбор" и "Исходный текст (Мхедрули)"."""

    original_text: str
    translated_text: str
    mkhedruli_text: Optional[str] = None  # заполняется только для режима GEO_RU


# Кэш последних переводов: (chat_id, message_id) -> PendingTranslation.
# Ограничен по размеру, чтобы не течь по памяти на долго работающем процессе.
# Ограничение: после перезапуска бота кэш пуст, и кнопки под уже отправленными
# сообщениями ответят подсказкой "отправьте фразу ещё раз" — см. README.
MAX_PENDING_TRANSLATIONS = 2000
pending_translations: "OrderedDict[Tuple[int, int], PendingTranslation]" = OrderedDict()


def remember_translation(
    chat_id: int,
    message_id: int,
    original: str,
    translated: str,
    mkhedruli: Optional[str] = None,
) -> None:
    key = (chat_id, message_id)
    pending_translations[key] = PendingTranslation(original, translated, mkhedruli)
    pending_translations.move_to_end(key)
    while len(pending_translations) > MAX_PENDING_TRANSLATIONS:
        pending_translations.popitem(last=False)


MKHEDRULI_REVEAL_PREFIX = "🇬🇪 Мхедрули:"

# --------------------------------------------------------------------------- #
# Клиент Gemini
# --------------------------------------------------------------------------- #

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


async def call_gemini(contents: str, system_prompt: str) -> str:
    """
    Отправляет текст в Gemini с заданным системным промтом и возвращает результат.
    Бросает исключение наверх при ошибке — обработка на стороне вызывающего кода.
    """
    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
        ),
    )

    result = (response.text or "").strip()
    if not result:
        raise ValueError("Gemini вернул пустой ответ")

    return result


async def translate_geo_to_ru_smart(user_text: str, tone: str) -> Tuple[str, str, str]:
    """
    Полный пайплайн режима GEO Translit -> RU: локальная транслитерация в
    мхедрули -> оценка сложности -> перевод через Google Translate (простой
    текст) или Gemini (сложный текст), с автоматическим фоллбэком на другой
    провайдер в обе стороны, если выбранный недоступен.

    Возвращает (translated_text, mkhedruli_text, provider) — provider только
    для логирования/отладки (какой путь сработал).
    """
    mkhedruli_text = transliterate_to_mkhedruli(user_text)
    system_prompt = build_system_prompt(MODE_GEO_RU, tone)

    if is_simple_text(mkhedruli_text):
        try:
            translated = await translate_via_google(mkhedruli_text)
            return translated, mkhedruli_text, "google_translate"
        except Exception as google_exc:
            logger.warning(
                "Google Translate недоступен для простого текста (%s), пробую Gemini", google_exc
            )
            translated = await call_gemini(mkhedruli_text, system_prompt)
            return translated, mkhedruli_text, "gemini_fallback"

    try:
        translated = await call_gemini(mkhedruli_text, system_prompt)
        return translated, mkhedruli_text, "gemini"
    except Exception as gemini_exc:
        logger.warning(
            "Gemini недоступен для сложного текста (%s), переключаюсь на Google Translate", gemini_exc
        )
        try:
            translated = await translate_via_google(mkhedruli_text)
            return translated, mkhedruli_text, "google_translate_fallback"
        except Exception:
            logger.exception("Google Translate тоже недоступен — оба провайдера отказали")
            raise gemini_exc  # пробрасываем исходную ошибку Gemini (например, 429) наверх


def user_facing_gemini_error(exc: Exception) -> str:
    """
    Превращает исключение от Gemini API (или от обоих провайдеров сразу) в
    понятное пользователю сообщение. Отдельно обрабатывает превышение квоты
    (429 RESOURCE_EXHAUSTED) — самая частая причина сбоев на бесплатном тарифе.
    """
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return (
            "⚠️ Достигнут лимит запросов бесплатного тарифа Gemini API "
            f"(модель {GEMINI_MODEL}). Подождите немного и попробуйте снова — "
            "дневная квота бесплатного тарифа обновляется в полночь по "
            "тихоокеанскому времени (США). Для более высоких лимитов "
            "подключите платный тариф в Google AI Studio."
        )

    return "⚠️ Не удалось выполнить перевод. Попробуйте, пожалуйста, ещё раз чуть позже."


def format_gemini_markdown_as_html(text: str) -> str:
    """Конвертирует **bold**/`code` из ответа Gemini в Telegram-safe HTML."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


async def send_html_with_fallback(message: Message, html_text: str, plain_text: str) -> None:
    try:
        await message.answer(html_text, parse_mode="HTML")
    except TelegramBadRequest:
        logger.warning("Не удалось отправить сообщение с HTML-разметкой, отправляю как обычный текст")
        await message.answer(plain_text)


# --------------------------------------------------------------------------- #
# Хендлеры Telegram
# --------------------------------------------------------------------------- #

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    user_id = message.from_user.id
    user_modes.setdefault(user_id, DEFAULT_MODE)
    user_tones.setdefault(user_id, DEFAULT_TONE)
    await message.answer(
        "Привет! Я перевожу между грузинским чат-транслитом, русским языком и "
        "грузинским алфавитом (мхедрули).\n\n"
        f"Текущий режим: {MODES[DEFAULT_MODE]['label']}\n"
        f"Тональность: {TONES[DEFAULT_TONE]['label']}\n"
        "Сменить их можно командой /settings.\n\n"
        "Просто отправьте текстовое сообщение — переведу его в выбранном режиме. "
        "Кнопки ниже — быстрый доступ к словарю и шпаргалке.",
        reply_markup=build_main_menu_keyboard(),
    )


@router.message(Command("settings"))
async def handle_settings(message: Message) -> None:
    user_id = message.from_user.id
    mode = user_modes.get(user_id, DEFAULT_MODE)
    tone = user_tones.get(user_id, DEFAULT_TONE)
    await message.answer(
        render_settings_text(mode, tone),
        reply_markup=build_settings_keyboard(mode, tone),
    )


@router.message(F.text == MENU_BUTTON_SETTINGS)
async def handle_settings_menu_button(message: Message) -> None:
    await handle_settings(message)


@router.message(Command("dictionary"))
async def handle_dictionary_command(message: Message) -> None:
    await _send_vocabulary_page(message, message.from_user.id, offset=0)


@router.message(F.text == MENU_BUTTON_DICTIONARY)
async def handle_dictionary_menu_button(message: Message) -> None:
    await _send_vocabulary_page(message, message.from_user.id, offset=0)


@router.message(Command("alphabet"))
async def handle_alphabet_command(message: Message) -> None:
    await _send_alphabet_cheatsheet(message)


@router.message(F.text == MENU_BUTTON_ALPHABET)
async def handle_alphabet_menu_button(message: Message) -> None:
    await _send_alphabet_cheatsheet(message)


async def _send_vocabulary_page(target: Message, user_id: int, offset: int) -> None:
    text, kb = await render_vocabulary_page(user_id, offset)
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


async def _send_alphabet_cheatsheet(message: Message) -> None:
    plain_fallback = re.sub(r"<[^>]+>", "", ALPHABET_CHEATSHEET_HTML)
    await send_html_with_fallback(message, ALPHABET_CHEATSHEET_HTML, plain_fallback)


# --------------------------------------------------------------------------- #
# Callback-хендлеры: /settings (режим и тональность)
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith(MODE_CALLBACK_PREFIX))
async def handle_mode_selection(callback: CallbackQuery) -> None:
    mode_key = callback.data[len(MODE_CALLBACK_PREFIX):]
    if mode_key not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    user_id = callback.from_user.id
    user_modes[user_id] = mode_key
    tone = user_tones.get(user_id, DEFAULT_TONE)

    try:
        await callback.message.edit_text(
            render_settings_text(mode_key, tone),
            reply_markup=build_settings_keyboard(mode_key, tone),
        )
    except TelegramBadRequest:
        pass  # пользователь повторно выбрал тот же режим — сообщение не изменилось

    await callback.answer(f"Режим переключён: {MODES[mode_key]['label']}")


@router.callback_query(F.data.startswith(TONE_CALLBACK_PREFIX))
async def handle_tone_selection(callback: CallbackQuery) -> None:
    tone_key = callback.data[len(TONE_CALLBACK_PREFIX):]
    if tone_key not in TONES:
        await callback.answer("Неизвестная тональность", show_alert=True)
        return

    user_id = callback.from_user.id
    user_tones[user_id] = tone_key
    mode = user_modes.get(user_id, DEFAULT_MODE)

    try:
        await callback.message.edit_text(
            render_settings_text(mode, tone_key),
            reply_markup=build_settings_keyboard(mode, tone_key),
        )
    except TelegramBadRequest:
        pass

    await callback.answer(f"Тональность переключена: {TONES[tone_key]['label']}")


# --------------------------------------------------------------------------- #
# Callback-хендлеры: кнопки под переводом
# --------------------------------------------------------------------------- #

@router.callback_query(F.data == MHEDRULI_CALLBACK_DATA)
async def handle_mhedruli_button(callback: CallbackQuery) -> None:
    message = callback.message
    record = pending_translations.get((message.chat.id, message.message_id))
    if not record or record.mkhedruli_text is None:
        await callback.answer(
            "Не удалось показать исходный текст: сообщение устарело. Отправьте фразу ещё раз.",
            show_alert=True,
        )
        return

    await callback.answer()  # сразу убираем "часики" на кнопке
    await message.answer(f"{MKHEDRULI_REVEAL_PREFIX} {record.mkhedruli_text}")

    try:
        new_kb = replace_button(
            message.reply_markup, MHEDRULI_CALLBACK_DATA, "✅ Исходный текст показан", NOOP_CALLBACK_DATA
        )
        if new_kb is not None:
            await message.edit_reply_markup(reply_markup=new_kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == EXPLAIN_CALLBACK_DATA)
async def handle_explain_button(callback: CallbackQuery) -> None:
    message = callback.message
    record = pending_translations.get((message.chat.id, message.message_id))
    if not record:
        await callback.answer(
            "Не удалось выполнить разбор: сообщение устарело. Отправьте фразу ещё раз.",
            show_alert=True,
        )
        return

    await callback.answer()

    prompt_input = f"Транслит: {record.original_text}\nПеревод: {record.translated_text}"
    try:
        explanation = await call_gemini(prompt_input, EXPLAIN_PROMPT)
    except Exception as exc:
        logger.exception("Ошибка при разборе фразы (chat_id=%s)", message.chat.id)
        await message.answer(user_facing_gemini_error(exc))
        return

    await send_html_with_fallback(message, format_gemini_markdown_as_html(explanation), explanation)

    try:
        new_kb = replace_button(message.reply_markup, EXPLAIN_CALLBACK_DATA, "✅ Разбор", NOOP_CALLBACK_DATA)
        if new_kb is not None:
            await message.edit_reply_markup(reply_markup=new_kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == SAVE_CALLBACK_DATA)
async def handle_save_button(callback: CallbackQuery) -> None:
    message = callback.message
    record = pending_translations.get((message.chat.id, message.message_id))
    if not record:
        await callback.answer(
            "Не удалось сохранить: сообщение устарело. Отправьте фразу ещё раз.",
            show_alert=True,
        )
        return

    try:
        await db.add_vocabulary_entry(callback.from_user.id, record.original_text, record.translated_text)
    except Exception:
        logger.exception("Ошибка при сохранении в словарь (user_id=%s)", callback.from_user.id)
        await callback.answer("⚠️ Не удалось сохранить. Попробуйте ещё раз.", show_alert=True)
        return

    try:
        new_kb = replace_button(message.reply_markup, SAVE_CALLBACK_DATA, "✅ Сохранено", NOOP_CALLBACK_DATA)
        if new_kb is not None:
            await message.edit_reply_markup(reply_markup=new_kb)
    except TelegramBadRequest:
        pass

    await callback.answer("Сохранено в словарь ⭐")


@router.callback_query(F.data == NOOP_CALLBACK_DATA)
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer("Уже сделано")


# --------------------------------------------------------------------------- #
# Callback-хендлеры: /dictionary (пагинация и удаление)
# --------------------------------------------------------------------------- #

@router.callback_query(F.data.startswith(VOCAB_PAGE_CALLBACK_PREFIX))
async def handle_vocab_page(callback: CallbackQuery) -> None:
    offset = int(callback.data[len(VOCAB_PAGE_CALLBACK_PREFIX):])
    text, kb = await render_vocabulary_page(callback.from_user.id, offset)
    await callback.answer()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith(VOCAB_DELETE_CALLBACK_PREFIX))
async def handle_vocab_delete(callback: CallbackQuery) -> None:
    payload = callback.data[len(VOCAB_DELETE_CALLBACK_PREFIX):]
    try:
        entry_id_str, offset_str = payload.split(":")
        entry_id, offset = int(entry_id_str), int(offset_str)
    except ValueError:
        await callback.answer("Некорректные данные кнопки", show_alert=True)
        return

    deleted = await db.delete_vocabulary_entry(entry_id, callback.from_user.id)
    await callback.answer("Удалено" if deleted else "Запись не найдена (возможно, уже удалена)")

    text, kb = await render_vocabulary_page(callback.from_user.id, offset)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass


# --------------------------------------------------------------------------- #
# Перевод текста (основной сценарий)
# --------------------------------------------------------------------------- #

@router.message(F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    await message.answer(
        "Неизвестная команда. Доступные команды: /start, /settings, /dictionary, /alphabet"
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    user_text = message.text.strip()
    if not user_text:
        return

    user_id = message.from_user.id
    mode = user_modes.get(user_id, DEFAULT_MODE)
    tone = user_tones.get(user_id, DEFAULT_TONE)

    await message.bot.send_chat_action(message.chat.id, "typing")

    mkhedruli_text: Optional[str] = None

    if mode == MODE_GEO_RU:
        try:
            result, mkhedruli_text, provider = await translate_geo_to_ru_smart(user_text, tone)
        except Exception as exc:
            logger.exception("Ошибка перевода GEO->RU через смарт-роутинг (chat_id=%s)", message.chat.id)
            await message.answer(user_facing_gemini_error(exc))
            return
        logger.info("GEO->RU переведено через %s (chat_id=%s)", provider, message.chat.id)
    else:
        system_prompt = build_system_prompt(mode, tone)
        try:
            result = await call_gemini(user_text, system_prompt)
        except Exception as exc:
            logger.exception(
                "Ошибка при обращении к Gemini API (chat_id=%s, mode=%s)", message.chat.id, mode
            )
            await message.answer(user_facing_gemini_error(exc))
            return

    sent = await message.answer(result, reply_markup=build_translation_keyboard(mode))
    remember_translation(sent.chat.id, sent.message_id, user_text, result, mkhedruli_text)


@router.message()
async def handle_other(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстовое сообщение для перевода.")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

async def main() -> None:
    await db.init_db()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начало работы"),
            BotCommand(command="settings", description="Режим и тональность перевода"),
            BotCommand(command="dictionary", description="Мой словарь сохранённых фраз"),
            BotCommand(command="alphabet", description="Шпаргалка по транслиту"),
        ]
    )

    logger.info("Бот запущен, начинаю polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")