# Trading Backtester (Django)

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python manage.py makemigrations backtester && python manage.py migrate
    python manage.py runserver

Im Formular "Synthetisch (offline)" wählen, um ohne Börsen-API zu testen.

## TA-Lib (optional)
Ubuntu/Debian: `sudo apt install libta-lib-dev && pip install TA-Lib`
macOS: `brew install ta-lib && pip install TA-Lib`
Windows: passendes Wheel installieren. Ohne TA-Lib nutzt `indicators.py` einen pandas-Fallback.

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
