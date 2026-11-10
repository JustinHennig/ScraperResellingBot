# ResellingBot — Kleinanzeigen Monitor

Überwacht Kleinanzeigen automatisch und sendet Telegram-Benachrichtigungen bei guten Wiederverkaufsangeboten für Smartphones. Unterstützt iPhone 12–16e, Samsung S21–S25 / A-Serie und Google Pixel 7–9.

Benachrichtigungen werden über die **Telegram Bot API** versendet — inklusive Fernsteuerung (stop/start/status per Nachricht) ganz ohne öffentlich erreichbaren Webhook, da per Long Polling abgefragt wird. Optional bewertet eine **Groq AI** jedes Inserat mit einem Wiederverkaufscore (1–10) und warnt vor nicht-originalen Ersatzteilen.

Die Oberfläche ist ein lokales Web-Dashboard — kein separates Tool notwendig.

---

## Einrichtung

### 1. Python & Abhängigkeiten installieren

```bash
pip install -r requirements.txt
```

### 2. credentials.json anlegen

Erstelle eine Datei `credentials.json` im Projektordner (wird von Git ignoriert):

```json
{
  "groq_api_key": "dein-groq-api-key",
  "telegram": {
    "bot_token": "dein-telegram-bot-token",
    "chat_id": ["123456789"]
  }
}
```

- **Telegram Bot**: Bei [@BotFather](https://t.me/BotFather) in Telegram `/newbot` schicken, Namen vergeben — du bekommst den `bot_token`.
- **chat_id**: Schick deinem neuen Bot irgendeine Nachricht, dann `https://api.telegram.org/bot<TOKEN>/getUpdates` im Browser öffnen — die `chat.id` steht in der Antwort. Alternativ Nachricht an [@userinfobot](https://t.me/userinfobot) schicken, um die eigene ID zu erfahren.
- **Groq API Key**: Kostenlos auf [console.groq.com](https://console.groq.com) erstellen. Wenn kein Key angegeben wird, läuft der Bot ohne KI-Scoring.
- `chat_id` kann eine einzelne ID/String oder eine Liste von IDs sein — für mehrere Empfänger.

### 3. config.json anpassen

`config.json` enthält alle Suchen und allgemeinen Einstellungen. Die Datei ist bereits mit gängigen iPhone-, Samsung- und Pixel-Modellen vorkonfiguriert. Suchen können einzeln aktiviert/deaktiviert werden.

```json
{
  "searches": [
    {
      "name": "iPhone 13",
      "query": "iphone 13",
      "max_price": 130,
      "min_price": 35,
      "keywords_blocked": ["mini", "pro"],
      "enabled": true
    }
  ],
  "settings": {
    "check_interval_minutes": 2,
    "max_workers": 6,
    "seen_listings_file": "data/seen_listings.json",
    "log_file": "logs/bot.log",
    "keywords_blocked": [
      "defekt",
      "bastler",
      "ersatzteil",
      "gesperrt",
      "icloud"
    ]
  },
  "telegram": {}
}
```

> Sensitive Felder (`bot_token`, `chat_id`, `groq_api_key`) gehören in `credentials.json`, nicht in `config.json`.

### 4. Bot starten

```bash
python server.py
```

Öffne dann **http://localhost:5000** im Browser. Über das Dashboard kannst du den Bot starten/stoppen, das Prüfintervall ändern und einzelne Suchen ein-/ausschalten.

---

## Konfigurationsoptionen

### Suche (`searches[]`)

| Feld                | Beschreibung                                         | Beispiel          |
| ------------------- | ---------------------------------------------------- | ----------------- |
| `name`              | Anzeigename in Benachrichtigungen                    | `"iPhone 13"`     |
| `query`             | Suchbegriff für Kleinanzeigen                        | `"iphone 13"`     |
| `max_price`         | Maximaler Preis in € (0 = kein Limit)                | `130`             |
| `min_price`         | Minimaler Preis in € (filtert Spam-Inserate)         | `35`              |
| `keywords_required` | Alle Keywords müssen im Titel/Beschreibung vorkommen | `["256gb"]`       |
| `keywords_blocked`  | Modellvarianten, die diese Suche ausschließt — zählen nur direkt am Modellnamen („iPhone 13 **Pro**“), nicht irgendwo in der Beschreibung | `["mini", "pro"]` |
| `category_id`       | Kleinanzeigen-Kategorie (173 = Handy & Telekom)      | `173`             |
| `category_filter`   | Zusatzfilter zur Kategorie, z.B. nur eine Marke      | `"handy_telekom.art_s:apple"` |
| `enabled`           | Suche aktiviert/deaktiviert                          | `true`            |

### Einstellungen (`settings`)

| Feld                     | Beschreibung                                              | Standard                   |
| ------------------------ | --------------------------------------------------------- | -------------------------- |
| `check_interval_minutes` | Prüfintervall in Minuten                                  | `2`                        |
| `max_workers`            | Parallele Such-Threads                                    | `6`                        |
| `seen_listings_file`     | Pfad zur Datei mit bereits gesehenen IDs                  | `data/seen_listings.json`  |
| `log_file`               | Pfad zur Logdatei                                         | `logs/bot.log`             |
| `keywords_blocked`       | Harte Blacklist (Sperren, Fälschungen, Wasserschaden, nur Abholung …) — gilt für alle Suchen, überall im Inserat. Verneinungen wie „kein Wasserschaden“ oder „iCloud frei“ werden erkannt | `["defekt", "bastler", …]` |
| `defect_keywords`        | Defekte (Riss, Sprung, Pixelfehler, schwarze Flecken, „defekt“ …). Solche Inserate werden nur gemeldet, wenn sie mindestens `defect_discount_percent` unter dem `max_price` der Suche liegen. Ein „Defekt“ an Ladebuchse/Lightning/USB-C oder ein Wackelkontakt zählt nicht | `["sprung", "pixelfehler", …]` |
| `defect_discount_percent`| Mindestrabatt in %, ab dem defekte Geräte trotzdem gemeldet werden | `30` |
| `new_seller_max_age_days`| Konten, die jünger als so viele Tage sind, werden übersprungen | `5` |
| `pickup_only_keywords`   | Formulierungen für „nur Abholung“ („nur Selbstabholer“, „kein Versand“ …). Inserate mit diesen Formulierungen werden nur im `pickup_area` gemeldet. Die Versandart „Nur Abholung“ allein zählt nicht, weil Verkäufer sie oft willkürlich setzen | `["nur abholung", …]` |
| `pickup_area`            | Umkreis, in dem Abholung in Frage kommt (Abstand zwischen PLZ-Mittelpunkten) | `{"postcode": "60438", "radius_km": 5}` |
| `autostart`              | Bot nach Neustart von Server/Pi automatisch starten        | `true`                     |
| `groq_api_key`           | Groq API Key (alternativ in `credentials.json` eintragen) | `""`                       |
| `ai_score_skip_threshold`| KI-Score (1-10), bei dem/darunter ein Fund automatisch übersprungen statt gemeldet wird | `2` |

---

## Funktionsweise

- Pro Prüfzyklus werden bis zu **3 Seiten** Suchergebnisse gescannt — es werden nur Inserate der letzten 30 Minuten weiterverarbeitet.
- Alle Suchen laufen **parallel** (Thread Pool mit `max_workers` Threads).
- Für jedes vielversprechende Inserat wird die **Detailseite** aufgerufen, um die vollständige Beschreibung und das Konto-Erstellungsdatum des Verkäufers zu lesen.
- **Neues Verkäuferkonto** (jünger als `new_seller_max_age_days`, Standard 5 Tage) → Inserat wird übersprungen.
- **Nur Abholung** (laut Titel oder Beschreibung) → nur gemeldet, wenn der Ort im `pickup_area` liegt; die Nachricht zeigt dann „📍 Nur Abholung — Ort (Entfernung)“. PLZ-Koordinaten: `bot/plz_de.csv` aus [GeoNames](https://www.geonames.org/) (CC BY 4.0).
- **Defekte Geräte** (Riss, Pixelfehler, …) werden gemeldet, wenn sie mindestens 30 % (`defect_discount_percent`) unter dem Höchstpreis (`max_price`) der Suche liegen — die Nachricht zeigt dann „🔧 Defekt: …“.
- Optional bewertet die **Groq AI** das Inserat mit einem Score von 1–10 und gibt eine Warnung aus, falls Displaytausch, Akkutausch, physische Schäden oder Graumarkt-/Fälschungshinweise (z.B. „chinesische Version") erkannt werden. Bei Display-/Rückseitenschaden rechnet die KI den Wiederverkaufspreis nur mit ~60% des eBay-Medians. Ein Score ≤ `ai_score_skip_threshold` (Standard `2`) führt zum automatischen Überspringen statt einer Meldung.
- Angebote mit unsicheren Zahlungsmethoden (nur PayPal Freunde, nur Überweisung) werden immer übersprungen — unabhängig vom Groq-Key.
- 👍/👎-Bewertungen aus Telegram werden in `data/ratings.json` gespeichert und unter `/ratings` im Dashboard angezeigt. Ein automatischer Filter auf Basis der Bewertungen ist derzeit deaktiviert.
- Benachrichtigungen werden als Telegram-Nachricht mit Bild versendet; bei fehlender Abbildung nur als Text.
- `data/seen_listings.json` speichert pro Suche, welche Inserate schon geprüft wurden, und welche schon gemeldet wurden (keine doppelten Meldungen). Einträge älter als 7 Tage werden beim Start bereinigt.
- Bezahlte TOP-Anzeigen (ohne Datum, immer oben angeheftet) beenden das Blättern nicht — nur datierte Inserate entscheiden, ob weitere Seiten geladen werden.

---

## Projektstruktur

```
ResellingBot/
├── server.py               — Flask-Webserver & Bot-Steuerung (Einstiegspunkt)
├── config.json             — Suchen, Einstellungen, Telegram-Platzhalter
├── credentials.json        — Sensitive Zugangsdaten (gitignored)
├── requirements.txt        — Python-Abhängigkeiten
├── bot/
│   ├── main.py             — Orchestrierung, Config-Laden, Seen-Listings
│   ├── scraper.py          — Kleinanzeigen-Scraper & Angebotsfilter
│   ├── notifier.py         — Telegram Bot API Benachrichtigungen
│   └── ai_scorer.py        — Groq AI Scoring (1–10)
├── frontend/
│   ├── index.html          — Dashboard (Start/Stop, Suchen, Status)
│   ├── log.html / log.js   — Log-Viewer
│   ├── main.js             — Dashboard-Logik
│   └── style.css           — Styles
├── data/
│   └── seen_listings.json  — Automatisch erstellt; bereits gesehene IDs
└── logs/
    └── bot.log             — Automatisch erstellt; Logdatei
```

---

## Deployment auf dem Raspberry Pi

Der Pi aktualisiert sich selbst: `resellingbot-update.timer` startet alle 5 Minuten `update.sh`. Gibt es neue Commits auf `origin/main`, holt das Skript sie per Fast-Forward, installiert `requirements.txt` ins `venv` und startet `resellingbot.service` neu.

- Neuer Code geht also live, sobald er auf `main` gepusht ist — spätestens nach 5 Minuten.
- Kompiliert der neue Code nicht, bleibt der Pi auf dem alten Stand und der Lauf schlägt fehl.
- Lokale Änderungen auf dem Pi blockieren das Update (Fast-Forward schlägt fehl), statt überschrieben zu werden.
- Schlägt ein Schritt fehl (Kompilieren, `pip install`, Neustart), geht der Pi auf den alten Stand zurück und versucht es beim nächsten Lauf erneut.
- Änderungen per Telegram oder Dashboard (Preise, Suchen an/aus, neue Suchen) landen in `data/config_overrides.json`, nicht in `config.json`. So bleibt der Git-Stand auf dem Pi sauber, und Live-Änderungen gelten vor `config.json`.
- Protokoll: `journalctl -u resellingbot-update.service`

---
## Hinweise

- `data/seen_listings.json` verhindert Doppel-Benachrichtigungen, auch nach Neustart.
- Beim ersten Start werden alle aktuell vorhandenen Inserate als „gesehen" markiert — nur _neue_ Inserate lösen Benachrichtigungen aus.
- Respektiere die Nutzungsbedingungen von Kleinanzeigen. Zu kurze Prüfintervalle können zu temporären Sperren führen (empfohlen: ≥ 2 Minuten).
- Stop/Start/Status per Telegram-Nachricht funktioniert nur von Chat-IDs, die in `chat_id` eingetragen sind — Nachrichten von anderen Chats werden ignoriert.
