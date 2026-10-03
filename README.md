# Trading Backtester (Django)

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python manage.py makemigrations backtester && python manage.py migrate
    python manage.py runserver

Im Formular "Synthetisch (offline)" wählen, um ohne Börsen-API zu testen.

## PostgreSQL (Neon) und Render
Die App liest die Datenbank aus der Umgebungsvariable `DATABASE_URL`. Ohne sie läuft lokal SQLite wie bisher.
- **Treiber:** `psycopg[binary]` (psycopg 3) und `dj-database-url` stehen in `requirements.txt`. psycopg 3 ist der Treiber, den Django bevorzugt. Ein zusätzliches `psycopg2` wäre überflüssig und würde ignoriert.
- **Render:** Umgebungsvariablen `DATABASE_URL` (Neon-Verbindungsstring), `SECRET_KEY` (zufälliger langer Wert) und `DEBUG=0`. Host und CSRF-Herkunft setzt die App aus `RENDER_EXTERNAL_HOSTNAME` selbst.
- **Build-Befehl:** `pip install -r requirements.txt && python manage.py migrate`
- **Start-Befehl:** `gunicorn config.wsgi`
- **Neon:** SSL wird erzwungen. Der Pooler-Endpunkt (Host mit `-pooler`) funktioniert; serverseitige Cursor sind dafür abgeschaltet. Treten bei `migrate` über den Pooler Probleme auf, `migrate` einmalig mit der direkten Verbindung (ohne `-pooler`) ausführen.
- **Aufwachen:** Neon pausiert inaktive Datenbanken. Die erste Anfrage danach kann einige Sekunden dauern (Timeout 15 s), tote Verbindungen werden automatisch ersetzt.
- **Admin:** `python manage.py createsuperuser` einmalig ausführen. Statische Dateien des Admins werden ohne zusätzliche Konfiguration (z. B. WhiteNoise) nicht ausgeliefert, die App selbst braucht keine.

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

## Börsen, Hintergrund-Berechnung, Chain-Vergleich
- **Börse** (Dropdown): Binance, Kraken, Coinbase, Bybit, OKX, Bitstamp über ccxt. Fehlt ein USDT-Paar, wird USD bzw. USDC genutzt. Begrenzt eine Börse die Historie, erscheint ein Datenhinweis.
- **Hintergrund**: Backtests laufen im Thread-Pool (`jobs.py`), die Seite aktualisiert sich selbst. Für Produktivbetrieb kann `jobs.submit()` durch Celery oder django-q ersetzt werden, `compute()` bleibt gleich.
- **Chain-Vergleich**: Button "Auf allen 3 Chains vergleichen" startet dieselben Einstellungen auf BTC, SOL und ETH.

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
