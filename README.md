# ballotage.online

Selbst hostbares Tool für **anonyme Online-Abstimmungen** (z. B. Freimaurer-Kugelungen).
Passwortloser Login per Magic Link, Einladung per E-Mail, Einmal-Links zur Stimmabgabe –
und eine Datenstruktur, in der Stimme und Person **nicht verknüpfbar** sind.

Die Webseite enthält Startseite, Erklärung, Sicherheitsseite, FAQ, Selbst-Hosting-Anleitung,
Impressum und Datenschutzerklärung. Alle Texte stehen in Sprachdateien (derzeit Deutsch),
siehe [docs/TRANSLATING.md](docs/TRANSLATING.md).

## Lizenz

[Elastic License 2.0 (ELv2)](LICENSE) – Quelltext frei einsehbar, nutzbar, veränderbar und
weitergebbar, auch kommerziell. Einzige Einschränkung: Niemand darf die Software Dritten als
gehosteten/verwalteten Dienst anbieten, der im Wesentlichen dieselbe Funktionalität bereitstellt.
Das erfüllt nicht die Open Source Definition der OSI und ist daher als **„source-available“**
zu bezeichnen.

## Entwicklung

Stack: FastAPI, PostgreSQL, Jinja2, Docker Compose.

```
cp .env.example .env     # DB_PASSWORD, SECRET_KEY, BASE_URL=http://localhost:8000 ausfüllen
docker compose up -d --build
```

`docker compose up` lädt automatisch `docker-compose.override.yml` (Mailpit als Test-Mailserver,
Weboberfläche auf http://localhost:8025, SMTP-Werte sind dort vorbelegt). App: http://localhost:8000.

Tests (eigene Datenbank `kugelung_test`, Schema aus den Alembic-Migrationen):

```
make test
```

Schema-Änderungen laufen über Alembic (`migrations/`); der Container führt beim Start
`alembic upgrade head` aus.

## Produktivbetrieb

Zwei Varianten, ausführlich unter `/self-hosting` bzw. in `app/locales/de.json`:

- **Eigenständig mit automatischem HTTPS (Caddy):**
  `docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d --build`
  (`SITE_ADDRESS` in der `.env` setzen)
- **Hinter eigenem Reverse-Proxy (z. B. Nginx Proxy Manager):**
  `docker compose -f docker-compose.yml up -d --build`, Proxy auf Port 8000 des App-Containers.
  `/v/` und `/auth/` vom Access-Log ausnehmen, `FORWARDED_ALLOW_IPS` auf die Proxy-IP setzen.
  Port 8000 ist standardmäßig nur auf `127.0.0.1` veröffentlicht. Läuft NPM selbst als Container,
  am besten beide Stacks in ein gemeinsames Docker-Netz hängen und NPM auf `app:8000` zeigen
  lassen; alternativ `APP_BIND=0.0.0.0` und Port 8000 per Firewall auf den Proxy beschränken.

Mailpit (Override-Datei) startet dabei **nicht**. Vor dem öffentlichen Betrieb müssen die `LEGAL_*`-Angaben (Impressum) und `ADMIN_EMAIL` gesetzt sein.

Konfiguration über Umgebungsvariablen: siehe `.env.example` und `app/config.py`
(u. a. `TIMEZONE`, `ADMIN_EMAIL`, `TEST_MODE_MAX_VOTERS`, `TEST_MODE_DAILY_INVITATIONS`, `MAX_RECIPIENTS`,
`MAIL_MIN_INTERVAL_SECONDS`, `MIN_VOTERS`, `RETENTION_DAYS`, `APP_BIND`,
`REMINDER_HOURS_BEFORE`, `RATE_LIMIT_PER_EMAIL`, `RATE_LIMIT_PER_IP`, `LEGAL_*`).
`SECRET_KEY` muss mindestens 32 Zeichen lang sein, sonst startet die App nicht.

**Verifizierung:** Ist `ADMIN_EMAIL` gesetzt, laufen neue Logen im Testmodus (höchstens 3 Empfänger
je Abstimmung und 10 Einladungen pro Tag).
Im Konto können sie die Verifizierung beantragen (verantwortliche Person, E-Mail, Webseite oder Telefon);
der Admin erhält eine Mail mit geheimem Link, über den er die Loge freischaltet oder ablehnt und
löscht. Ohne `ADMIN_EMAIL` ist die Verifizierung aus. Notfalls per CLI:
`docker compose exec app python -m app.cli verify loge@example.org`.

**Mailversand:** global höchstens eine E-Mail pro Sekunde (`MAIL_MIN_INTERVAL_SECONDS`); große
Einladungsrunden dauern entsprechend.

Betrieb mit genau **einem** uvicorn-Worker (Wartungslauf und Rate-Limits liegen im Prozess).

## Sicherheitsrelevantes (für Entwickler)

- **Anonymität**: `votes` hat keine Fremdschlüssel-Beziehung zu `invitations` und **keine
  Zeitspalte**; `invitations` speichert nur `used` (Boolean) statt eines Zeitstempels. Diese
  Trennung darf nicht aufgeweicht werden (auch keine Logging-Korrelation von Token-Verbrauch
  und Stimmeneingang). Die Tests in `tests/test_anonymity.py` sichern das ab.
- **Postgres-Systemspalten**: Stimme und „hat abgestimmt“ entstehen in einer Transaktion und
  hätten sonst dieselbe `xmin` und benachbarte `ctid`. `scramble_row_versions()` schreibt bei
  jeder Stimmabgabe alle Zeilen der Abstimmung in zufälliger Reihenfolge neu. Jede neue
  Schreiboperation auf `votes`/`invitations` im Stimm-Kontext muss das berücksichtigen
  (`tests/test_security.py`). Nicht abgedeckt: WAL, physische Datenbankdateien vor dem VACUUM.
- **Sperrreihenfolge**: immer erst `elections`-Zeile, dann `invitations` – sonst Deadlocks.
- **CSRF**: POSTs mit `Sec-Fetch-Site: cross-site/same-site` oder fremdem `Origin` werden abgelehnt.
- **Access-Logs**: Der App-Container startet mit `--no-access-log`; Proxy-Logs für `/v/*` und
  `/auth/*` müssen ebenfalls abgeschaltet sein.
- **Tokens**: `secrets.token_urlsafe(32)`, serverseitig nur als SHA-256-Hash gespeichert.
- **Nach Abschluss eingefroren**: kein Verlängern/Nachladen/Neu-Ausstellen mehr, sonst wäre
  eine Stimme aus der Differenz zweier Zwischenstände ableitbar.
- **Strikte CSP**: keine Inline-Skripte/-Styles; Skripte liegen in `app/static/js/`.

## Offene Punkte

- Mehrere Administratoren pro Loge
- Kryptografische Entkopplung von Token und Stimme (Blind Signatures), damit auch der
  laufende Server Stimme und Person nicht verknüpfen kann
