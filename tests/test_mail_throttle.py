import smtplib

from app import mail
from app.mail import MailThrottle
from conftest import ORIGINAL_SEND_MAIL


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def _throttle(interval=1.0):
    fake = FakeClock()
    return MailThrottle(interval, clock=fake.clock, sleep=fake.sleep), fake


def test_first_mail_is_not_delayed_and_following_ones_wait():
    throttle, fake = _throttle()
    for _ in range(4):
        throttle.wait()
    assert fake.sleeps == [1.0, 1.0, 1.0]  # erste sofort, danach jeweils 1 s Abstand


def test_no_wait_when_enough_time_passed():
    throttle, fake = _throttle()
    throttle.wait()
    fake.now += 5
    throttle.wait()
    assert fake.sleeps == []


def test_partial_wait_only_for_the_remaining_time():
    throttle, fake = _throttle()
    throttle.wait()
    fake.now += 0.4
    throttle.wait()
    assert fake.sleeps == [0.6]


def test_interval_zero_disables_throttle():
    throttle, fake = _throttle(0)
    for _ in range(3):
        throttle.wait()
    assert fake.sleeps == []


def test_real_send_mail_goes_through_global_throttle(monkeypatch):
    """Alle Mailarten laufen über send_mail() und damit über dieselbe Drosselung."""
    throttle, fake = _throttle()
    monkeypatch.setattr(mail, "throttle", throttle)

    class DummySMTP:
        sent = []

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            DummySMTP.sent.append(msg["To"])

    monkeypatch.setattr(smtplib, "SMTP", DummySMTP)
    monkeypatch.setattr(mail.settings, "smtp_use_tls", False)
    for i in range(3):
        ORIGINAL_SEND_MAIL(f"u{i}@x.test", "Betreff", "Text")
    assert DummySMTP.sent == ["u0@x.test", "u1@x.test", "u2@x.test"]
    assert fake.sleeps == [1.0, 1.0]


def test_default_interval_is_one_second():
    assert mail.settings.mail_min_interval_seconds == 1.0
