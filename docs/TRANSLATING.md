# Neue Sprache hinzufügen

Alle sichtbaren Texte (Webseite, App, E-Mails) stehen in `app/locales/<code>.json`.
Der Code kennt keine festen Texte.

1. `app/locales/de.json` nach `app/locales/<code>.json` kopieren (z. B. `en.json`).
2. In der Kopie alle Werte übersetzen. **Schlüssel nicht ändern.**
   - `meta.name` ist der Anzeigename im Sprachumschalter, `meta.html_lang` das `lang`-Attribut.
   - `meta.datetime_format` / `meta.date_format` bestimmen die Datumsdarstellung (`strftime`).
   - Platzhalter wie `{title}` oder `{n}` müssen erhalten bleiben.
   - Texte unterstützen minimales Markup: `**fett**`, `` `code` ``, `[Text](/pfad)`, Listen mit `- `.
   - `election.new.options_default` sind die Standard-Optionen der Abstimmung (kommagetrennt).
3. `make test` ausführen. Die Tests prüfen, dass alle Schlüssel und Platzhalter vorhanden sind.
4. Fertig: Der Sprachumschalter im Footer erscheint automatisch, sobald mehr als eine Sprache existiert.

Sprachwahl: Cookie `lang` (über `/lang/<code>`), sonst `Accept-Language` des Browsers, sonst Deutsch.
Fehlt ein Schlüssel in einer Sprache, wird der deutsche Text angezeigt.
E-Mails und die Abstimmungsseiten der Wähler erscheinen in der Sprache, in der die Abstimmung angelegt wurde.
