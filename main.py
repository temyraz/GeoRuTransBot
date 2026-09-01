"""
Telegram-бот для перевода между грузинским чат-транслитом, русским языком
и грузинским алфавитом (мхедрули).

Режимы (переключаются командой /settings, хранятся в памяти на пользователя):
    1. GEO Translit ➡️ RU        — перевод транслита на русский (по умолчанию)
    2. RU ➡️ GEO Translit        — перевод русского текста на грузинский транслит
    3. GEO Translit ➡️ ქართული   — конвертация транслита в грузинский алфавит

Под каждым переводом в режиме (1) есть кнопка "🇬🇪 На грузинский алфавит",
которая мгновенно дописывает к сообщению исходный текст, восстановленный
буквами мхедрули.

Стек:
    - aiogram 3.x — Telegram Bot API
    - google-genai — официальный SDK для Gemini API (модель gemini-3.6-flash)

Запуск:
    python main.py

Переменные окружения (см. .env.example):
    TELEGRAM_BOT_TOKEN — токен бота, выданный @BotFather
    GEMINI_API_KEY     — ключ Gemini API (https://aistudio.google.com/apikey)
"""

import asyncio
import logging
import os
import re
import sys
from typing import Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

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
# Системные промты для Gemini
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT_GEO_TO_RU = """\
Ты — профессиональный переводчик с грузинского языка на русский.
Твоя единственная задача — переводить сообщения, которые пользователи пишут грузинскими словами, но латинскими буквами (грузинский чат-транслит / карто-латиница).

Правила перевода:
1. Сначала мысленно восстанови исходный текст на грузинском алфавите (мхедрули), распознавая характерные латинские буквосочетания (например: dz = ძ, ts = წ/ც, ch = ჭ/ჩ, sh = შ, gh = ღ, kh = ხ).
2. Переведи полученный текст на грамотный, естественный русский язык.
3. Сохраняй все числа, даты, спецсимволы и форматирование без изменений.
4. Выводи ТОЛЬКО готовый перевод на русский язык. Не добавляй никаких пояснений, транскрипций, приветствий или исходного текста.

Пример:
Вход: Gacnobebt, rom ganaxlda momsaxurebis pirobebis 3.11 muxli da dzalashi shedis 10.09-dan.
Выход: Сообщаем вам, что обновлена статья 3.11 условий обслуживания и она вступает в силу с 10.09.
"""

SYSTEM_PROMPT_RU_TO_GEO = """\
Ты — переводчик с русского языка на грузинский чат-транслит (грузинский язык, написанный латинскими буквами).
Правила:
1. Переведи русский текст на естественный грузинский язык.
2. Запиши полученный грузинский перевод латиницей, используя общепринятые правила грузинского чат-транслита:
   - ძ = dz, წ/ც = ts, ჭ/ჩ = ch, შ = sh, ღ = gh, ხ = kh, ჟ = zh
3. Используй разговорные фразы, подходящие для мессенджеров.
4. Сохраняй форматирование, числа, даты и пунктуацию.
5. Выводи ТОЛЬКО итоговый транслит без пояснений, грузинских букв и исходного текста.
"""

SYSTEM_PROMPT_GEO_TO_KA = """\
Ты — эксперт по грузинской письменности и лингвистике.
Твоя задача — конвертировать грузинский текст, написанный латинским транслитом (чат-латиницей), в правильный грузинский алфавит (Мхедрули / ქართული დამწერლობა).
Правила:
1. Восстанови исходный грузинский текст, заменив латинские буквосочетания на соответствующий грузинский алфавит (например: dz -> ძ, kh -> ხ, sh -> შ, ch -> ჩ/ჭ).
2. Если в транслите были опечатки, исправь их так, чтобы на грузинском получилось грамматически корректное слово.
3. Сохраняй все числа, пунктуацию и структуру оригинала.
4. Выводи ТОЛЬКО готовый текст на грузинском языке (буквами мхедрули) без комментариев и перевода.
"""

# --------------------------------------------------------------------------- #
# Режимы перевода
# --------------------------------------------------------------------------- #

MODE_GEO_RU = "geo_ru"
MODE_RU_GEO = "ru_geo"
MODE_GEO_KA = "geo_ka"
DEFAULT_MODE = MODE_GEO_RU

MODES: Dict[str, Dict[str, str]] = {
    MODE_GEO_RU: {"label": "GEO Translit ➡️ RU", "prompt": SYSTEM_PROMPT_GEO_TO_RU},
    MODE_RU_GEO: {"label": "RU ➡️ GEO Translit", "prompt": SYSTEM_PROMPT_RU_TO_GEO},
    MODE_GEO_KA: {"label": "GEO Translit ➡️ ქართული", "prompt": SYSTEM_PROMPT_GEO_TO_KA},
}

# Текущий режим перевода на пользователя. Хранится в памяти процесса: при
# перезапуске бота сбрасывается на DEFAULT_MODE для всех. Для продакшена с
# большим числом пользователей стоит вынести в БД (см. README).
user_modes: Dict[int, str] = {}

MHEDRULI_BUTTON_TEXT = "🇬🇪 На грузинский алфавит"
MHEDRULI_CALLBACK_DATA = "mhedruli"

TRANSLIT_LABEL = "Транслит:"
TRANSLATION_LABEL = "Перевод:"
MKHEDRULI_LABEL = "Мхедрули:"

# --------------------------------------------------------------------------- #
# Клиент Gemini
# --------------------------------------------------------------------------- #

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


async def translate_with_prompt(text: str, system_prompt: str) -> str:
    """
    Отправляет текст в Gemini с заданным системным промтом и возвращает результат.
    Бросает исключение наверх при ошибке — обработка на стороне вызывающего кода.
    """
    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=text,
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


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #

def build_settings_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    rows = []
    for mode_key, mode_info in MODES.items():
        prefix = "✅ " if mode_key == current_mode else ""
        rows.append(
            [InlineKeyboardButton(text=f"{prefix}{mode_info['label']}", callback_data=f"mode:{mode_key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_mhedruli_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=MHEDRULI_BUTTON_TEXT, callback_data=MHEDRULI_CALLBACK_DATA)]]
    )


# --------------------------------------------------------------------------- #
# Хендлеры Telegram
# --------------------------------------------------------------------------- #

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    user_modes.setdefault(message.from_user.id, DEFAULT_MODE)
    await message.answer(
        "Привет! Я перевожу между грузинским чат-транслитом, русским языком и "
        "грузинским алфавитом (мхедрули).\n\n"
        f"Текущий режим: {MODES[DEFAULT_MODE]['label']}\n"
        "Сменить режим можно командой /settings.\n\n"
        "Просто отправьте текстовое сообщение — переведу его в выбранном режиме."
    )


@router.message(Command("settings"))
async def handle_settings(message: Message) -> None:
    current_mode = user_modes.get(message.from_user.id, DEFAULT_MODE)
    await message.answer(
        f"Текущий режим перевода: {MODES[current_mode]['label']}\n\nВыберите режим:",
        reply_markup=build_settings_keyboard(current_mode),
    )


@router.callback_query(F.data.startswith("mode:"))
async def handle_mode_selection(callback: CallbackQuery) -> None:
    mode_key = callback.data.split(":", 1)[1]
    if mode_key not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    user_modes[callback.from_user.id] = mode_key

    try:
        await callback.message.edit_text(
            f"Текущий режим перевода: {MODES[mode_key]['label']}\n\nВыберите режим:",
            reply_markup=build_settings_keyboard(mode_key),
        )
    except TelegramBadRequest:
        # Сообщение не изменилось (пользователь повторно выбрал тот же режим) — игнорируем.
        pass

    await callback.answer(f"Режим переключён: {MODES[mode_key]['label']}")


@router.callback_query(F.data == MHEDRULI_CALLBACK_DATA)
async def handle_mhedruli_button(callback: CallbackQuery) -> None:
    message = callback.message
    text = message.text or ""

    if MKHEDRULI_LABEL in text:
        await callback.answer("Уже показано ниже")
        return

    match = re.search(
        rf"{re.escape(TRANSLIT_LABEL)}\s*(.*?)\n\n{re.escape(TRANSLATION_LABEL)}",
        text,
        re.DOTALL,
    )
    if not match:
        await callback.answer("Не удалось найти исходный текст в сообщении", show_alert=True)
        return

    original_text = match.group(1).strip()
    await callback.answer()  # сразу убираем "часики" на кнопке

    try:
        mkhedruli_text = await translate_with_prompt(original_text, SYSTEM_PROMPT_GEO_TO_KA)
    except Exception as exc:
        logger.exception("Ошибка при конвертации в мхедрули (chat_id=%s)", message.chat.id)
        await message.answer(user_facing_gemini_error(exc))
        return

    new_text = f"{text}\n\n{MKHEDRULI_LABEL} {mkhedruli_text}"
    try:
        await message.edit_text(new_text)
    except TelegramBadRequest:
        logger.exception("Не удалось отредактировать сообщение с мхедрули (chat_id=%s)", message.chat.id)


@router.message(F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Доступные команды: /start, /settings")


@router.message(F.text)
async def handle_text(message: Message) -> None:
    user_text = message.text.strip()
    if not user_text:
        return

    mode = user_modes.get(message.from_user.id, DEFAULT_MODE)
    system_prompt = MODES[mode]["prompt"]

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        result = await translate_with_prompt(user_text, system_prompt)
    except Exception as exc:
        logger.exception(
            "Ошибка при обращении к Gemini API (chat_id=%s, mode=%s)", message.chat.id, mode
        )
        await message.answer(user_facing_gemini_error(exc))
        return

    if mode == MODE_GEO_RU:
        reply_text = f"{TRANSLIT_LABEL} {user_text}\n\n{TRANSLATION_LABEL} {result}"
        await message.answer(reply_text, reply_markup=build_mhedruli_keyboard())
    else:
        await message.answer(result)


@router.message()
async def handle_other(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстовое сообщение для перевода.")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

async def main() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Бот запущен, начинаю polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
