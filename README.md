# online-ballotage

Minimalistisches, selbstgehostetes Tool für anonyme Online-Abstimmungen
(z. B. Freimaurer-Kugelungen). Passwortloser Login (Magic Link), Freitext-
Email-Erfassung, Token-basierte Einmal-Stimmabgabe ohne Verknüpfung
zwischen Person und Stimme in der Datenbank.

## Lizenz

[Elastic License 2.0 (ELv2)](LICENSE) – Quellcode frei einsehbar,
nutzbar, veränderbar und weitergebbar, auch kommerziell. Einzige
Einschränkung: Niemand darf die Software Dritten als gehosteten/verwalteten
Dienst anbieten, der im Wesentlichen dieselbe Funktionalität bereitstellt
(verhindert z. B. das 1:1-Weiterbetreiben hinter einer eigenen Paywall).
Das erfüllt nicht die Open Source Definition der OSI (die schließt jede
Nutzungsbeschränkung aus) und ist daher korrekt als **"Source-available"**
statt "Open Source" zu bezeichnen.

## Setup

1. `.env.example` nach `.env` kopieren und ausfüllen:
   - `DB_PASSWORD`: langes Zufallspasswort
   - `SECRET_KEY`: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`
   - `BASE_URL`: öffentliche URL der Instanz (für Links in Emails)
   - SMTP-Zugangsdaten eurer bestehenden Mail-Infrastruktur

2. Stack starten:
   ```
   docker compose -f docker-compose.yml --env-file .env up -d --build
   ```
   Wichtig: `-f docker-compose.yml` lädt `docker-compose.override.yml`
   (Mailpit, nur für die Entwicklung) bewusst nicht. Lokal reicht
   `docker compose up -d --build`; Mails erscheinen dann auf
   http://localhost:8025.
   Tabellen werden beim ersten Start automatisch angelegt (kein separater
   Migrationsschritt für den aktuellen Stand nötig).

3. Hinter Nginx Proxy Manager: Proxy-Host auf den `app`-Container,
   Port 8000, SSL aktivieren. Danach in `docker-compose.yml` den
   `ports`-Block entfernen, da der Zugriff dann über NPM läuft.

## Sicherheitsrelevantes

- **Anonymität**: `votes`-Tabelle hat keine Fremdschlüssel-Beziehung zu
  `invitations`. Diese Trennung ist absichtlich und darf bei Erweiterungen
  nicht aufgeweicht werden (z. B. keine Logging-Korrelation von
  Token-Verbrauch und Stimmeneingang im selben Request-Log).
- **Zeitstempel**: Weder `votes` noch `invitations` speichern einen
  Zeitpunkt der Stimmabgabe (`invitations.used` ist nur ein Boolean),
  damit sich Stimme und Person nicht über Zeitnähe zuordnen lassen.
- **Access-Logs**: Der App-Container startet mit `--no-access-log`
  (sonst stünden Roh-Tokens samt Zeitpunkt im Log). Reverse-Proxy-Logs (NPM/Nginx) können Tokens aus der
  URL mitschreiben. Für produktiven Einsatz Log-Format für `/v/*` und
  `/auth/*` anpassen oder diese Pfade vom Access-Log ausnehmen.
- **Tokens**: `secrets.token_urlsafe(32)`, serverseitig nur als SHA-256-Hash
  gespeichert. Magic Links und Wahl-Links sind strukturell identisch,
  nur mit unterschiedlicher Gültigkeitsdauer.

## Offene Punkte (bewusst nicht im MVP)

- Erinnerungsmails vor Fristende (Feld `reminder_enabled` existiert bereits
  im Datenmodell, es fehlt der Cron-/Scheduler-Job, der es ausliest)
- Opt-in-Verifizierungsprozess für Logen (aktuell nur das `verified`-Flag
  im Datenmodell, kein Antragsworkflow)
- Alembic-Migrationen (aktuell `create_all` beim Start – für den MVP
  ausreichend, bei Schema-Änderungen im Produktivbetrieb nachrüsten)
