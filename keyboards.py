"""
Клавиатуры и связанные с ними текстовые константы (кроме тех, что относятся
к постраничному словарю — они в vocabulary.py, так как завязаны на данные из БД).
"""

from typing import Optional

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from modes import MODE_GEO_RU, MODES, TONES

# --------------------------------------------------------------------------- #
# Постоянное меню (ReplyKeyboardMarkup) под полем ввода
# --------------------------------------------------------------------------- #

MENU_BUTTON_DICTIONARY = "📚 Словарь"
MENU_BUTTON_ALPHABET = "📖 Транслит"
MENU_BUTTON_SETTINGS = "⚙️ Настройки"
MENU_BUTTON_HELP = "❓ Помощь"
MENU_BUTTON_EXAMPLES = "💬 Примеры"
MENU_BUTTON_HISTORY = "🕘 История"
MENU_BUTTON_QUICK = "⚡ Быстро"


QUICK_PHRASES = [
    "gamarjoba",
    "madloba",
    "rogor xar",
    "sad khar",
    "ras akhali ambavi",
    "nakhvamdis",
    "ki",
    "ara",
]


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_BUTTON_DICTIONARY), KeyboardButton(text=MENU_BUTTON_ALPHABET)],
            [KeyboardButton(text=MENU_BUTTON_SETTINGS)],
            [KeyboardButton(text=MENU_BUTTON_HELP), KeyboardButton(text=MENU_BUTTON_EXAMPLES)],
            [KeyboardButton(text=MENU_BUTTON_HISTORY), KeyboardButton(text=MENU_BUTTON_QUICK)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_quick_phrases_keyboard() -> ReplyKeyboardMarkup:
    rows = [QUICK_PHRASES[i : i + 2] for i in range(0, len(QUICK_PHRASES), 2)]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=phrase) for phrase in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# --------------------------------------------------------------------------- #
# /settings — режим перевода + тональность
# --------------------------------------------------------------------------- #

MODE_CALLBACK_PREFIX = "mode:"
TONE_CALLBACK_PREFIX = "tone:"


def build_settings_keyboard(current_mode: str, current_tone: str) -> InlineKeyboardMarkup:
    rows = []
    for mode_key, mode_info in MODES.items():
        prefix = "✅ " if mode_key == current_mode else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{mode_info['label']}",
                    callback_data=f"{MODE_CALLBACK_PREFIX}{mode_key}",
                )
            ]
        )
    for tone_key, tone_info in TONES.items():
        prefix = "✅ " if tone_key == current_tone else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{tone_info['label']}",
                    callback_data=f"{TONE_CALLBACK_PREFIX}{tone_key}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_settings_text(current_mode: str, current_tone: str) -> str:
    return (
        "⚙️ Настройки перевода\n"
        "────────────────────\n"
        f"Режим: {MODES[current_mode]['label']}\n"
        f"Тональность: {TONES[current_tone]['label']}\n\n"
        f"Сценарий: {MODES[current_mode].get('description', '')}\n\n"
        "Выберите нужный режим и стиль:"
    )


# --------------------------------------------------------------------------- #
# Клавиатура под каждым переводом
# --------------------------------------------------------------------------- #

MHEDRULI_BUTTON_TEXT = "🇬🇪 Исходный текст (Мхедрули)"
MHEDRULI_CALLBACK_DATA = "mhedruli"

EXPLAIN_BUTTON_TEXT = "🔍 Разбор"
EXPLAIN_CALLBACK_DATA = "explain"

SAVE_BUTTON_TEXT = "⭐ Сохранить"
SAVE_CALLBACK_DATA = "save"

NOOP_CALLBACK_DATA = "noop"


def build_translation_keyboard(mode: str) -> InlineKeyboardMarkup:
    rows = []
    if mode == MODE_GEO_RU:
        rows.append(
            [
                InlineKeyboardButton(text=MHEDRULI_BUTTON_TEXT, callback_data=MHEDRULI_CALLBACK_DATA),
                InlineKeyboardButton(text=EXPLAIN_BUTTON_TEXT, callback_data=EXPLAIN_CALLBACK_DATA),
            ]
        )
    rows.append([InlineKeyboardButton(text=SAVE_BUTTON_TEXT, callback_data=SAVE_CALLBACK_DATA)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def replace_button(
    markup: Optional[InlineKeyboardMarkup], old_callback_data: str, new_text: str, new_callback_data: str
) -> Optional[InlineKeyboardMarkup]:
    """
    Точечно заменяет одну кнопку в существующей клавиатуре по её callback_data
    (например, "⭐ Сохранить" -> "✅ Сохранено"), не трогая остальные кнопки.
    """
    if markup is None:
        return None

    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            if button.callback_data == old_callback_data:
                new_row.append(InlineKeyboardButton(text=new_text, callback_data=new_callback_data))
            else:
                new_row.append(button)
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)