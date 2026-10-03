import re

from conftest import make_login


def _token(outbox, email):
    return re.search(r"/auth/(\S+)", outbox.to(email)[-1].body).group(1)


def test_magic_link_get_does_not_consume(anon, outbox):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    token = _token(outbox, "a@loge.test")
    assert anon.get(f"/auth/{token}").status_code == 200
    assert anon.get(f"/auth/{token}").status_code == 200  # Mail-Scanner dürfen vorab aufrufen
    assert anon.post(f"/auth/{token}").status_code == 303
    assert anon.get("/dashboard").status_code == 200


def test_magic_link_is_single_use(anon, outbox):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    token = _token(outbox, "a@loge.test")
    assert anon.post(f"/auth/{token}").headers["location"] == "/dashboard"
    again = anon.post(f"/auth/{token}")
    assert "error=expired" in again.headers["location"]


def test_invalid_magic_link(anon):
    assert "error=expired" in anon.get("/auth/unbekannt").headers["location"]


def test_dashboard_requires_login(anon):
    assert anon.get("/dashboard").headers["location"] == "/login"


def test_login_gives_same_answer_for_unknown_email(anon, outbox):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    outbox.clear()
    known = anon.post("/login", data={"email": "a@loge.test"})
    unknown = anon.post("/login", data={"email": "nobody@loge.test"})
    assert known.status_code == unknown.status_code == 200
    assert len(outbox.to("a@loge.test")) == 1 and not outbox.to("nobody@loge.test")


def test_logout_only_via_post(orga):
    assert orga.get("/logout").status_code == 405
    assert orga.post("/logout").headers["location"] == "/login"


def test_logout_clears_cookie(orga):
    response = orga.post("/logout")
    assert "session=" in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"]


def test_rate_limit_per_email_is_silent(anon, outbox):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    for _ in range(10):
        assert anon.post("/login", data={"email": "a@loge.test"}).status_code == 200
    assert len(outbox.to("a@loge.test")) == 5  # Limit pro Stunde; keine Fehlermeldung als Orakel


def test_rate_limit_per_ip(anon):
    codes = [anon.post("/login", data={"email": f"u{i}@x.test"}).status_code for i in range(25)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


def test_session_cookie_flags(anon, outbox):
    anon.post("/register", data={"name": "L", "email": "a@loge.test"})
    token = _token(outbox, "a@loge.test")
    cookie = anon.post(f"/auth/{token}").headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # BASE_URL im Test ist http


def test_invalid_email_rejected_and_never_rendered_as_markup(anon, outbox):
    evil = "[Jetzt anmelden](https://evil.test)"
    for path in ("/login", "/register"):
        response = anon.post(path, data={"name": "x", "email": evil})
        assert response.status_code == 400
        assert "https://evil.test" not in response.text
    assert not outbox


def test_magic_sent_page_escapes_email(anon):
    response = anon.post("/login", data={"email": "a+<b>@loge.test"})
    assert "<b>" not in response.text


def test_register_confirmation_does_not_say_maybe_known(anon, outbox):
    new = anon.post("/register", data={"name": "L", "email": "a@loge.test"}).text
    again = anon.post("/register", data={"name": "L", "email": "a@loge.test"}).text
    for html in (new, again):
        assert "Falls die Adresse" not in html and "Wir haben eine E-Mail an a@loge.test gesendet" in html
