"""Hilfen für den Übersetzungstest: findet Texte, die in der englischen Ausgabe noch deutsch sind."""
import ast
import html as _html
import json
import re
from pathlib import Path

from . import i18n

GERMAN_WORDS = set("""und nicht mit für ist wird werden oder der die das dem den des ein eine einen einem einer bei auf aus von zum zur
im sich auch nur wenn noch kein keine keinen sind wurde wurden dieser diese diesen dieses nach über unter zwischen jede jeden jedes
bis wie dass ob kann können sollte gibt wenig mehr weniger sehr zu aber doch dann weil sowie hier dort alle beim vom ins ihr ihre
konto lauf läufe läufen kurs kurse strategie auswertung einstellungen hinweis wert werte zeitraum kerzen gewählt""".split())
_UML = re.compile(r"[äöüÄÖÜß]")
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")
IGNORE_WORDS = {"die"}                     # zu oft englisch ("die" als Verb) - selten in unseren Texten


def looks_german(text: str) -> bool:
    if _UML.search(text):
        return True
    words = [w.lower() for w in _WORD.findall(text)]
    return len(words) >= 2 and any(w in GERMAN_WORDS and w not in IGNORE_WORDS for w in words)


def _iter_texts(html: str):
    """Liefert (art, text) für alles, was nach der Übersetzung sichtbar wäre."""
    for m in i18n._TOKEN.finditer(html):
        s = m.group(0)
        if s.startswith("<!--"):
            continue
        low = s[:9].lower()
        if low.startswith("<script"):
            sm = i18n._SCRIPT.fullmatch(s)
            if not sm or "src=" in sm.group(2):
                continue
            body = sm.group(3)
            if "application/json" in sm.group(2):
                try:
                    data = json.loads(body)
                except ValueError:
                    continue
                stack = [data]
                while stack:
                    x = stack.pop()
                    if isinstance(x, str):
                        yield "json", x
                    elif isinstance(x, list):
                        if len(x) < 400:
                            stack.extend(x)
                    elif isinstance(x, dict):
                        stack.extend(x.values())
            else:
                for jm in i18n._JSSTR.finditer(body):
                    yield "js", jm.group(1) if jm.group(1) is not None else jm.group(2)
            continue
        if s.startswith("<"):
            for am in i18n._ATTR.finditer(s):
                name = am.group(1).strip()[:-1].lower()
                if name in ("value", "content"):
                    continue
                yield "attr", am.group(2) if am.group(2) is not None else am.group(3)
            continue
        yield "text", s


def untranslated(html: str) -> list:
    """html = bereits übersetzte Seite; Rückgabe: noch deutsche Texte (dedupliziert, in Reihenfolge)."""
    seen, out = set(), []
    for kind, raw in _iter_texts(html):
        text = " ".join(_html.unescape(raw).split())
        if len(text) < 2 or text in seen:
            continue
        if looks_german(text):
            seen.add(text)
            out.append(text)
    return out


# ------------------------------------------------------------ Texte aus dem Quellcode (Analyse-, Mail-, PDF-Texte …)
def source_strings(path: Path) -> list:
    """Alle Zeichenketten eines Moduls als Katalogschlüssel: f-Strings werden zu Mustern mit {}."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            skip.update(id(k) for k in node.keys if k is not None)
        elif isinstance(node, ast.Subscript):
            skip.add(id(node.slice))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):   # Docstrings
            skip.add(id(node.value))
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant):
                    skip.add(id(v))
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                parts.append(v.value if isinstance(v, ast.Constant) else "{}")
            out.append("".join(parts))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    res, seen = [], set()
    for s in out:
        k = " ".join(s.split())
        if k and k not in seen and looks_german(k.replace("{}", " ")):
            seen.add(k)
            res.append(k)
    return res


# ------------------------------------------------------------ bewusst nicht übersetzte Texte (Namen, Kürzel, Zahlen, Nutzereingaben)
KEEP_EXACT = set("""Bitcoin Ethereum Solana BTC ETH SOL USDT USD USDC Binance Kraken Coinbase Bybit OKX Bitstamp Plotly Groq Neon Brevo Resend
SMA RSI MACD EMA CAGR Sharpe Sortino Calmar OOS PDF CSV JSON GDPR DSGVO UTC ccxt TA-Lib pandas Python Excel
Open High Low Close Volume Seeds Admin Django Deutsch English Cookie Engine Heatmap Journal Live Momentum Name Phase Plateau Position Profit Factor
Signal Slippage Tags Test Train Trades Parameter Journal Calmar Sortino Ratio i on off renew flat csrftoken sessionid tb_lang tb_theme""".split())
KEEP_TEXT = {
    "Max Drawdown", "Max Drawdown %", "Max Drawdown (MDD)", "CAGR %", "CAGR (Compound Annual Growth Rate)", "Calmar Ratio", "Sortino Ratio", "Sharpe Ratio",
    "Profit Factor", "Profitable Folds", "Profitable Folds:", "Cookie (HttpOnly)", "Engine:", "Export (CSV, PDF)", "Export:", "Hosting:",
    "In-Sample / Out-of-Sample (OOS)", "Look-ahead-Bias", "SMA (Simple Moving Average)", "Signal, Long, Flat, Long-only", "Parameter 1", "Parameter 2",
    "Parameter 3", "Test %", "Test-Sharpe", "Train-Sharpe", "Trades (CSV)", "Trading Backtester", "B&H %", "Max DD %", "Bitcoin (BTC), Ethereum (ETH), Solana (SOL)",
    "Binance-Archiv (data.binance.vision)", "RSI (Relative Strength Index), period, low, high", "width=device-width, initial-scale=1",
    "Enter a valid email address.", "New password", "New password confirmation", "Old password", "Password", "Not Found", "This field is required.",
    "The requested resource was not found on this server.", "Please enter a correct username and password. Note that both fields may be case-sensitive.",
    "Journal (0)", "test,eins", "RSI BTC/USDT 1d",
}
KEEP_RX = [
    r"^\d+[a-z]$",                                                          # Zeitfenster 1d, 4h
    r"^Trades: A \d+, B \d+\.$",
    r"^-?[\d.,]+ % \(B&H -?[\d.,]+ %\)$",
    r"^(period|entry|exit|logic|fast|slow|k|low|high|signal)=",              # Parameter-Anzeige
    r"^\{.*\}$",
    r"^profitable Folds: \d+/\d+$",
    r"^[\d\s.,:;/()%+\-–−·×≈|\[\]{}'\"=<>_]*$",                        # nur Zahlen/Zeichen
    r"^[A-Z0-9/ ._\-]{1,24}$",                                              # Kürzel (BTC/USDT, 1d, SMA …)
    r"^\S+@\S+$",
    r"^\d{2}\.\d{2}\. \d{2}:\d{2} · [\w ./-]+ · -?[\d.,]+ % \(B&H -?[\d.,]+ %\)$",   # Verlaufszeile
    r"^[\w ./-]+ · (SMA|RSI|MACD|Momentum|EMA|Bollinger|Donchian|[\w ()+]+) · \d+[a-z]$",  # Titel Coin · Strategie · Zeitfenster                                                          # E-Mail-Adressen
]


def is_kept(text: str) -> bool:
    import re as _re
    return text in KEEP_EXACT or text in KEEP_TEXT or any(_re.match(p, text) for p in KEEP_RX)
