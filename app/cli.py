"""Kleine Admin-CLI, z. B.: docker compose exec app python -m app.cli verify loge@example.org"""
import sys

from .db import SessionLocal, Organization


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("verify", "unverify"):
        print("Aufruf: python -m app.cli verify|unverify <email>")
        return 2
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.email == argv[2].strip().lower()).first()
        if not org:
            print("Keine Loge mit dieser Email gefunden.")
            return 1
        org.verified = argv[1] == "verify"
        db.commit()
        print(f"{org.name}: verified={org.verified}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
