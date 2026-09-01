"""
Отрисовка страниц личного словаря пользователя (/dictionary): текст + inline-клавиатура
с пагинацией и удалением записей. Логика хранения — в db.py, здесь только
презентационный слой (форматирование, HTML-escape, callback_data для кнопок).
"""

import html
from typing import Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db

PAGE_SIZE = db.PAGE_SIZE

VOCAB_PAGE_CALLBACK_PREFIX = "vocab_page:"
VOCAB_DELETE_CALLBACK_PREFIX = "vocab_del:"

EMPTY_VOCABULARY_TEXT = (
    "📚 Ваш словарь пока пуст.\n\n"
    "Переведите что-нибудь и нажмите «⭐ Сохранить» под ответом — фраза появится здесь."
)

_SHORT_LABEL_MAX_LEN = 28


def _short_label(text: str) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) > _SHORT_LABEL_MAX_LEN:
        return text[:_SHORT_LABEL_MAX_LEN] + "…"
    return text


async def render_vocabulary_page(user_id: int, offset: int) -> Tuple[str, InlineKeyboardMarkup]:
    total = await db.count_vocabulary_entries(user_id)

    if total == 0:
        return EMPTY_VOCABULARY_TEXT, InlineKeyboardMarkup(inline_keyboard=[])

    # Если запись удалили и offset вышёл за пределы — откатываемся на последнюю доступную страницу
    last_page_offset = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
    offset = max(0, min(offset, last_page_offset))

    entries = await db.get_vocabulary_page(user_id, offset, PAGE_SIZE)

    page_number = offset // PAGE_SIZE + 1
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"📚 <b>Мой словарь</b> (стр. {page_number} из {total_pages}, всего {total}):", ""]
    rows = []
    for i, entry in enumerate(entries, start=offset + 1):
        original = html.escape(entry.original_text)
        translated = html.escape(entry.translated_text)
        lines.append(f"{i}. {original}\n    ➜ {translated}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить #{i}: {_short_label(entry.original_text)}",
                    callback_data=f"{VOCAB_DELETE_CALLBACK_PREFIX}{entry.id}:{offset}",
                )
            ]
        )

    nav_row = []
    if offset > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"{VOCAB_PAGE_CALLBACK_PREFIX}{offset - PAGE_SIZE}"
            )
        )
    if offset + PAGE_SIZE < total:
        nav_row.append(
            InlineKeyboardButton(
                text="Вперёд ➡️", callback_data=f"{VOCAB_PAGE_CALLBACK_PREFIX}{offset + PAGE_SIZE}"
            )
        )
    if nav_row:
        rows.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)