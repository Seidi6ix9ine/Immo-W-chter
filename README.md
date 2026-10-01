# Immo-Wächter Wien

Das Programm fragt regelmäßig willhaben, ImmoScout24, immodirekt, derStandard Immobilien, immowelt und WG-Gesucht nach neuen Mietwohnungen in Wien ab. Passt ein Inserat zu deinen Vorgaben, kommt eine Nachricht aufs Handy. Jedes Inserat wird nur einmal gemeldet, und steht dieselbe Wohnung auf mehreren Portalen (gleiche PLZ, Miete und Fläche), kommt sie auch nur einmal. Beim allerersten Lauf kommt nur eine Startmeldung, damit du nicht mit hunderten alten Inseraten zugeschüttet wirst.

Voreingestellt sind zwei Suchprofile: eine WG-Wohnung (mind. 4 Zimmer, also 3 Schlafzimmer plus Wohnzimmer) und eine Garçonniere, beide in den Bezirken rund um die WU. Preis, Fläche und Bezirke änderst du in `config.yaml`.

## 1. Benachrichtigung einrichten (eine Variante reicht)

**ntfy (am schnellsten, kein Konto nötig)**
1. App „ntfy“ aus dem App Store / Play Store installieren.
2. In der App „Subscribe to topic“ und einen langen, zufälligen Namen ausdenken, z.B. `sandro-wohnung-k82mxq4`. Wer den Namen kennt, kann mitlesen, also nichts Erratbares nehmen.
3. Genau diesen Namen später als `NTFY_TOPIC` eintragen.

**Telegram**
1. In Telegram `@BotFather` anschreiben, `/newbot` senden, Namen vergeben. Du bekommst einen Token wie `123456:ABC-...`.
2. Deinem neuen Bot irgendeine Nachricht schicken.
3. Im Browser `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen. Bei `"chat":{"id":...}` steht deine Chat-ID.
4. Token und Chat-ID später als `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` eintragen.

## 2. Suche anpassen

`config.yaml` hat zwei Teile.

Unter **`searches`** stehen die Seiten, die abgerufen werden, eine pro Portal. Die vorgegebenen Adressen zeigen alle Mietwohnungen in Wien. Schneller und genauer wird es, wenn du auf dem Portal selbst Filter setzt (Preis bis, Zimmer ab, Bezirke), wenn möglich nach „neueste zuerst“ sortierst und die Adresse aus dem Browser bei `url:` einsetzt. Ein Portal, das du nicht willst, schaltest du mit `active: false` ab.

Unter **`profiles`** steht, was dich interessiert. Jedes gefundene Inserat wird gegen jedes Profil geprüft. Die Zeilen mit `<-- ANPASSEN` durchgehen. Weitere Filter: `include_keywords` (mindestens eines dieser Wörter muss vorkommen, z.B. `balkon`), `exclude_keywords`, `max_area`, `min_price`, `sources` (Profil nur für bestimmte Portale). Inserate ohne Preis (meist Neubauprojekte) werden übersprungen, außer du setzt `allow_missing_price: true`. Fehlen Fläche, Zimmerzahl oder Bezirk, wird das Inserat sicherheitshalber trotzdem gemeldet.

Zum Überprüfen, ob alle Portale gelesen werden:

```
python immo_watch.py --check
```

Das ruft jede Suche einmal ab und zeigt pro Portal die ersten drei erkannten Inserate mit Preis, Fläche, Zimmern und PLZ. Nichts wird gesendet oder gespeichert.

## 3a. Laufen lassen über GitHub (kostenlos, PC kann aus sein)

1. Auf github.com ein **privates** Repository anlegen und alle Dateien dieses Ordners hochladen (auch den Ordner `.github/workflows`).
2. Settings → Secrets and variables → Actions → „New repository secret“: `NTFY_TOPIC` bzw. `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` anlegen.
3. Reiter „Actions“ → „Immo-Wächter“ → „Run workflow“. Nach ein, zwei Minuten sollte die Startmeldung am Handy sein. Sie sagt dir auch, wie viele Portale funktioniert haben („5 von 6 Suchen ok“).

Danach läuft es ungefähr alle 30 Minuten von selbst. In `seen.json` merkt es sich, was schon gemeldet wurde.

Einige Portale sperren Server-Adressen wie die von GitHub. Welche das sind, sieht man erst beim echten Lauf: Im Log des Laufs steht bei jedem Portal „X Inserate erkannt“ oder ein Fehler, und nach 6 Fehlschlägen hintereinander kommt eine Warnung aufs Handy. Blockiert ein wichtiges Portal dauerhaft, nimm Variante 3b.

## 3b. Laufen lassen am eigenen PC

Python 3 installieren, dann im Ordner:

```
pip install -r requirements.txt
python immo_watch.py --test-notify      # prüft, ob Nachrichten ankommen
python immo_watch.py --check            # prüft, ob alle Portale gelesen werden
python immo_watch.py --dry-run          # zeigt, was aktuell passen würde, ohne zu senden
python immo_watch.py --loop 1200        # läuft dauerhaft, prüft alle ~20 Minuten
```

Bei lokaler Nutzung kannst du Token/Topic direkt in `config.yaml` unter `notify:` eintragen. Das Fenster muss offen bleiben. Alternativ unter Windows die Aufgabenplanung `python immo_watch.py` alle 20 Minuten starten lassen. Von einem Heimanschluss aus blockieren die Portale deutlich seltener als bei GitHub.

## Hinweise zu den Portalen

- **willhaben** liefert die Daten sauber strukturiert, dort ist die Erkennung am verlässlichsten.
- **ImmoScout24 und immodirekt** gehören zusammen und zeigen großteils dieselben Inserate. immodirekt lädt weitere Seiten nur per JavaScript, deshalb wird dort nur die erste Seite gelesen.
- **immowelt** zeigt oft die Nettokaltmiete ohne Betriebskosten. Die echte Monatsmiete liegt dann ein paar hundert Euro höher. Wenn dich das stört, für immowelt ein eigenes Profil mit niedrigerem `max_price` und `sources: ["immowelt"]` anlegen.
- **WG-Gesucht** zeigt automatischen Abrufen häufig ein Captcha. Kommt dauernd die Warnung, dort `active: false` setzen.
- **Andere Seiten** lassen sich ohne Programmieren ergänzen, wenn die Inserate als normale Links auf der Ergebnisseite stehen: siehe das Beispiel mit `link_pattern` in `config.yaml`.

Alle Portale außer willhaben werden über die Ergebnisseite gelesen: Das Programm sucht die Links zu Inseraten und liest Preis, m², Zimmer und PLZ aus dem Text drumherum. Das überlebt kleinere Umbauten der Seiten, aber nicht jeden. Liefert ein Portal auf Dauer „Keine Inserate erkannt“, muss dort das Linkmuster in `PORTALS` in `immo_watch.py` angepasst werden.

Nicht öfter als alle 15–20 Minuten abfragen. Häufiger bringt kaum etwas und erhöht das Risiko, gesperrt zu werden.
