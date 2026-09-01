"""
Telegram-бот для перевода грузинского чат-транслита (карто-латиницы) на русский язык.

Стек:
    - aiogram 3.x — Telegram Bot API
    - google-genai — официальный SDK для Gemini API (модель gemini-2.5-flash)

Запуск:
    python main.py

Переменные окружения (см. .env.example):
    TELEGRAM_BOT_TOKEN — токен бота, выданный @BotFather
    GEMINI_API_KEY     — ключ Gemini API (https://aistudio.google.com/apikey)
"""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from google import genai
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
# Системный промт для Gemini
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
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

# --------------------------------------------------------------------------- #
# Клиент Gemini
# --------------------------------------------------------------------------- #

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


async def translate_georgian_transliteration(text: str) -> str:
    """
    Отправляет текст в Gemini и возвращает перевод с грузинского транслита на русский.
    Бросает исключение наверх при ошибке — обработка на стороне вызывающего кода.
    """
    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
        ),
    )

    translated = (response.text or "").strip()
    if not translated:
        raise ValueError("Gemini вернул пустой ответ")

    return translated


# --------------------------------------------------------------------------- #
# Хендлеры Telegram
# --------------------------------------------------------------------------- #

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Отправь мне сообщение на грузинском чат-транслите "
        "(грузинские слова латинскими буквами), и я переведу его на русский язык."
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    user_text = message.text.strip()
    if not user_text:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        translation = await translate_georgian_transliteration(user_text)
    except Exception:
        logger.exception(
            "Ошибка при обращении к Gemini API (chat_id=%s)", message.chat.id
        )
        await message.answer(
            "⚠️ Не удалось выполнить перевод. Попробуйте, пожалуйста, ещё раз чуть позже."
        )
        return

    await message.answer(translation)


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
