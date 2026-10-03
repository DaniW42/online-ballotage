from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone)
DISPLAY_FORMAT = "%d.%m.%Y %H:%M"


def utcnow() -> datetime:
    """Naive UTC-Zeit (so wird alles in der DB gespeichert)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_input_to_utc(value: str) -> datetime:
    """Wert eines <input type=datetime-local> (Lokalzeit) -> naive UTC."""
    naive_local = datetime.fromisoformat(value)
    return naive_local.replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def fmt_local(dt: datetime, fmt: str = DISPLAY_FORMAT) -> str:
    return to_local(dt).strftime(fmt)


def to_input_value(dt: datetime) -> str:
    """Für <input type=datetime-local> (Lokalzeit)."""
    return to_local(dt).strftime("%Y-%m-%dT%H:%M")
