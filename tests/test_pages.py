import re

import pytest

from app.config import settings
from app.main import PUBLIC_PAGES

PAGES = list(PUBLIC_PAGES)


@pytest.mark.parametrize("path", PAGES)
def test_public_page_renders(anon, path):
    response = anon.get(path)
    assert response.status_code == 200
    html = response.text
    assert "<title>" in html and len(re.findall(r"<h1[ >]", html)) == 1
    assert 'name="description"' in html and 'lang="de"' in html


@pytest.mark.parametrize("path", PAGES + ["/login", "/register"])
def test_no_third_party_resources_and_no_inline_code(anon, path):
    html = anon.get(path).text
    assert not re.search(r'(?:src|href)="https?://(?!ballotage)[^"]*"[^>]*rel="(?:stylesheet|icon)"', html)
    assert not re.search(r'<(?:script|link|img)[^>]+(?:src|href)="https?://', html)
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)       # kein Inline-Skript (CSP)
    assert not re.search(r"\sstyle=", html) and not re.search(r"\son\w+=", html)


def test_home_has_call_to_action(anon):
    html = anon.get("/").text
    assert 'href="/register"' in html and 'href="/login"' in html


def test_logged_in_header_and_home(orga):
    html = orga.get("/").text
    assert 'href="/dashboard"' in html and 'action="/logout"' in html


def test_private_pages_are_noindex(orga, anon):
    for path in ("/login", "/register", "/dashboard", "/elections/new"):
        client = orga if path in ("/dashboard", "/elections/new") else anon
        assert "noindex" in client.get(path).text


def test_impressum_warns_when_unconfigured(anon, monkeypatch):
    for name in ("legal_name", "legal_street", "legal_city", "legal_email"):
        monkeypatch.setattr(settings, name, "")
    html = anon.get("/impressum").text
    assert "LEGAL_NAME" in html and "notice error" in html
    assert "LEGAL_NAME" in anon.get("/datenschutz").text


def test_impressum_and_privacy_show_configured_data(anon, monkeypatch):
    values = {"legal_name": "Erika Muster", "legal_street": "Musterweg 1", "legal_city": "12345 Musterstadt",
              "legal_email": "kontakt@example.org", "legal_phone": "+49 123 456", "legal_hosting": "Beispiel-Hosting GmbH"}
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    for path in ("/impressum", "/datenschutz"):
        html = anon.get(path).text
        assert "Erika Muster" in html and "Musterweg 1" in html and "12345 Musterstadt" in html
        assert 'href="mailto:kontakt@example.org"' in html
        assert "LEGAL_NAME" not in html
    assert "Beispiel-Hosting GmbH" in anon.get("/datenschutz").text


def test_robots_sitemap_security_txt(anon, monkeypatch):
    monkeypatch.setattr(settings, "legal_email", "sec@example.org")
    robots = anon.get("/robots.txt").text
    assert "Disallow: /v/" in robots and "Disallow: /auth/" in robots and "sitemap.xml" in robots
    sitemap = anon.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert all(f"{settings.base_url}{p}</loc>" in sitemap.text for p in PAGES)
    security = anon.get("/.well-known/security.txt").text
    assert "Contact: mailto:sec@example.org" in security and "Expires:" in security


def test_custom_404(anon):
    response = anon.get("/gibt-es-nicht")
    assert response.status_code == 404 and "Diese Seite gibt es nicht" in response.text


def test_healthz(anon):
    response = anon.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


def test_api_docs_not_exposed(anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path).status_code == 404


def test_login_shows_expired_notice(anon):
    assert "abgelaufen" in anon.get("/login?error=expired").text


def test_abort_confirmation_page_explains_consequences(create_election, orga):
    election_id, _ = create_election()
    html = orga.get(f"/elections/{election_id}/abort").text
    assert "endgültig gelöscht" in html and 'name="confirm" value="yes"' in html


def test_vote_pages_do_not_leak_to_search_engines(create_election, anon):
    _, tokens = create_election()
    assert "noindex" in anon.get(f"/v/{tokens['a@x.test']}").text


def test_static_assets_are_versioned(anon):
    html = anon.get("/").text
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{10}"', html)


def test_vote_page_has_no_site_navigation(create_election, anon):
    _, tokens = create_election()
    html = anon.get(f"/v/{tokens['a@x.test']}").text
    assert 'class="main-nav"' not in html and 'href="/register"' not in html


def test_new_election_defaults(orga):
    html = orga.get("/elections/new").text
    assert 'value="Weiß, Schwarz, Enthaltung"' in html
    assert re.search(r'name="reminder_enabled" value="true" checked', html)


def test_scheduled_election_is_not_shown_as_running(create_election, orga):
    election_id, _ = create_election(start=60 * 24, end=60 * 48)
    assert "Geplant" in orga.get("/dashboard").text
    assert "Geplant" in orga.get(f"/elections/{election_id}").text
    other, _ = create_election(title="läuft", start=-5, end=60)
    assert "Läuft" in orga.get(f"/elections/{other}").text


def test_dark_mode_sets_native_color_scheme(anon):
    css = anon.get("/static/style.css").text
    assert "color-scheme: dark" in css
