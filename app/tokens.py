import hashlib
import re
import secrets

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def extract_emails(raw_text: str) -> list[str]:
    """Pickt Email-Adressen aus beliebig formatiertem Freitext (Namen,
    Klammern, Kommas, Zeilenumbrüche egal) statt stur zu splitten."""
    found = EMAIL_PATTERN.findall(raw_text or "")
    return sorted(set(e.lower() for e in found))


def merge_emails(existing: list[str], raw_text: str) -> list[str]:
    """Für den 'mehrere Blöcke nacheinander einfügen'-Flow: neue Treffer
    additiv und dedupliziert in die bestehende Liste einfügen."""
    new_emails = extract_emails(raw_text)
    return sorted(set(existing) | set(new_emails))


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch((email or "").strip()))


def generate_token() -> tuple[str, str]:
    """Gibt (raw_token, token_hash) zurück. Nur der Hash wird je gespeichert,
    das raw_token existiert nur im Email-Link."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
