"""Mehrsprachigkeit: alle Texte stehen in app/locales/<code>.json.

Neue Sprache hinzufügen: de.json kopieren, nach <code>.json umbenennen
(z. B. en.json) und übersetzen - fertig, siehe docs/TRANSLATING.md.
Fehlt ein Schlüssel, greift die Standardsprache (de)."""
import json
import logging
import re
from pathlib import Path

from markupsafe import Markup, escape

from .config import settings
from .timeutil import to_local

log = logging.getLogger("kugelung")

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANG = "de"
LANG_COOKIE = "lang"


def _flatten(data: dict, prefix: str = "") -> dict:
    """Verschachtelte Dicts zu 'a.b.c'-Schlüsseln; Listen/Strings bleiben Werte."""
    flat = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, full + "."))
        else:
            flat[full] = value
    return flat


def load_catalogs() -> dict[str, dict]:
    catalogs = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        catalogs[path.stem] = _flatten(json.loads(path.read_text(encoding="utf-8")))
    return catalogs


CATALOGS = load_catalogs()


def languages() -> list[tuple[str, str]]:
    """[(code, Anzeigename)] aller vorhandenen Sprachdateien."""
    return [(code, cat.get("meta.name", code)) for code, cat in CATALOGS.items()]


_NUMBER_WORDS = {1: "ein", 2: "zwei", 3: "drei", 4: "vier", 5: "fünf", 6: "sechs", 7: "sieben",
                 8: "acht", 9: "neun", 10: "zehn", 11: "elf", 12: "zwölf"}


def _globals() -> dict[str, str]:
    """Werte aus der Konfiguration, die in Texten als {platzhalter} stehen dürfen.
    Zahlwörter (…_word) gibt es nur für 1-12 (deutsch), sonst die Ziffer."""
    def word(n: int) -> str:
        return _NUMBER_WORDS.get(n, str(n))
    return {
        "min_voters": str(settings.min_voters),
        "min_voters_word": word(settings.min_voters),
        "test_max_voters_word": word(settings.test_mode_max_voters),
        "test_daily_word": word(settings.test_mode_daily_invitations),
        "retention_days": str(settings.retention_days),
        "reminder_hours": str(settings.reminder_hours_before),
        "reminder_hours_plus": str(settings.reminder_hours_before + 1),
        "magic_minutes": str(settings.magic_link_ttl_minutes),
        "session_days": str(settings.session_days),
    }


_GLOBAL_PLACEHOLDER = re.compile(r"\{(min_voters_word|min_voters|test_max_voters_word|test_daily_word|"
                                 r"retention_days|reminder_hours_plus|reminder_hours|magic_minutes|session_days)\}")


def fill_globals(value):
    """Ersetzt Konfigurations-Platzhalter in Strings (auch in Listen/Dicts)."""
    if isinstance(value, str):
        values = _globals()
        return _GLOBAL_PLACEHOLDER.sub(lambda m: values[m.group(1)], value)
    if isinstance(value, list):
        return [fill_globals(v) for v in value]
    if isinstance(value, dict):
        return {k: fill_globals(v) for k, v in value.items()}
    return value


def raw(lang: str, key: str):
    """Rohwert (String oder Liste) mit Rückfall auf die Standardsprache."""
    for code in (lang, DEFAULT_LANG):
        catalog = CATALOGS.get(code)
        if catalog and key in catalog:
            return catalog[key]
    log.warning("Übersetzungsschlüssel fehlt: %s", key)
    return key


def t(lang: str, key: str, **params) -> str:
    value = fill_globals(raw(lang, key))
    if params and isinstance(value, str):
        return value.format(**params)
    return value


def tl(lang: str, key: str):
    """Liste/Struktur aus der Sprachdatei, Konfigurations-Platzhalter eingesetzt."""
    return fill_globals(raw(lang, key))


def pick_language(cookie_value: str | None, accept_language: str | None) -> str:
    """Cookie > Accept-Language > Standardsprache."""
    if cookie_value in CATALOGS:
        return cookie_value
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()
        for candidate in (code, code.split("-")[0]):
            if candidate in CATALOGS:
                return candidate
    return DEFAULT_LANG


def fmt_datetime(lang: str, dt) -> str:
    return to_local(dt).strftime(t(lang, "meta.datetime_format"))


def fmt_date(lang: str, dt) -> str:
    return to_local(dt).strftime(t(lang, "meta.date_format"))


_LINK = re.compile(r"\[([^\]]+)\]\(((?:/|https://|mailto:)[^)\s]*)\)")


def _inline(text: str) -> str:
    text = str(escape(text))
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return _LINK.sub(r'<a href="\2">\1</a>', text)


def render_md(text) -> Markup:
    """Minimales, sicheres Markup für Sprachdateien: Absätze, '- ' Listen,
    **fett**, `code`, [Text](/pfad|https://...). Alles andere wird escaped."""
    blocks = []
    for block in str(text).strip().split("\n\n"):
        lines = block.strip().splitlines()
        if lines and all(line.startswith("- ") for line in lines):
            items = "".join(f"<li>{_inline(line[2:])}</li>" for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append(f"<p>{_inline(' '.join(lines))}</p>")
    return Markup("".join(blocks))
