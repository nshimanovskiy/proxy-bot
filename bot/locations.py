"""Красивые названия локаций.

Steel Proxy отдаёт в title внутренние имена нод («DE_ISP01», «США_DC05», «Россия»),
а country_code бывает не ISO-кодом. Здесь из этого получается «🇩🇪 Германия»
(и «🇩🇪 Германия · ISP01», если у страны несколько нод в одном списке).
"""
from __future__ import annotations

import re

COUNTRIES_RU = {
    "RU": "Россия", "US": "США", "DE": "Германия", "NL": "Нидерланды", "FI": "Финляндия",
    "TR": "Турция", "KZ": "Казахстан", "GB": "Великобритания", "UK": "Великобритания",
    "FR": "Франция", "PL": "Польша", "SE": "Швеция", "NO": "Норвегия", "CH": "Швейцария",
    "AT": "Австрия", "CZ": "Чехия", "LV": "Латвия", "LT": "Литва", "EE": "Эстония",
    "UA": "Украина", "BY": "Беларусь", "AM": "Армения", "GE": "Грузия", "AZ": "Азербайджан",
    "UZ": "Узбекистан", "KG": "Киргизия", "MD": "Молдова", "RO": "Румыния", "BG": "Болгария",
    "RS": "Сербия", "HU": "Венгрия", "IT": "Италия", "ES": "Испания", "PT": "Португалия",
    "IE": "Ирландия", "BE": "Бельгия", "DK": "Дания", "IS": "Исландия", "GR": "Греция",
    "CY": "Кипр", "IL": "Израиль", "AE": "ОАЭ", "IN": "Индия", "CN": "Китай",
    "HK": "Гонконг", "JP": "Япония", "KR": "Южная Корея", "SG": "Сингапур", "TH": "Таиланд",
    "VN": "Вьетнам", "ID": "Индонезия", "MY": "Малайзия", "AU": "Австралия", "CA": "Канада",
    "MX": "Мексика", "BR": "Бразилия", "AR": "Аргентина", "ZA": "ЮАР", "EG": "Египет",
}
# Обратный поиск: «США» → US, «германия» → DE. Плюс частые варианты написания.
_BY_NAME = {name.lower(): code for code, name in COUNTRIES_RU.items() if code != "UK"}
_BY_NAME.update({
    "сша": "US", "америка": "US", "usa": "US", "англия": "GB", "голландия": "NL",
    "казахстан": "KZ", "рф": "RU", "russia": "RU", "germany": "DE", "netherlands": "NL",
    "finland": "FI", "turkey": "TR", "türkiye": "TR", "kazakhstan": "KZ", "england": "GB",
})

_ISO = re.compile(r"^[A-Z]{2}$")
_SPLIT = re.compile(r"[\s_\-./]+")


def _iso(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().upper()
    if code == "UK":
        code = "GB"
    return code if _ISO.match(code) else None


def parse(country_code: str | None, title: str | None) -> tuple[str | None, str]:
    """→ (ISO-код или None, суффикс ноды вроде «ISP01» / «DC05» или '')."""
    title = (title or "").strip()
    parts = [p for p in _SPLIT.split(title) if p]
    head = parts[0] if parts else ""
    suffix = " ".join(parts[1:])

    iso = _iso(country_code)
    if iso is None and country_code:
        # country_code вида «DE_ISP01» или «США»
        cc_parts = [p for p in _SPLIT.split(country_code.strip()) if p]
        if cc_parts:
            iso = _iso(cc_parts[0]) or _BY_NAME.get(cc_parts[0].lower())
    head_iso = _iso(head) or _BY_NAME.get(head.lower()) or _BY_NAME.get(title.lower())
    if iso is None:
        iso = head_iso
    if head_iso is None:
        # заголовок — не страна и не код страны: оставляем его целиком как уточнение
        suffix = title if title.upper() != (iso or "") else ""
    return iso, suffix


def flag(iso: str | None) -> str:
    if not iso or iso not in COUNTRIES_RU:
        return "🌐"  # неизвестный код — без «флага» из случайных букв
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def display(country_code: str | None, title: str | None, with_suffix: bool = True) -> str:
    iso, suffix = parse(country_code, title)
    if iso and iso in COUNTRIES_RU:
        name = COUNTRIES_RU[iso]
    elif iso and title and parse(None, title)[0] is None:
        name, suffix = title, ""  # незнакомый код страны — показываем название как есть
    elif iso:
        name = iso
    else:
        name, suffix = (title or country_code or "—"), ""
    text = f"{flag(iso)} {name}"
    if with_suffix and suffix:
        text += f" · {suffix}"
    return text


def display_many(items: list[tuple[str | None, str | None]]) -> list[str]:
    """Названия для списка: суффикс ноды показываем только у стран с несколькими нодами."""
    isos = [parse(cc, t)[0] for cc, t in items]
    return [display(cc, t, with_suffix=bool(iso) and isos.count(iso) > 1)
            for (cc, t), iso in zip(items, isos)]
