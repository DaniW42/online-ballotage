"""Mehrsprachigkeit: alle Texte stehen in app/locales/<code>.json.

Neue Sprache hinzufügen: de.json kopieren, nach <code>.json umbenennen
(z. B. en.json) und übersetzen - fertig, siehe docs/TRANSLATING.md.
Fehlt ein Schlüssel, greift die Standardsprache (de)."""
import json
import logging
import re
from pathlib import Path

from markupsafe import Markup, escape

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


def raw(lang: str, key: str):
    """Rohwert (String oder Liste) mit Rückfall auf die Standardsprache."""
    for code in (lang, DEFAULT_LANG):
        catalog = CATALOGS.get(code)
        if catalog and key in catalog:
            return catalog[key]
    log.warning("Übersetzungsschlüssel fehlt: %s", key)
    return key


def t(lang: str, key: str, **params) -> str:
    value = raw(lang, key)
    if params and isinstance(value, str):
        return value.format(**params)
    return value


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
