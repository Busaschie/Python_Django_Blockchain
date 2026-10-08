from datetime import date, timedelta

from django import forms

from .chains import CHAIN_CHOICES, EXCHANGE_CHOICES
from .strategies import LABELS, STRATEGIES, params_from_inputs


MIN_DAYS, MAX_DAYS, EARLIEST = 30, 1500, date(2010, 1, 1)

# --- Erklaerungen (i-Symbol neben jedem Feld): was es ist und wofuer es gut ist ---
SMA_FAST = ("SMA fast: Länge des kurzen gleitenden Durchschnitts in Kerzen. Er reagiert schnell auf Kursänderungen. "
            "Kreuzt er den langen Durchschnitt nach oben, entsteht ein Kaufsignal. Typisch: 10 bis 20.")
SMA_SLOW = ("SMA slow: Länge des langen gleitenden Durchschnitts in Kerzen. Er zeigt den Haupttrend und muss größer "
            "als fast sein. Typisch: 50 bis 200.")
PARAM_TIPS = {  # je Strategie die Erklärung für Parameter 1, 2, 3 (das Skript tauscht sie beim Wechsel der Strategie)
    "sma_cross": [SMA_FAST, SMA_SLOW, ""],
    "rsi": ["RSI period: Anzahl der Kerzen, aus denen der RSI berechnet wird (Standard 14). Kürzer reagiert nervöser "
            "und erzeugt mehr Signale, länger glättet stärker.",
            "RSI low: Kaufschwelle. Fällt der RSI darunter (überverkauft, z. B. 30), wird gekauft, in der Erwartung "
            "einer Gegenbewegung nach oben.",
            "RSI high: Verkaufsschwelle. Steigt der RSI darüber (überkauft, z. B. 70), wird verkauft."],
    "combo": ["SMA fast (Trend-Teil der Kombi-Strategie): " + SMA_FAST.split(": ", 1)[1],
              "SMA slow (Trend-Teil der Kombi-Strategie): " + SMA_SLOW.split(": ", 1)[1], ""],
}
SIZE_TIPS = {
    "fixed": "Anteil des Kapitals, der je Trade eingesetzt wird (0 bis 100 %). 50 bedeutet eine halbe Position. "
             "Der Rest bleibt in bar. Weniger Risiko, dafür weniger Gewinn und weniger Verlust.",
    "vol": "Ziel-Schwankung (Volatilität) in % pro Jahr. Die Position ist Ziel geteilt durch die aktuelle Schwankung, "
           "höchstens 100 % (kein Hebel). Unruhige Märkte führen zu kleineren Positionen. Kryptowährungen schwanken "
           "oft 50 bis 80 % p. a.; ein niedriger Wert ist vorsichtiger.",
}
HELP = {
    "strategy": "Das Handelsverfahren, das Kauf- und Verkaufssignale erzeugt. SMA-Crossover folgt dem Trend, RSI kauft "
                "nach starken Kursrückgängen auf eine Gegenbewegung, Kombiniert verbindet beides. Mit „Strategien "
                "vergleichen“ siehst du alle drei nebeneinander.",
    "mode": "Wie ausgewertet wird. Train/Test-Split: Parameter werden auf den ersten Daten gesucht und auf ungesehenen "
            "Daten geprüft. Walk-Forward: wiederholt das rollierend und ist am aussagekräftigsten. Einzellauf: eigene "
            "Parameter über den ganzen Zeitraum, schnell, aber anfällig für Überanpassung.",
    "train_frac": "Anteil des Zeitraums, auf dem die besten Parameter gesucht werden (Training). Der Rest ist die "
                  "Testphase mit ungesehenen Daten, nur sie zeigt, ob die Strategie wirklich taugt. Üblich: 70 %.",
    "wf_folds": "Anzahl der Wiederholungen beim Walk-Forward. Jeder Fold trainiert und testet auf einem neuen, weiter "
                "verschobenen Zeitfenster. Mehr Folds bedeuten mehr Stichproben, aber kürzere Testfenster. Üblich: 4 bis 6.",
    "wf_train_mult": "Wie viel länger das Trainingsfenster als das Testfenster ist. 3 heißt: Training dreimal so lang "
                     "wie jeder Test. Längeres Training macht die Parameter stabiler, lässt aber weniger Folds zu.",
    "param_a": PARAM_TIPS["sma_cross"][0],
    "param_b": PARAM_TIPS["sma_cross"][1],
    "param_c": PARAM_TIPS["rsi"][2],
    "combo_logic": "Wie SMA und RSI zusammenspielen. Trendfilter + RSI-Einstieg: Kauf nur im Aufwärtstrend bei einem "
                   "Rücksetzer. ODER: long, sobald SMA oder RSI long sind, das ergibt mehr Zeit im Markt.",
    "rsi_period": "Anzahl der Kerzen für den RSI-Teil der Kombi-Strategie (Standard 14). Wird nicht optimiert, sondern "
                  "für alle Parameterkombinationen gleich verwendet.",
    "rsi_entry": "RSI-Schwelle für den Einstieg (Kombi): Gekauft wird, wenn der RSI darunter fällt, beim Trendfilter nur "
                 "im Aufwärtstrend. Höher steigt früher und öfter ein. Typisch: 30 bis 45.",
    "rsi_exit": "RSI-Schwelle für den Ausstieg (Kombi): Verkauf, sobald der RSI darüber steigt. Muss über dem "
                "Einstieg liegen. Typisch: 65 bis 75.",
    "timeframe": "Länge einer Kerze: 1h = eine Stunde, 4h = vier Stunden, 1d = ein Tag. Kürzere Zeitfenster liefern mehr "
                 "Signale und Trades, aber mehr Rauschen, mehr Kosten und mehr Daten (längere Ladezeit). Nicht jede "
                 "Börse bietet jedes Zeitfenster an.",
    "start_date": f"Beginn des ausgewerteten Zeitraums (UTC, einschließlich). Zwischen Von und Bis müssen "
                  f"{MIN_DAYS} bis {MAX_DAYS} Tage liegen, Daten gibt es ab 2010. Längere Zeiträume enthalten mehr "
                  f"Marktphasen und sind aussagekräftiger.",
    "end_date": "Ende des Zeitraums (UTC, einschließlich), höchstens heute. Es zählen nur abgeschlossene Kerzen, die "
                "laufende Kerze von heute ist noch nicht dabei.",
    "fee": "Handelsgebühr der Börse je Kauf und je Verkauf als Anteil des Handelsbetrags: 0.001 sind 0,1 %, ein "
           "üblicher Wert im Spot-Handel. Ohne Gebühren wirken Strategien mit vielen Trades zu gut.",
    "slippage": "Pauschaler Aufschlag je Kauf und Verkauf für Spread und ungünstigere Ausführung (der Preis weicht vom "
                "erwarteten ab): 0.0005 sind 0,05 %. Macht Ergebnisse realistischer, vor allem bei vielen Trades.",
    "stop_loss": "Verkauft automatisch, wenn der Kurs um diesen Prozentsatz unter den Einstiegskurs fällt. Begrenzt den "
                 "Verlust je Trade, kann aber bei kurzen Rücksetzern zu früh aussteigen. Leer = aus.",
    "take_profit": "Verkauft automatisch, sobald der Kurs diesen Prozentsatz über dem Einstiegskurs liegt. Sichert "
                   "Gewinne, schneidet aber starke Trends ab. Leer = aus.",
    "trailing_stop": "Stop, der dem Höchstkurs seit dem Einstieg nach oben folgt und nie sinkt. Verkauft, wenn der Kurs "
                     "um diesen Prozentsatz unter den Höchststand fällt. Sichert Gewinne in Trends. Leer = aus.",
    "size_mode": "Wie viel Kapital je Trade eingesetzt wird. Voll investiert: 100 %. Feste Größe: ein fester Anteil. "
                 "Volatilitätsziel: je unruhiger der Markt, desto kleiner die Position (höchstens 100 %, kein Hebel). "
                 "Weniger Risiko, dafür meist weniger Rendite.",
    "size_value": SIZE_TIPS["fixed"],
    "execution": "Zu welchem Preis ein Signal umgesetzt wird. Eröffnungskurs der Folgekerze: realistisch, weil das "
                 "Signal erst nach Kerzenende bekannt ist. Schlusskurs der Signalkerze: optimistischer, die "
                 "Ergebnisse fallen meist etwas besser aus.",
    "source": "Woher die Kursdaten kommen. Börse (ccxt): echte historische Kurse. Synthetisch: künstliche Zufallskurse "
              "zum Testen ohne Internet, sie enthalten kein echtes Marktmuster.",
    "exchange": "Börse, von der die Kurse geladen werden. Preise und Länge der Historie unterscheiden sich leicht, manche "
                "Börsen sind regional gesperrt. Geladene Kerzen werden zwischengespeichert.",
}
UI_TIPS = {
    "chain": "Die Kryptowährung, deren Kurse ausgewertet werden: Bitcoin (BTC), Solana (SOL) oder Ethereum (ETH), jeweils "
             "gegen USDT bzw. USD. Der Chain-Vergleich testet alle drei auf einmal.",
    "run": "Startet einen Backtest mit diesen Einstellungen. Er läuft im Hintergrund, die Seite aktualisiert sich selbst.",
    "compare_chains": "Startet dieselben Einstellungen auf Bitcoin, Solana und Ethereum und zeigt die Ergebnisse "
                      "nebeneinander.",
    "montecarlo": "Robustheits-Test: Die Trades werden 1.000-mal zufällig neu gemischt bzw. gezogen. Statt einer einzelnen "
                  "Zahl siehst du, wie stark Rendite und Drawdown streuen, und ob die Strategie besser ist als "
                  "zufällige Einstiege mit gleicher Haltedauer.",
    "plausibility": "Automatische Prüfungen je Lauf: sind die Kursdaten vollständig, rechnet die Simulation in sich stimmig "
                    "(Gegenrechnung, kein Blick in die Zukunft) und reicht die Datenbasis für eine Aussage? Grün = in "
                    "Ordnung, gelb = Hinweise beachten, rot = Ergebnis nicht verwertbar.",
    "regimes": "Zeigt, wie die Strategie im Aufwärts-, Seitwärts- und Abwärtstrend abschneidet, verglichen mit dem Markt. "
               "Die Phase ergibt sich aus der Kursentwicklung der letzten ca. 90 Tage. So erkennst du, ob die Strategie "
               "nur in einer Marktlage funktioniert.",
    "costs": "Dasselbe Ergebnis mit 0-, 1-, 2-, 3- und 5-fachen Kosten (Gebühr + Slippage), Parameter unverändert. "
             "Zeigt, wie viel Spielraum die Strategie bei höheren Kosten oder schlechteren Ausführungskursen hat.",
    "stability": "Rechnet die Strategie mit leicht verschobenen Parametern (Raster um deinen Wert, ohne neue Optimierung). "
                 "Liegt dein Wert auf einem Plateau guter Nachbarn, ist das Ergebnis robust; ist er eine einzelne Spitze, "
                 "spricht das für Überanpassung. Beim Train/Test-Split zählt die Testphase.",
    "signal": "Zeigt, was die Strategie mit den Parametern dieses Laufs auf den aktuellen Kursen anzeigt (long = investiert, "
              "flat = nicht investiert). Optional eine Mail bei jedem Wechsel. Stops und Positionsgröße sind darin nicht "
              "berücksichtigt. Keine Anlageberatung.",
    "ai": "Erklärt das Ergebnis in Klartext und schlägt nächste Tests vor. Entsteht nur auf Knopfdruck, wird je Lauf einmal "
          "gespeichert und zählt zum Tageslimit. Die KI bekommt nur Kennzahlen, rechnet nichts selbst und jede genannte "
          "Zahl wird gegen die berechneten Werte geprüft. Keine Anlageberatung.",
    "share": "Erzeugt einen Link, mit dem jeder diese Auswertung ohne Anmeldung nur lesen kann (Kennzahlen, Diagramme, "
             "Trades, PDF). Name und E-Mail sind nicht sichtbar. Die Freigabe lässt sich jederzeit beenden, dann "
             "funktioniert der Link nicht mehr.",
    "templates": "Speichert alle Werte des Formulars unter einem Namen, damit du eine Lieblingskonfiguration mit einem Klick "
                 "wieder laden kannst. Der Zeitraum wird als Länge in Tagen bis heute gespeichert.",
    "tags": "Ordne Läufe mit kurzen Stichworten (mit Komma getrennt, höchstens 5) und markiere wichtige Läufe mit dem Stern. "
            "Die Liste rechts lässt sich danach nach Tag und nach Favoriten filtern.",
    "report": "Kreuze links an den Läufen Häkchen an (höchstens 8) und erzeuge daraus ein PDF mit Übersichtstabelle, "
              "gemeinsamem Diagramm und der Einzelauswertung je Lauf.",
    "compare_strategies": "Startet dieselben Einstellungen mit SMA-Crossover, RSI und Kombiniert auf der gewählten "
                          "Chain und zeigt die Ergebnisse nebeneinander. Im Einzellauf gelten Standardparameter.",
}


def _date_widget():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class BacktestForm(forms.Form):
    chain = forms.ChoiceField(choices=CHAIN_CHOICES, initial="btc", widget=forms.RadioSelect)
    strategy = forms.ChoiceField(label="Strategie", choices=[(k, LABELS[k]) for k in STRATEGIES])
    mode = forms.ChoiceField(label="Modus", initial="split", choices=[
        ("split", "Optimieren mit Train/Test-Split"),
        ("walkforward", "Walk-Forward-Analyse"),
        ("single", "Einzelner Backtest (manuelle Parameter)"),
    ])
    train_frac = forms.IntegerField(min_value=50, max_value=90, initial=70, required=False,
                                    label="Train-Anteil (%)")
    wf_folds = forms.IntegerField(min_value=3, max_value=12, initial=5, required=False,
                                  label="Anzahl Folds")
    wf_train_mult = forms.IntegerField(min_value=1, max_value=8, initial=3, required=False,
                                       label="Train-Fenster (× Testfenster)")
    param_a = forms.IntegerField(label="Parameter 1", initial=20, required=False)
    param_b = forms.IntegerField(label="Parameter 2", initial=50, required=False)
    param_c = forms.IntegerField(label="Parameter 3", initial=70, required=False)
    # Nur fuer "Kombiniert (SMA + RSI)": Verknuepfung und RSI-Teil (der SMA-Teil nutzt Parameter 1/2 bzw. die Grid-Search)
    combo_logic = forms.ChoiceField(label="Verknüpfung (Kombiniert)", initial="trend", required=False, choices=[
        ("trend", "Trendfilter + RSI-Einstieg"), ("or", "ODER (SMA oder RSI)")])
    rsi_period = forms.IntegerField(label="RSI period (Kombiniert)", initial=14, min_value=2, max_value=100, required=False)
    rsi_entry = forms.IntegerField(label="RSI Einstieg unter (Kombiniert)", initial=40, min_value=1, max_value=99, required=False)
    rsi_exit = forms.IntegerField(label="RSI Ausstieg über (Kombiniert)", initial=70, min_value=2, max_value=99, required=False)
    timeframe = forms.ChoiceField(label="Zeitfenster", choices=[("1h", "1h"), ("4h", "4h"), ("1d", "1d")],
                                  initial="1d")
    start_date = forms.DateField(label="Von", widget=_date_widget(),
                                 initial=lambda: date.today() - timedelta(days=730))
    end_date = forms.DateField(label="Bis (einschließlich)", widget=_date_widget(), initial=date.today)
    fee = forms.FloatField(min_value=0, initial=0.001, label="Gebühr (0.001 = 0,1 %)")
    slippage = forms.FloatField(min_value=0, initial=0.0005, required=False,
                                label="Slippage/Spread je Seite (0.0005 = 0,05 %)")
    stop_loss = forms.FloatField(min_value=0, max_value=99, required=False,
                                 label="Stop-Loss (% unter Einstieg, leer = aus)")
    take_profit = forms.FloatField(min_value=0, max_value=1000, required=False,
                                   label="Take-Profit (% über Einstieg, leer = aus)")
    trailing_stop = forms.FloatField(min_value=0, max_value=99, required=False,
                                     label="Trailing-Stop (% unter Höchstkurs, leer = aus)")
    size_mode = forms.ChoiceField(label="Positionsgröße", initial="full", required=False, choices=[
        ("full", "Voll investiert (100 %)"),
        ("fixed", "Feste Größe (% des Kapitals)"),
        ("vol", "Volatilitätsziel (% p. a.)"),
    ])
    size_value = forms.FloatField(min_value=0, initial=50, required=False, label="Wert")
    execution = forms.ChoiceField(label="Ausführung", initial="open", choices=[
        ("open", "Eröffnungskurs der Folgekerze"),
        ("close", "Schlusskurs der Signalkerze"),
    ])
    source = forms.ChoiceField(label="Datenquelle", choices=[
        ("ccxt", "Börse (ccxt)"), ("synthetic", "Synthetisch (offline)")])
    exchange = forms.ChoiceField(label="Börse", choices=EXCHANGE_CHOICES, initial="binance")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("start_date", "end_date"):  # Datumsauswahl im Browser auf heute begrenzen
            self.fields[name].widget.attrs["max"] = date.today().isoformat()
        for name, text in HELP.items():  # Erklärung für das i-Symbol neben dem Feld
            self.fields[name].help_text = text

    def clean_size_mode(self):
        return self.cleaned_data.get("size_mode") or "full"

    def clean_slippage(self):
        v = self.cleaned_data.get("slippage")
        return 0.0 if v is None else v

    def clean(self):
        d = super().clean()
        if d.get("mode") == "single" and (d.get("param_a") is None or d.get("param_b") is None):
            raise forms.ValidationError("Beim Einzellauf werden Parameter 1 und 2 benötigt.")
        entry, exit_ = d.get("rsi_entry"), d.get("rsi_exit")
        if entry and exit_ and entry >= exit_:
            self.add_error("rsi_exit", "Der RSI-Ausstieg muss über dem RSI-Einstieg liegen.")
        start, end = d.get("start_date"), d.get("end_date")
        if start and start < EARLIEST:
            self.add_error("start_date", "Kursdaten gibt es erst ab 2010.")
        elif start and end:
            n = (end - start).days + 1
            if end < start:
                self.add_error("end_date", "Das Enddatum muss nach dem Startdatum liegen.")
            elif n < MIN_DAYS:
                self.add_error("end_date", f"Der Zeitraum muss mindestens {MIN_DAYS} Tage umfassen (aktuell {n}).")
            elif n > MAX_DAYS:
                self.add_error("end_date", f"Der Zeitraum darf höchstens {MAX_DAYS} Tage umfassen (aktuell {n}).")
        if end and end > date.today():
            self.add_error("end_date", "Das Enddatum liegt in der Zukunft.")
        mode, val = d.get("size_mode"), d.get("size_value")
        if mode == "fixed" and not (val and 0 < val <= 100):
            raise forms.ValidationError("Feste Positionsgröße: Wert zwischen 0 und 100 % angeben.")
        if mode == "vol" and not (val and 0 < val <= 500):
            raise forms.ValidationError("Volatilitätsziel: Wert zwischen 0 und 500 % p. a. angeben.")
        return d

    def strategy_params(self) -> dict:
        d = self.cleaned_data
        return params_from_inputs(d["strategy"], d["param_a"], d["param_b"], d["param_c"], d)
