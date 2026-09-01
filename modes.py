"""
Режимы перевода и тональность: конфигурация + сборка итогового системного промта.
"""

from typing import Dict

from prompts import (
    CASUAL_STYLE,
    OFFICIAL_STYLE,
    SYSTEM_PROMPT_GEO_TO_KA,
    SYSTEM_PROMPT_GEO_TO_RU,
    SYSTEM_PROMPT_RU_TO_GEO,
)

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

# Режимы, для которых имеет смысл тональность (это "переводы", а не конвертация письменности)
MODES_WITH_TONE = {MODE_GEO_RU, MODE_RU_GEO}

# --------------------------------------------------------------------------- #
# Тональность перевода
# --------------------------------------------------------------------------- #

TONE_OFFICIAL = "official"
TONE_CASUAL = "casual"
DEFAULT_TONE = TONE_CASUAL  # по умолчанию — разговорный/универсальный стиль

TONES: Dict[str, Dict[str, str]] = {
    TONE_OFFICIAL: {"label": "👔 Официальный", "modifier": OFFICIAL_STYLE},
    TONE_CASUAL: {"label": "💬 Разговорный", "modifier": CASUAL_STYLE},
}


def build_system_prompt(mode: str, tone: str) -> str:
    """
    Собирает итоговый системный промт: базовый промт режима + модификатор
    тональности (только для режимов "перевода" — GEO_RU и RU_GEO; для
    конвертации в мхедрули тональность не применяется).
    """
    base_prompt = MODES[mode]["prompt"]
    if mode in MODES_WITH_TONE:
        tone_modifier = TONES.get(tone, TONES[DEFAULT_TONE])["modifier"]
        return f"{base_prompt}\n{tone_modifier}"
    return base_prompt
