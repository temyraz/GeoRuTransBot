"""
Telegram-бот для перевода между грузинским чат-транслитом, русским языком
и грузинским алфавитом (мхедрули).

Функционал:
    - 3 режима перевода (/settings): GEO Translit -> RU, RU -> GEO Translit,
      GEO Translit -> ქართული (мхедрули).
    - Тональность перевода (/settings): официальная / разговорная.
    - Кнопка "🇬🇪 На грузинский алфавит" под переводом (режим 1) — мгновенно
      дописывает исходный текст буквами мхедрули.
    - Кнопка "🔍 Разбор" (режим 1) — короткий разбор тона, нюансов и ключевых
      слов фразы.
    - Кнопка "⭐ Сохранить" под любым переводом — сохраняет пару текстов в
      личный словарь пользователя (SQLite). /dictionary — просмотр с
      пагинацией и удалением записей.
    - /alphabet — статичная шпаргалка по "сложным" буквосочетаниям транслита
      (без обращения к Gemini).

Код разбит на модули:
    - prompts.py    — тексты системных промтов и шпаргалка
    - modes.py       — конфигурация режимов/тональности, сборка промта
    - db.py          — SQLAlchemy + SQLite, таблица vocabulary
    - keyboards.py   — общие клавиатуры (меню, /settings, кнопки под переводом)
    - vocabulary.py  — отрисовка страниц личного словаря
    - main.py (этот файл) — хендлеры aiogram и точка входа

Стек: aiogram 3.x, google-genai (Gemini API, модель gemini-3.6-flash),
SQLAlchemy (async) + aiosqlite.

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
from typing import Dict, Optional, Tuple

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
from prompts import ALPHABET_CHEATSHEET_HTML, EXPLAIN_PROMPT, SYSTEM_PROMPT_GEO_TO_KA
from vocabulary import VOCAB_DELETE_CALLBACK_PREFIX, VOCAB_PAGE_CALLBACK_PREFIX, render_vocabulary_page

# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
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

# Кэш последних переводов для кнопки "⭐ Сохранить": (chat_id, message_id) -> (original, translated).
# Ограничен по размеру, чтобы не течь по памяти на долго работающем процессе.
MAX_PENDING_TRANSLATIONS = 2000
pending_translations: "OrderedDict[Tuple[int, int], Tuple[str, str]]" = OrderedDict()


def remember_translation(chat_id: int, message_id: int, original: str, translated: str) -> None:
    key = (chat_id, message_id)
    pending_translations[key] = (original, translated)
    pending_translations.move_to_end(key)
    while len(pending_translations) > MAX_PENDING_TRANSLATIONS:
        pending_translations.popitem(last=False)


# Метки, которыми размечен ответ бота в режиме GEO Translit -> RU. Используются
# для регэксп-парсинга исходного текста и перевода из тела сообщения (кнопки
# "На грузинский алфавит" и "Разбор" не нуждаются в отдельном хранилище и
# продолжают работать даже после перезапуска бота).
TRANSLIT_LABEL = "Транслит:"
TRANSLATION_LABEL = "Перевод:"
MKHEDRULI_LABEL = "Мхедрули:"


def extract_translit_pair(text: str) -> Optional[Tuple[str, str]]:
    match = re.search(
        rf"{re.escape(TRANSLIT_LABEL)}\s*(.*?)\n\n{re.escape(TRANSLATION_LABEL)}"
        rf"\s*(.*?)(?:\n\n{re.escape(MKHEDRULI_LABEL)}|$)",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


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


def user_facing_gemini_error(exc: Exception) -> str:
    """
    Превращает исключение от Gemini API в понятное пользователю сообщение.
    Отдельно обрабатывает превышение квоты (429 RESOURCE_EXHAUSTED) — самая
    частая причина сбоев на бесплатном тарифе.
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
    text = message.text or ""

    if MKHEDRULI_LABEL in text:
        await callback.answer("Уже показано ниже")
        return

    pair = extract_translit_pair(text)
    if not pair:
        await callback.answer("Не удалось найти исходный текст в сообщении", show_alert=True)
        return
    original_text, _translation = pair

    await callback.answer()  # сразу убираем "часики" на кнопке

    try:
        mkhedruli_text = await call_gemini(original_text, SYSTEM_PROMPT_GEO_TO_KA)
    except Exception as exc:
        logger.exception("Ошибка при конвертации в мхедрули (chat_id=%s)", message.chat.id)
        await message.answer(user_facing_gemini_error(exc))
        return

    new_text = f"{text}\n\n{MKHEDRULI_LABEL} {mkhedruli_text}"
    try:
        await message.edit_text(new_text)
    except TelegramBadRequest:
        logger.exception("Не удалось отредактировать сообщение с мхедрули (chat_id=%s)", message.chat.id)


@router.callback_query(F.data == EXPLAIN_CALLBACK_DATA)
async def handle_explain_button(callback: CallbackQuery) -> None:
    message = callback.message
    text = message.text or ""

    pair = extract_translit_pair(text)
    if not pair:
        await callback.answer("Не удалось найти текст для разбора", show_alert=True)
        return
    original_text, translated_text = pair

    await callback.answer()

    prompt_input = f"{TRANSLIT_LABEL} {original_text}\n{TRANSLATION_LABEL} {translated_text}"
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
    pair = pending_translations.get((message.chat.id, message.message_id))
    if not pair:
        await callback.answer(
            "Не удалось сохранить: сообщение устарело. Отправьте фразу ещё раз.",
            show_alert=True,
        )
        return
    original_text, translated_text = pair

    try:
        await db.add_vocabulary_entry(callback.from_user.id, original_text, translated_text)
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
    system_prompt = build_system_prompt(mode, tone)

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        result = await call_gemini(user_text, system_prompt)
    except Exception as exc:
        logger.exception(
            "Ошибка при обращении к Gemini API (chat_id=%s, mode=%s)", message.chat.id, mode
        )
        await message.answer(user_facing_gemini_error(exc))
        return

    if mode == MODE_GEO_RU:
        reply_text = f"{TRANSLIT_LABEL} {user_text}\n\n{TRANSLATION_LABEL} {result}"
    else:
        reply_text = result

    sent = await message.answer(reply_text, reply_markup=build_translation_keyboard(mode))
    remember_translation(sent.chat.id, sent.message_id, user_text, result)


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
