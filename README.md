# Trading Backtester (Django)

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python manage.py makemigrations backtester && python manage.py migrate
    python manage.py runserver

Im Formular "Synthetisch (offline)" wählen, um ohne Börsen-API zu testen.

## Börsendaten: Cache, Archiv, Sperren-Schutz
Gegen IP-Sperren der Börsen (z. B. Binance HTTP 418 / Code -1003 auf geteilten Render-IPs):
- **Kerzen-Cache** in der Datenbank (`Candle`, `CandleCoverage`): Abgeschlossene Kerzen werden einmal geladen. Spätere Läufe holen nur fehlende Zeiträume. Nutzt Neon/Postgres, auf Render darf die Datenbank nicht flüchtig sein.
- **Binance-Archiv:** Vergangene Tage kommen von `data.binance.vision` (Download, keine API-Limits). Die API wird nur für die jüngsten Tage genutzt.
- **Sperren-Schutz** (`ExchangeBlock`): Eine erkannte Sperre wird gemerkt (bei Binance mit der genauen Zeit aus der Fehlermeldung). Bis dahin gehen keine Anfragen mehr an die Börse. Ein Hinweis erscheint über dem Formular.
- **Ein Client je Prozess**, ein API-Abruf je Börse gleichzeitig, Märkte nur einmal je 12 Stunden (bei Binance gar nicht).
- Zum manuellen Aufheben einer Sperre im Neon-SQL-Editor: `delete from backtester_exchangeblock;`
- Die Log-Zeile `tradebot.marketdata: … (Cache n, Archiv n, API n)` zeigt je Lauf, woher die Kerzen kamen.

## PostgreSQL (Neon) und Render
Die App liest die Datenbank aus der Umgebungsvariable `DATABASE_URL`. Ohne sie läuft lokal SQLite wie bisher.
- **Treiber:** `psycopg[binary]` (psycopg 3) und `dj-database-url` stehen in `requirements.txt`. psycopg 3 ist der Treiber, den Django bevorzugt. Ein zusätzliches `psycopg2` wäre überflüssig und würde ignoriert.
- **Render:** Nötig ist nur `DATABASE_URL` (Neon-Verbindungsstring). `SECRET_KEY` und `DEBUG` müssen nicht gesetzt werden: Auf Render ist DEBUG automatisch aus, der Secret Key wird stabil aus `DATABASE_URL` abgeleitet (nicht im Code). Wer will, kann beide trotzdem als Umgebungsvariablen setzen, sie haben Vorrang. Host und CSRF-Herkunft kommen aus `RENDER_EXTERNAL_HOSTNAME`.
- **Migrationen:** Beim Start wendet `config/wsgi.py` fehlende Migrationen automatisch an (mit Postgres-Sperre, damit mehrere Worker sich nicht stören). Ein Build-Schritt oder eine Shell ist nicht nötig. Abschalten mit `AUTO_MIGRATE=0`, lokal einschalten mit `AUTO_MIGRATE=1`. Im Render-Log steht dann „Migrationen angewendet: …“.
- **Build-Befehl:** `pip install -r requirements.txt`
- **Start-Befehl:** `gunicorn config.wsgi`
- **Fehler im Log:** Auch mit DEBUG aus erscheinen Tracebacks im Render-Log (Logs-Reiter).
- **Neon:** SSL wird erzwungen. Der Pooler-Endpunkt (Host mit `-pooler`) funktioniert; serverseitige Cursor sind dafür abgeschaltet. Treten bei `migrate` über den Pooler Probleme auf, `migrate` einmalig mit der direkten Verbindung (ohne `-pooler`) ausführen.
- **Migrationen:** laufen automatisch einmalig im Gunicorn-Master (`gunicorn.conf.py`), lokal vor `runserver` (abschaltbar mit `AUTO_MIGRATE=0`). Hängt der Start trotzdem, als Start-Befehl `python manage.py migrate && gunicorn config.wsgi` setzen und `AUTO_MIGRATE=0` .
- **Aufwachen:** Neon pausiert inaktive Datenbanken. Die erste Anfrage danach kann einige Sekunden dauern (Timeout 15 s), tote Verbindungen werden automatisch ersetzt.
- **Admin:** Ohne Shell lässt sich kein Superuser anlegen; die App selbst braucht den Admin nicht. Statische Dateien des Admins werden ohne WhiteNoise nicht ausgeliefert.

## TA-Lib
Steht in `requirements.txt` und wird bei `pip install -r requirements.txt` installiert. Aktuelle Versionen bringen die C-Bibliothek in den Wheels mit. Scheitert die Installation (z. B. sehr alte Python-Version), die Zeile `TA-Lib` in `requirements.txt` entfernen; dann nutzt `indicators.py` einen pandas-Ersatz. SMA ist identisch, der RSI weicht in den ersten Kerzen leicht ab (ab ca. 100 Kerzen praktisch gleich).
Ältere Systeme: Ubuntu/Debian `sudo apt install libta-lib-dev`, macOS `brew install ta-lib`, Windows passendes Wheel.

Welche Bibliothek gerechnet hat (TA-Lib oder pandas-Ersatz), steht in der Kopfzeile jedes Laufs unter „Indikatoren“.

## Zeitraum
Start- und Enddatum frei wählbar (Enddatum eingeschlossen, 30 bis 1500 Tage, nicht in der Zukunft). Es werden nur abgeschlossene Kerzen verwendet. Synthetische Kurse sind datumsfest: derselbe Tag hat immer denselben Kurs.

## Struktur
- `data.py`        Preisdaten (ccxt / synthetisch)
- `indicators.py`  SMA, RSI (TA-Lib oder pandas)
- `strategies.py`  Signale (SMA-Crossover, RSI)
- `engine.py`      Backtest + Kennzahlen (shift(1) gegen Look-ahead, Gebühren)
- `validation.py`  Train/Test-Split und Walk-Forward (Parameter nur auf Train wählen, nur auf ungesehenen Daten bewerten)
- `models.py`      BacktestRun speichert Parameter, Metriken, Equity-Kurven
- `views.py`       Formular -> Backtest -> Ergebnisseite mit Plotly

## Modi
- **Einzelner Backtest**: manuelle Parameter über den gesamten Zeitraum
- **Train/Test-Split**: Grid-Search auf Train, einmalige Bewertung auf Test, Heatmap
- **Walk-Forward**: rollierend trainieren/testen; die Testfenster ergeben eine reine Out-of-Sample-Kurve

## Oberfläche
Oben Auswahl Bitcoin / Solana / Ethereum, darunter drei Spalten (1: Einstellungen und Kennzahlen, 2: Charts und Trades, 3: gelaufene Auswertungen je Chain), unten die Legende.
Neben jedem Eingabefeld, der Chain-Auswahl und den Start-Buttons steht ein i-Symbol: Mit der Maus darüberfahren (am Handy antippen, mit der Tastatur anspringen) zeigt, was der Begriff bedeutet und wofür er gut ist. Die Texte stehen gesammelt in `backtester/forms.py` (`HELP`, `PARAM_TIPS`, `SIZE_TIPS`, `UI_TIPS`); die Erklärungen der Parameter 1 bis 3 wechseln mit der gewählten Strategie.

## Börsen, Hintergrund-Berechnung, Chain-Vergleich
- **Börse** (Dropdown): Binance, Kraken, Coinbase, Bybit, OKX, Bitstamp über ccxt. Fehlt ein USDT-Paar, wird USD bzw. USDC genutzt. Begrenzt eine Börse die Historie, erscheint ein Datenhinweis.
- **Hintergrund**: Backtests laufen im Thread-Pool (`jobs.py`), die Seite aktualisiert sich selbst. Für Produktivbetrieb kann `jobs.submit()` durch Celery oder django-q ersetzt werden, `compute()` bleibt gleich.
- **Chain-Vergleich**: Button "Auf allen 3 Chains vergleichen" startet dieselben Einstellungen auf BTC, SOL und ETH.

## Strategien und Vergleiche
- **SMA-Crossover**, **RSI** und **Kombiniert (SMA + RSI)**. Kombiniert hat zwei Verknüpfungen: *Trendfilter + RSI-Einstieg* (Standard: Kauf nur im Aufwärtstrend bei RSI unter der Einstiegsschwelle, Verkauf bei RSI über der Ausstiegsschwelle oder Trendbruch) und *ODER* (long, wenn SMA oder RSI long sind). Die Grid-Search optimiert nur den SMA-Teil (34 gültige Kombinationen), der RSI-Teil bleibt fest, um Überanpassung zu begrenzen.
- **Strategien vergleichen**: SMA, RSI und Kombiniert mit denselben Einstellungen auf der gewählten Chain, dazu Buy & Hold. Im Einzellauf gelten Standardparameter je Strategie; bei Train/Test-Split und Walk-Forward wählt die Grid-Search die Parameter, und der Chart zeigt nur Testphase bzw. Out-of-Sample.
- **Auf allen 3 Chains vergleichen**: dieselbe Strategie auf Bitcoin, Solana und Ethereum.

## Risikomanagement
Stop-Loss, Take-Profit, Trailing-Stop und Positionsgröße (voll / fest / Volatilitätsziel) im Formular unter "Risikomanagement". Ohne diese Optionen rechnet die schnelle vektorisierte Engine, mit ihnen eine Kerze-für-Kerze-Simulation (`_simulate_risk`). Tests: `python manage.py test backtester` (u. a. Regressionstest, dass beide Wege ohne Optionen identische Zahlen liefern, Handrechnungen für Stops und Sizing, Look-ahead-Test).

## Annahmen und Grenzen
- Ausführung wählbar: Eröffnungskurs der Folgekerze (Standard) oder Schlusskurs der Signalkerze (optimistischer); Gebühr und Slippage/Spread als Pauschale je Positionswechsel
- Walk-Forward: Beim Foldwechsel werden Parameter- und Positionswechsel nicht extra mit Gebühr belegt
- Trade-Marker im Walk-Forward zeigen nur Trades mit Einstieg im Testfenster; übernommene Positionen aus dem Train-Fenster haben keinen Marker
- Börsen-Anbindung nur mit gestubbter ccxt-Schnittstelle getestet (Pagination, Paar-Fallback, Fehlerfälle), nicht gegen echte Börsen
- Laufende Jobs gehen bei einem Server-Neustart verloren und werden nach 15 Minuten als Fehler markiert
- Slippage/Spread ist eine Pauschale je Seite (Standard 0,05 %), keine Orderbuch-Simulation
- Stops: Stop zuerst, wenn Stop und Ziel in derselben Kerze möglich sind; Kurslücken werden zum Open ausgeführt; kein Wiedereinstieg bis zu einem neuen Signal
- Risiko-Einstellungen sind nicht Teil der Grid-Search
- Long-only, kein Hebel

## Anmeldung
Alle Seiten außer Anmelden/Registrieren/Passwort vergessen erfordern eine Sitzung (Benutzername = E-Mail, Django-Auth, Konten in der Neon-DB). Seiten: `/anmelden/`, `/registrieren/` (E-Mail eingeben → Bestätigungslink 24 h gültig, einmalig → Passwort festlegen → erst dann wird das Konto angelegt), `/passwort-vergessen/` (Reset-Link per E-Mail, einmal nutzbar), `/konto/` (Passwort ändern; E-Mail ändern per Bestätigungslink an die neue Adresse; Konto samt Auswertungen endgültig löschen, mit Passwort + Bestätigung). Bei einer bereits registrierten Adresse sendet `/registrieren/` eine Mail „Konto existiert bereits“ mit Links zu Anmeldung und Passwort-Reset.
**E-Mail-Versand** auf Render über Umgebungsvariablen: `EMAIL_HOST`, `EMAIL_PORT` (587), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, optional `EMAIL_USE_TLS` (1), `DEFAULT_FROM_EMAIL`. Ohne `EMAIL_HOST` wird die Mail nur ins Log geschrieben (Entwicklung).
Jeder Benutzer sieht nur seine eigenen Auswertungen (Verlauf, Detail, Vergleich, Status). Früher angelegte Läufe ohne Besitzer sind nicht mehr sichtbar.
**Render sperrt ausgehendes SMTP** (Ports 25/465/587) bei kostenlosen Diensten. Deshalb E-Mail per HTTPS-API: `EMAIL_API_KEY` (und `EMAIL_API=brevo` oder `resend`) setzen, dazu `DEFAULT_FROM_EMAIL` mit beim Anbieter verifizierter Absenderadresse. Hat `EMAIL_API_KEY` Vorrang vor `EMAIL_HOST`.

## Admin-Bereich (`/admin/`)
Static-Dateien liefert WhiteNoise (werden beim Start per `collectstatic` gesammelt). Anmeldung mit der Admin-E-Mail. Admin auf Render anlegen: Umgebungsvariablen `ADMIN_EMAIL` und `ADMIN_PASSWORD` setzen und neu deployen - das Konto wird beim Start als Superuser angelegt (ein bestehendes Passwort wird nie überschrieben). Lokal: `python manage.py createsuperuser`.

## Robustheit (Monte-Carlo)
Jeder Lauf mit mindestens 5 Trades bekommt einen Robustheits-Test (`backtester/montecarlo.py`, fester Seed, 1.000 Durchläufe): Bootstrap der Trades (Gewinnwahrscheinlichkeit, Rendite- und Drawdown-Spanne, Fächer-Chart) und Vergleich mit Zufalls-Einstiegen gleicher Haltedauer (p-Wert). Unter 30 Trades Hinweis „statistisch nicht belastbar“. Ältere Läufe haben den Block nicht (neu starten).

Gelaufene Auswertungen löschen: rotes × rechts neben dem Titel in der Verlaufsliste (mit Rückfrage, nur eigene Läufe, nur per POST).

## Plausibilitäts-Ampel
Jeder neue Lauf wird automatisch geprüft (`backtester/plausibility.py`, Anzeige oberhalb des Kurs-Charts): **Daten** (Menge, Lücken, Duplikate, ungültige Preise, Sprünge, Volumen), **Engine** (Drawdown, Buy & Hold, Gesamtrendite und Einzeltrades unabhängig nachgerechnet, Trade-Reihenfolge, Look-ahead-Test) und **Aussagekraft** (Trades, Zeitraum, bester Trade, Zeit im Markt, unrealistische Kennzahlen, Train gegen Test, Parameter am Rand). Ältere Läufe haben die Ampel nicht (neu starten). Ein Fehler in der Prüfung lässt den Lauf nie scheitern.

## Marktphasen und Kosten-Sensitivität
`backtester/regimes.py`: Jede Kerze wird nach der Kursentwicklung der letzten ca. 90 Tage als Aufwärts/Seitwärts/Abwärts eingeordnet (nur Vergangenheit); Tabelle und Chart zeigen Strategie gegen Buy & Hold je Phase, im Kurs-Chart sind die Phasen hinterlegt. `backtester/sensitivity.py`: dasselbe Ergebnis mit 0-, 1-, 2-, 3-, 5-fachen Kosten (Parameter fest), inkl. „Kosten bis Gewinn null“. Die 1×-Neuberechnung wird als Selbsttest gegen das Hauptergebnis geprüft (Plausibilitäts-Ampel). Ältere Läufe haben beide Blöcke nicht (neu starten).

## KI-Kommentar (Groq)
Knopf „Auswertung kommentieren“ (nur auf Knopfdruck, einmal je Lauf gespeichert, `backtester/ai.py`). Umgebungsvariablen auf Render: `GROQ_API_KEY` (Pflicht für die KI), optional `GROQ_MODEL` (Standard `openai/gpt-oss-120b`), `GROQ_FALLBACK_MODEL` (`openai/gpt-oss-20b`), `AI_USER_DAILY_LIMIT` (20), `AI_GLOBAL_DAILY_LIMIT` (150, schützt das Gratis-Kontingent). Die KI bekommt nur das Kennzahlen-Datenblatt, rechnet nichts selbst; jede Zahl der Antwort wird gegen das Datenblatt geprüft (sonst eine Korrekturrunde, dann Ausweichmodell, dann regelbasierter Kommentar). Ohne Schlüssel oder bei Ausfall/Limit erscheint der regelbasierte Kommentar, der später per Knopf durch einen KI-Kommentar ersetzt werden kann.

## Anmelde-Bremse, Impressum, Datenschutz

- **Bremse** (`accounts/throttle.py`, Tabelle `accounts_attempt`, Migration 0001): Anmeldung 5 Fehlversuche pro Konto / 20 pro IP in 15 min; Registrierung und Passwort-Reset 3 Anfragen pro E-Mail / 10 pro IP pro Stunde. Gesperrt = HTTP 429 mit Hinweis, das Passwort wird dann nicht mehr geprüft. Ein erfolgreicher Login setzt den Zähler zurück; Einträge älter als 24 h werden gelöscht. Nicht abgedeckt: der Django-Admin-Login (`/admin/`).
- **Impressum / Datenschutz:** `/impressum/` und `/datenschutz/` (ohne Login, Links im Footer, Hinweis auf der Registrierung). Betreiberangaben aus Umgebungsvariablen: `LEGAL_NAME`, `LEGAL_STREET`, `LEGAL_CITY`, `LEGAL_EMAIL`, optional `LEGAL_PHONE`. Fehlen Pflichtangaben, zeigt die Seite eine Warnung. Der Datenschutztext ist eine Vorlage, keine Rechtsberatung; bei Änderungen an Dienstleistern (Mail, KI, Hosting) anpassen.

## Export, Parameter-Stabilität, aktuelles Signal

- **Export** (Link über den Kennzahlen eines Laufs): Trades als CSV (`;`, Dezimalkomma, UTF-8 mit BOM, öffnet in deutschem Excel direkt) und PDF-Zusammenfassung (`backtester/export.py`, benötigt `reportlab`).
- **Parameter-Stabilität** (`backtester/stability.py`, `curves["stab"]`): 5 × 5 Raster um die gewählten Parameter (SMA/Kombiniert: fast × slow; RSI: period × low), ohne neue Optimierung. Beim Train/Test-Split zählt die Testphase, bei Walk-Forward gibt es die Prüfung nicht. Urteil: stabil (≥ 75 % der Nachbarn im Plus, Nachbarmedian ≥ halber Wert), Spitze (< 50 % im Plus oder Median < ¼), sonst gemischt. Ältere Läufe haben diese Auswertung nicht (neu starten).
- **Aktuelles Signal + Signal-Mail** (`backtester/signals.py`), **ein-/ausschaltbar per Umgebungsvariable:**
  - `SIGNALS_ENABLED=1` schaltet die Funktion ein (Standard: aus; dann gibt es weder Box noch Routen noch Prüfung).
  - Box „Aktuelles Signal“ je Lauf: Knopf „Jetzt prüfen“ (long/flat, seit wann, Kurs) und „Mail bei Signal-Wechsel einschalten“ (je Benutzer höchstens `SIGNAL_MAX_PER_USER`, Standard 5).
  - Mails entstehen nur, wenn ein Zeitplan die Prüfung auslöst. Render Free hat keinen Cron: `SIGNAL_CRON_TOKEN=<langes Geheimnis>` setzen und `https://<deine-app>/signale/pruefen/` mit Header `Authorization: Bearer <Token>` (oder `?token=<Token>`) regelmäßig aufrufen, z. B. per cron-job.org (bei 1d-Läufen genügt 1× täglich kurz nach 02:00 Uhr deutscher Zeit, bei 1h/4h stündlich bzw. alle 4 h). Alternativ ein Render Cron Job mit `python manage.py check_signals --base-url https://<deine-app>`.
  - Mails gehen nur bei einem **Wechsel** (long ↔ flat); beim Einschalten wird der aktuelle Zustand als Ausgangspunkt gespeichert. Stops/Positionsgröße sind im Signal nicht berücksichtigt.

## Tests, Browser-Tests und CI
- `python manage.py test` führt alle Tests aus (Rechenkern, Plausibilität, Konto, Export, Signale, Teilen/Vorlagen, Performance).
- **Browser-Tests** (`backtester/test_browser.py`) steuern die echte Oberfläche mit Playwright und Chromium gegen einen Testserver:
  Login, 3-Spalten-Layout, schmale Anzeige ohne Seitwärts-Scrollen, Legende unten, Tooltips, kompletter Lauf mit Diagrammen,
  Export-Downloads, Konto-Seite, Teilen-Link, Favoriten/Tags/Filter, Vorlagen, PDF-Bericht. Einmalig einrichten:
  `pip install -r requirements-dev.txt && playwright install chromium`. Ohne Playwright werden sie übersprungen; mit
  `REQUIRE_BROWSER_TESTS=1` ist ein fehlender Browser ein Fehler (so läuft es in der CI).
- **CI** (`.github/workflows/ci.yml`): GitHub Actions führt bei jedem Push und Pull Request `manage.py check`,
  `makemigrations --check` (Modelländerung ohne Migration fällt auf) und alle Tests inklusive Browser-Tests aus.

## Performance: Zwischenspeicher und paralleles Rechnen
- `backtester/perf.py`: kleine Zwischenspeicher im Arbeitsspeicher (Kursdaten 10 Min., Grid-Search und Kennzahlen je Parameterpunkt
  1 Std.). Der Schlüssel enthält alle Eingaben und einen Fingerabdruck der Kursdaten, ein Treffer liefert also exakt dieselbe Zahl.
  Wiederholte oder ähnliche Läufe (z. B. Vergleichsläufe, erneutes Starten mit denselben Werten) sind dadurch fast sofort fertig
  (Messung: Walk-Forward + Split + Stabilität mit Stops 10,4 s kalt, 0,5 s wiederholt).
- **Paralleles Rechnen** ist optional: `PARALLEL_WORKERS=<n>` (Standard 1 = aus). Die Simulation mit Stops ist reines Python und
  läuft deshalb in einem Prozess-Pool (Grid-Search und Parameter-Stabilität). Gemessen auf 2 Kernen ca. 1,7× schneller bei identischen
  Ergebnissen; schlägt der Pool fehl, rechnet die App seriell weiter. Jeder Worker braucht ca. 100 MB Arbeitsspeicher, deshalb auf
  Render Free (512 MB, wenig CPU) aus lassen und nur bei einem größeren Plan einschalten.

## Teilen, PDF-Bericht, Favoriten, Tags, Vorlagen
- **Teilen:** Im Dashboard „Öffentlichen Link erzeugen“. Der Link `/geteilt/<Token>/` zeigt die Auswertung ohne Anmeldung nur lesend
  (Kennzahlen, Diagramme, Trades, CSV/PDF), ohne Namen und E-Mail des Besitzers, mit `noindex`. „Freigabe beenden“ und „Neuen Link
  erzeugen“ machen den alten Link ungültig; beim Löschen des Laufs oder Kontos verschwindet er ebenfalls.
- **PDF-Bericht:** Läufe in der Liste rechts ankreuzen (höchstens 8) und „Bericht aus Auswahl (PDF)“, oder auf der Vergleichsseite
  „Bericht aller Läufe“. Übersichtstabelle, gemeinsames Diagramm (Start = 100), danach je Lauf die Einzelauswertung. Das Einzel-PDF
  hat ein neues Layout (Kopf- und Fußzeile, Kennzahl-Kacheln, farbige Ampeln und Stabilitäts-Raster).
- **Favoriten und Tags:** Stern und bis zu 5 Tags je Lauf; die Liste rechts filtert nach Tag und nach Favoriten.
- **Vorlagen:** Alle Formularwerte unter einem Namen speichern (höchstens 20 je Nutzer), mit einem Klick wieder laden; der Zeitraum
  wird als Länge in Tagen gespeichert und beim Laden bis heute gerechnet.

## Weitere Strategien und Konfidenzintervalle

- **Strategien** (`strategies.py`): Bollinger-Bänder (Rückkehr zum Mittelwert), MACD, Donchian-Ausbruch, Momentum. Alle liefern wie bisher 1 = long / 0 = flat, rechnen nur mit Daten bis zur aktuellen Kerze und haben ein Raster für Train/Test und Walk-Forward sowie Achsen für die Parameter-Stabilität. Parameter-Belegung im Formular: Bollinger (Periode, Faktor in Zehnteln, 20 = 2,0), MACD (fast, slow, Signal), Donchian (Einstieg, Ausstieg), Momentum (Rückblick, Schwelle in %). Beim Wechsel der Strategie setzt das Formular passende Startwerte.
- **Robustheit** (`montecarlo.py`): Der Zufallsvergleich läuft mit 5 Seeds (p-Wert = Median, Spanne wird angezeigt). Neu sind 95-%-Konfidenzintervalle für Sharpe (Block-Bootstrap der Kerzenrenditen), Ø Trade und Trefferquote. Liegt die Untergrenze nicht über 0, gilt der Vorteil als statistisch nicht gesichert (auch im PDF und im KI-Kommentar).
- Tests: `test_strategies.py`, `test_confidence.py`, Browser-Tests `NewStrategyBrowserTests`.

## Paper-Trading

Ein virtuelles Konto führt das Signal eines Laufs mit Spielgeld aus (nur mit echten Kursen und `SIGNALS_ENABLED=1`; Start im Kasten „Aktuelles Signal“). Bei jeder Prüfung wird zum letzten bekannten Schlusskurs gekauft bzw. verkauft (ganzes Konto, Gebühr + Slippage wie im Lauf), jede Order steht im Journal. Der Abgleich rechnet dieselbe Strategie ab Start als Backtest und zeigt die Abweichung (≤ 1 Prozentpunkt = passend, ≤ 3 = leicht, sonst deutlich) samt Ursachen. Geprüft wird per Knopf und vom bestehenden Zeitplan (`/signale/pruefen/` bzw. `manage.py check_signals` prüft jetzt auch die Paper-Konten). `PAPER_MAX_PER_USER` (Standard 3) begrenzt aktive Konten. Modul `paper.py`, Seiten `views_paper.py`, Tests `test_paper.py`.

## KI-Erweiterungen (ohne Chat)

- **Zwei Läufe erklären** (Verlaufsliste: Häkchen setzen → „Zwei Läufe erklären“): Kennzahlen-Vergleich, unterschiedliche Einstellungen, Vergleichbarkeit.
- **Nächste Variante** (Kasten unter dem Kommentar): Nachbarpunkt mit dem besten Plateau (Mittel der Sharpe-Werte im 3×3-Fenster ohne den Spitzenwert, mindestens 5 gültige Felder) aus der Parameter-Stabilität; „übernehmen“ füllt das Formular vor.
- **Fragen zum Ergebnis**: feste Liste (schwächster/stärkster/bestimmter Zeitraum, größter Rückgang, gegen Buy & Hold, Belastbarkeit, Kosten); Zeiträume stammen aus den Daten des Laufs. Kein Freitext.
- **Strategie in Worten**: ein Satz (max. 280 Zeichen, nur gewöhnliche Zeichen) → Strategie und Parameter. Erst ein regelbasierter Parser, nur bei Misserfolg die KI mit festem JSON-Schema; alle Werte laufen durch Whitelists und `BacktestForm`, die Anzeige „Verstanden als …“ entsteht nur aus geprüften Werten, Modelltext wird nie angezeigt, das Formular wird nur vorbelegt.
- Für alle Antworten gilt: erst regelbasiert aus den berechneten Zahlen, die KI formuliert nur um, jede Zahl wird geprüft, sonst gilt der Regeltext. Ohne `GROQ_API_KEY` oder bei erreichtem Tageslimit gibt es den Regeltext. Antworten werden gespeichert (`AiResult`). Module `ai_extra.py`, `nl_strategy.py`, `views_ai.py`; Tests `test_ai_extra.py`.
- Neu geprüft im Formular: SMA fast < slow, RSI low < high.
