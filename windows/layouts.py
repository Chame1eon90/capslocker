# -*- coding: utf-8 -*-
"""Таблицы раскладок QWERTY (EN) и ЙЦУКЕН (RU) по скан-кодам клавиш."""

# scan: (en, EN_shift, ru, RU_shift)
KEYS = {
    0x29: ("`", "~", "ё", "Ё"),
    0x02: ("1", "!", "1", "!"),
    0x03: ("2", "@", "2", '"'),
    0x04: ("3", "#", "3", "№"),
    0x05: ("4", "$", "4", ";"),
    0x06: ("5", "%", "5", "%"),
    0x07: ("6", "^", "6", ":"),
    0x08: ("7", "&", "7", "?"),
    0x09: ("8", "*", "8", "*"),
    0x0A: ("9", "(", "9", "("),
    0x0B: ("0", ")", "0", ")"),
    0x0C: ("-", "_", "-", "_"),
    0x0D: ("=", "+", "=", "+"),
    0x10: ("q", "Q", "й", "Й"),
    0x11: ("w", "W", "ц", "Ц"),
    0x12: ("e", "E", "у", "У"),
    0x13: ("r", "R", "к", "К"),
    0x14: ("t", "T", "е", "Е"),
    0x15: ("y", "Y", "н", "Н"),
    0x16: ("u", "U", "г", "Г"),
    0x17: ("i", "I", "ш", "Ш"),
    0x18: ("o", "O", "щ", "Щ"),
    0x19: ("p", "P", "з", "З"),
    0x1A: ("[", "{", "х", "Х"),
    0x1B: ("]", "}", "ъ", "Ъ"),
    0x2B: ("\\", "|", "\\", "/"),
    0x1E: ("a", "A", "ф", "Ф"),
    0x1F: ("s", "S", "ы", "Ы"),
    0x20: ("d", "D", "в", "В"),
    0x21: ("f", "F", "а", "А"),
    0x22: ("g", "G", "п", "П"),
    0x23: ("h", "H", "р", "Р"),
    0x24: ("j", "J", "о", "О"),
    0x25: ("k", "K", "л", "Л"),
    0x26: ("l", "L", "д", "Д"),
    0x27: (";", ":", "ж", "Ж"),
    0x28: ("'", '"', "э", "Э"),
    0x2C: ("z", "Z", "я", "Я"),
    0x2D: ("x", "X", "ч", "Ч"),
    0x2E: ("c", "C", "с", "С"),
    0x2F: ("v", "V", "м", "М"),
    0x30: ("b", "B", "и", "И"),
    0x31: ("n", "N", "т", "Т"),
    0x32: ("m", "M", "ь", "Ь"),
    0x33: (",", "<", "б", "Б"),
    0x34: (".", ">", "ю", "Ю"),
    0x35: ("/", "?", ".", ","),
}

SPACE_SCAN = 0x39


def key_char(scan, shift, caps, lang):
    """Какой символ даёт клавиша в раскладке lang ('en'/'ru') с учётом Shift и CapsLock."""
    if scan == SPACE_SCAN:
        return " "
    row = KEYS.get(scan)
    if not row:
        return None
    base, up = (row[0], row[1]) if lang == "en" else (row[2], row[3])
    if base.isalpha():
        return up if (shift != caps) else base
    return up if shift else base


def render(keys, lang):
    return "".join(key_char(s, sh, c, lang) or "" for s, sh, c in keys)


# посимвольная таблица для перевода готового текста
_EN2RU, _RU2EN = {}, {}
for _row in KEYS.values():
    en, EN, ru, RU = _row
    for a, b in ((en, ru), (EN, RU)):
        _EN2RU.setdefault(a, b)
        _RU2EN.setdefault(b, a)


def detect_text_lang(text):
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if cyr == lat == 0:
        return None
    return "ru" if cyr > lat else "en"


def convert_text(text, src=None):
    """Переделывает текст, набранный не в той раскладке. Возвращает (новый текст, язык результата)."""
    src = src or detect_text_lang(text)
    if src is None:
        return text, None
    table = _RU2EN if src == "ru" else _EN2RU
    return "".join(table.get(ch, ch) for ch in text), ("en" if src == "ru" else "ru")
