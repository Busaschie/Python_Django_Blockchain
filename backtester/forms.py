from django import forms

from .chains import CHAIN_CHOICES, EXCHANGE_CHOICES
from .strategies import LABELS, STRATEGIES, params_from_inputs


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
    timeframe = forms.ChoiceField(label="Zeitfenster", choices=[("1h", "1h"), ("4h", "4h"), ("1d", "1d")],
                                  initial="1d")
    days = forms.IntegerField(min_value=30, max_value=1500, initial=730, label="Tage Historie")
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

    def clean_size_mode(self):
        return self.cleaned_data.get("size_mode") or "full"

    def clean_slippage(self):
        v = self.cleaned_data.get("slippage")
        return 0.0 if v is None else v

    def clean(self):
        d = super().clean()
        if d.get("mode") == "single" and (d.get("param_a") is None or d.get("param_b") is None):
            raise forms.ValidationError("Beim Einzellauf werden Parameter 1 und 2 benötigt.")
        mode, val = d.get("size_mode"), d.get("size_value")
        if mode == "fixed" and not (val and 0 < val <= 100):
            raise forms.ValidationError("Feste Positionsgröße: Wert zwischen 0 und 100 % angeben.")
        if mode == "vol" and not (val and 0 < val <= 500):
            raise forms.ValidationError("Volatilitätsziel: Wert zwischen 0 und 500 % p. a. angeben.")
        return d

    def strategy_params(self) -> dict:
        d = self.cleaned_data
        return params_from_inputs(d["strategy"], d["param_a"], d["param_b"], d["param_c"])
