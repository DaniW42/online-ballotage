import re
from pathlib import Path

import pytest

from app import i18n
from conftest import local_input

APP = Path(__file__).resolve().parent.parent / "app"
PLACEHOLDER = re.compile(r"{(\w+)}")


def _placeholders(value) -> set:
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    return set()


def test_all_locales_have_the_same_keys_as_default():
    default = i18n.CATALOGS[i18n.DEFAULT_LANG]
    for code, catalog in i18n.CATALOGS.items():
        assert set(catalog) == set(default), f"{code}: Schlüssel weichen von {i18n.DEFAULT_LANG} ab"


def test_all_locales_use_the_same_placeholders():
    default = i18n.CATALOGS[i18n.DEFAULT_LANG]
    for code, catalog in i18n.CATALOGS.items():
        for key, value in catalog.items():
            assert _placeholders(value) == _placeholders(default[key]), f"{code}:{key}"


def _used_keys():
    keys, prefixes = set(), set()
    patterns = [
        re.compile(r"""\btl?\(\s*['"]([\w.]+)['"](?!\s*~)"""),                # Templates: t('a.b')
        re.compile(r"""\bt\(\s*(?:lang|request\.state\.lang|election\.language),\s*"([\w.]+)\""""),
        re.compile(r"""message_key": "([\w.]+)\""""),
        re.compile(r"""_form_error\(\s*request,\s*org,\s*"([\w.]+)\""""),
    ]
    files = list((APP / "templates").glob("*.html")) + list(APP.glob("*.py"))
    dynamic = re.compile(r"""\btl?\(\s*['"]([\w.]+)['"]\s*~""")                 # Templates: t('a.' ~ x)
    for path in files:
        text = path.read_text(encoding="utf-8")
        prefixes.update(dynamic.findall(text))
        for pattern in patterns:
            keys.update(pattern.findall(text))
    return keys, prefixes


def test_every_used_key_exists():
    default = i18n.CATALOGS[i18n.DEFAULT_LANG]
    keys, prefixes = _used_keys()
    assert len(keys) > 100  # Plausibilität: die Suche findet wirklich etwas
    assert sorted(k for k in keys if k not in default) == []
    for prefix in prefixes:
        assert any(k.startswith(prefix) for k in default), prefix


def test_no_hardcoded_german_in_templates():
    """Sichtbare Texte gehören in die Sprachdatei, nicht in die Templates."""
    umlaut_text = re.compile(r">[^<{]*[äöüÄÖÜß][^<{]*<")
    for path in (APP / "templates").glob("*.html"):
        assert not umlaut_text.search(path.read_text(encoding="utf-8")), path.name


@pytest.fixture
def second_language(monkeypatch):
    catalog = {"meta.name": "Test", "meta.html_lang": "xx", "mail.invite.subject": "XX-Einladung: {title}"}
    monkeypatch.setitem(i18n.CATALOGS, "xx", catalog)
    return "xx"


def test_missing_key_falls_back_to_default(second_language):
    assert i18n.t("xx", "nav.login") == i18n.t("de", "nav.login")
    assert i18n.t("xx", "mail.invite.subject", title="A") == "XX-Einladung: A"


def test_pick_language():
    assert i18n.pick_language(None, None) == "de"
    assert i18n.pick_language("de", None) == "de"
    assert i18n.pick_language("zz", "fr-CH, de;q=0.8") == "de"
    assert i18n.pick_language(None, "de-DE,de;q=0.9,en;q=0.8") == "de"


def test_pick_language_prefers_cookie_then_header(second_language):
    assert i18n.pick_language("xx", "de") == "xx"
    assert i18n.pick_language(None, "xx-YY,de;q=0.5") == "xx"


def test_language_switch_sets_cookie_and_sanitizes_redirect(anon, second_language):
    response = anon.get("/lang/xx?next=/faq")
    assert response.headers["location"] == "/faq" and "lang=xx" in response.headers["set-cookie"]
    assert anon.get("/lang/xx?next=//evil.test").headers["location"] == "/"
    assert anon.get("/lang/xx?next=https://evil.test").headers["location"] == "/"
    assert "set-cookie" not in anon.get("/lang/doesnotexist").headers


def test_language_switcher_only_visible_with_multiple_languages(anon, second_language):
    assert "/lang/xx" in anon.get("/").text


def test_single_language_hides_switcher(anon):
    assert "/lang/" not in anon.get("/").text


def test_election_language_follows_creator_and_is_used_in_mails(orga, outbox, second_language, db):
    from app.db import Election
    orga.cookies.set("lang", "xx")
    orga.post("/elections/new", data={
        "title": "T", "starts_at": local_input(-60), "ends_at": local_input(60),
        "emails_raw": "a@x.test b@x.test c@x.test", "options_raw": ""})
    assert db.query(Election).one().language == "xx"
    assert outbox.to("a@x.test")[0].subject == "XX-Einladung: T"


def test_render_md_escapes_html_and_formats():
    html = str(i18n.render_md("Ein **Test** mit `code` <script>x</script>\n\n- eins\n- zwei"))
    assert "<strong>Test</strong>" in html and "<code>code</code>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<ul><li>eins</li><li>zwei</li></ul>" in html


def test_render_md_links_only_safe_schemes():
    ok = str(i18n.render_md("[a](/faq) [b](https://x.test) [c](mailto:a@b.de)"))
    assert 'href="/faq"' in ok and 'href="https://x.test"' in ok and 'href="mailto:a@b.de"' in ok
    bad = str(i18n.render_md("[x](javascript:alert(1))"))
    assert "<a " not in bad


def test_switcher_hidden_on_token_pages(create_election, anon, second_language):
    """Auf /v/<token> und /auth/<token> darf kein Link mit dem Token als ?next= entstehen."""
    _, tokens = create_election()
    assert "/lang/" not in anon.get(f"/v/{tokens['a@x.test']}").text
    assert "/lang/xx" in anon.get("/faq").text


def test_no_markdown_applied_to_parametrised_strings():
    """Strings mit Platzhaltern (Nutzereingaben!) dürfen nie durch |md laufen."""
    pattern = re.compile(r"\bt\([^)]*=[^)]*\)\s*\|\s*md")
    for path in (APP / "templates").glob("*.html"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


# Sätze, in denen "Sie" ein Pronomen für Sachen ist ("sie läuft", "sie werden gelöscht"), keine Anrede
SIE_AS_PRONOUN = {"home.open_text", "selfhost.lead", "dashboard.recipients_none", "faq.groups.4.items.0.a"}
LEGAL_SECTIONS = ("impressum.", "privacy.")


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}.{i}" if path else str(i))


def test_german_uses_informal_address_except_legal_texts():
    """Seite und Mails sind in der Du-Form; nur Impressum und Datenschutz bleiben bei 'Sie'."""
    catalog = i18n.CATALOGS["de"]
    offenders = []
    for key, value in catalog.items():
        if key.startswith(LEGAL_SECTIONS):
            continue
        for path, text in _strings(value, key):
            formal = re.search(r"\b(Ihr\w*|Ihnen)\b", text)
            sie = re.search(r"\bSie\b", text) and not path.startswith(tuple(SIE_AS_PRONOUN))
            if formal or sie:
                offenders.append(path)
    assert offenders == []


def test_legal_texts_keep_formal_address():
    catalog = i18n.CATALOGS["de"]
    legal = " ".join(t for k, v in catalog.items() if k.startswith(LEGAL_SECTIONS) for _, t in _strings(v, k))
    assert re.search(r"\bSie\b", legal) and "du " not in legal.lower()


PUBLIC = ["/", "/how-it-works", "/security", "/faq", "/self-hosting", "/source", "/impressum", "/datenschutz"]


def test_configurable_numbers_are_shown_dynamically(anon, monkeypatch):
    from app.config import settings
    pages = {p: anon.get(p).text for p in PUBLIC}
    assert "30 Tage" in pages["/faq"] and "drei" in pages["/faq"] and "15 Minuten" in pages["/faq"]
    monkeypatch.setattr(settings, "min_voters", 5)
    monkeypatch.setattr(settings, "retention_days", 14)
    monkeypatch.setattr(settings, "magic_link_ttl_minutes", 10)
    monkeypatch.setattr(settings, "reminder_hours_before", 12)
    monkeypatch.setattr(settings, "test_mode_max_voters", 4)
    monkeypatch.setattr(settings, "test_mode_daily_invitations", 20)
    faq, security, privacy = (anon.get(p).text for p in ("/faq", "/security", "/datenschutz"))
    assert "14 Tage" in faq and "14 Tage" in security and "14 Tage" in privacy
    assert "fünf" in faq and "fünf" in security and "fünf" in privacy
    assert "10 Minuten" in faq and "10 Minuten" in security
    assert "12 Stunden vor Fristende" in faq and "13 Stunden" in faq
    assert "vier Empfänger" in security and "20 Einladungen" in security
    assert "30 Tage" not in faq and "30 Tage nach" not in privacy


def test_no_unresolved_placeholders_on_public_pages(anon):
    for path in PUBLIC + ["/login", "/register"]:
        html = anon.get(path).text
        assert not re.search(r"\{[a-z_0-9]+\}", html), (path, re.findall(r"\{[a-z_0-9]+\}", html))


def test_global_placeholders_are_the_same_in_every_language():
    """Platzhalter-Konsistenz gilt auch für die Konfigurationswerte."""
    default = i18n.CATALOGS[i18n.DEFAULT_LANG]
    assert any("{min_voters_word}" in str(v) for v in default.values())


def test_number_words():
    from app import i18n as i
    assert i._NUMBER_WORDS[3] == "drei"
