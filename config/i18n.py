"""Zweisprachigkeit DE/EN ohne Umbau der Templates.

Die Quellsprache ist Deutsch. Ist Englisch gewählt (Cookie ``tb_lang``), übersetzt die Middleware die fertige
HTML-Antwort mit einem Katalog (``config/i18n_en/*.py``): Textknoten, Attribute (title, aria-label, data-tip, …),
Zeichenketten in Skripten und JSON-Daten. Katalogeinträge sind entweder ganze Texte oder Muster mit ``{}`` als Platzhalter
(für Zahlen und Namen); Zahlen im deutschen Format (1,5) werden dabei ins englische (1.5) umgesetzt.
Nicht im Katalog stehende Texte bleiben unverändert (Deutsch) - ein Test prüft, dass das bei echten Läufen nicht vorkommt."""
import html as _html
import json
from django.views.decorators.csrf import csrf_exempt
import re
import threading
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponseRedirect
from django.utils import translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

LANGS = ("de", "en")
COOKIE = "tb_lang"
_local = threading.local()


def get_lang() -> str:
    return getattr(_local, "lang", "de")


def set_lang(lang: str) -> None:
    _local.lang = lang if lang in LANGS else "de"


# ------------------------------------------------------------------ Katalog
NUMRX = r"[-−+]?\d[\d.,]*"
_cat = {"exact": None, "rx": None, "tpl": None, "frag": []}


def _load():
    if _cat["exact"] is not None:
        return
    import importlib
    import pkgutil
    from . import i18n_en
    exact, pats, frags = {}, [], []
    for m in pkgutil.iter_modules(i18n_en.__path__):
        mod = importlib.import_module(f"{i18n_en.__name__}.{m.name}")
        for de, en in getattr(mod, "CATALOG", {}).items():
            key = " ".join(de.split())
            if key.startswith("~"):
                frags.append((key[1:], en))
            elif "{}" in key or "{#}" in key or re.search(r"\{\d+\}", key):
                pats.append((key, en))
            else:
                exact[key] = en
    pats.sort(key=lambda p: -len(re.sub(r"\{[\d#]*\}", "", p[0])))          # spezifischere Muster zuerst
    parts, tpl = [], []
    for i, (key, en) in enumerate(pats):
        bits = re.split(r"\{[\d#]*\}", key)
        kinds = re.findall(r"\{([\d#]*)\}", key)
        out = ""
        for k, b in enumerate(bits):
            out += re.escape(b)
            if k < len(bits) - 1:
                out += f"(?P<g{i}_{k}>{NUMRX if kinds[k] == '#' else '.{1,200}?'})"
        parts.append(f"(?P<p{i}>{out})")
        tpl.append((en, len(bits) - 1))
    frags.sort(key=lambda p: -len(re.sub(r"\{[\d#]*\}", "", p[0])))
    fl = []
    for key, en in frags:
        bits = re.split(r"\{[\d#]*\}", key)
        kinds = re.findall(r"\{([\d#]*)\}", key)
        rx = ""
        for k, b in enumerate(bits):
            rx += re.escape(b)
            if k < len(bits) - 1:
                rx += f"({NUMRX if kinds[k] == '#' else '.{1,80}?'})"
        fl.append((re.compile(rx), en))
    _cat["frag"] = fl
    _cat["exact"] = exact
    _cat["rx"] = re.compile("|".join(parts), re.S) if parts else None
    _cat["tpl"] = tpl


def reload_catalog():
    _cat["exact"] = None
    _tr_core.cache_clear()
    _load()


_NUM = re.compile(r"(?<=\d),(?=\d)")
_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


def _num(s: str) -> str:
    """Deutsche Schreibweise -> englische: 1,5 -> 1.5 und 05.10.2026 -> 2026-10-05."""
    return _DATE.sub(r"\3-\2-\1", _NUM.sub(".", s))


@lru_cache(maxsize=50000)
def _tr_core(core: str):
    """Übersetzt einen normalisierten Text; None = unbekannt."""
    _load()
    hit = _cat["exact"].get(core)
    if hit is not None:
        return hit
    rx = _cat["rx"]
    m = rx.fullmatch(core) if rx is not None else None
    if not m:
        return _fragments(core)
    i = int(m.lastgroup[1:])
    en, n = _cat["tpl"][i]
    groups = [m.group(f"g{i}_{k}") for k in range(n)]
    return _fill(en, [_part(g) for g in groups])


def _fill(en: str, groups: list) -> str:
    it = iter(range(len(groups)))

    def rep(mm):
        g = groups[int(mm.group(2)) if mm.group(2) else next(it)]
        return g[:1].lower() + g[1:] if mm.group(1) else g
    return re.sub(r"\{(l?)(\d*)\}", rep, en)


def _fragments(core: str):
    """Teilstücke ersetzen (für zusammengesetzte Texte); None, wenn nichts passt."""
    out = core
    for rx, en in _cat["frag"]:
        out = rx.sub(lambda m: _fill(en, [_part(g) for g in m.groups()]), out)
    return out if out != core else None


def _part(g: str) -> str:
    """Eingefangenes Stück: erst als eigener Text versuchen (Namen, Wörter), sonst nur Zahlenformat anpassen."""
    t = _tr_core(g)
    return t if t is not None else _num(g)


MISSES = None          # Prüfwerkzeug: Menge der Texte, für die es keine Übersetzung gab
_WS = re.compile(r"\s+")
_HASLETTER = re.compile(r"[A-Za-zÄÖÜäöüß]")


def tr(s: str, rec: str = "") -> str:
    """Übersetzt einen Text (Whitespace außen bleibt erhalten); unbekannte Texte bleiben unverändert."""
    if not s or not _HASLETTER.search(s):
        return s
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()):]
    core = _WS.sub(" ", s.strip())
    hit = _tr_core(core)
    if hit is None and MISSES is not None and (rec == "text" or (rec == "code" and " " in core)):
        MISSES.add(core)
    return s if hit is None else lead + hit + trail


def t(s: str) -> str:
    """Für Python-Code (PDF, Mails): übersetzt nur, wenn Englisch aktiv ist."""
    return tr(s) if get_lang() == "en" else s


def tr_all(obj, rec: str = ""):
    """Rekursiv alle Strings in Listen/Dicts (z. B. gespeicherte Analyse-Ergebnisse)."""
    if isinstance(obj, str):
        return tr(obj, rec)
    if isinstance(obj, list):
        return [tr_all(x, rec) for x in obj]
    if isinstance(obj, dict):
        return {k: tr_all(v, rec) for k, v in obj.items()}
    return obj


# ------------------------------------------------------------------ HTML
_TOKEN = re.compile(r"<!--.*?-->|<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>|<textarea\b[^>]*>.*?</textarea>|<[^>]+>|[^<]+", re.S | re.I)
_ATTR = re.compile(r"""(\s(?:title|aria-label|placeholder|alt|data-tip|data-label|content|value)=)(?:"([^"]*)"|'([^']*)')""", re.I)
_SCRIPT = re.compile(r"(<script\b([^>]*)>)(.*?)(</script>)", re.S | re.I)
_JSSTR = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'")


def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text_node(raw: str) -> str:
    plain = _html.unescape(raw)
    out = tr(plain, "text")
    return raw if out == plain else _esc_text(out)


def _tag(raw: str) -> str:
    if raw.startswith("<html"):
        raw = raw.replace('lang="de"', 'lang="en"', 1)
    if "=" not in raw:
        return raw
    is_input = raw[:6].lower() in ("<input", "<butto")
    is_meta = raw[:5].lower() == "<meta"

    def rep(m):
        name = m.group(1).strip()[:-1].lower()
        if name == "value" and not (is_input and re.search(r"""type=["']?(submit|button)""", raw, re.I)):
            return m.group(0)
        if name == "content" and not is_meta:
            return m.group(0)
        q = '"' if m.group(2) is not None else "'"
        val = m.group(2) if m.group(2) is not None else m.group(3)
        plain = _html.unescape(val)
        out = tr(plain, "text")
        if out == plain:
            return m.group(0)
        return m.group(1) + q + out.replace("&", "&amp;").replace(q, "&quot;" if q == '"' else "&#x27;") + q
    return _ATTR.sub(rep, raw)


def _json_block(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body
    new = tr_all(data, "code")
    if new == data:
        return body
    s = json.dumps(new, ensure_ascii=False, separators=(", ", ": "))
    return s.replace("<", "\\u003C").replace(">", "\\u003E").replace("&", "\\u0026")


_UNESC = {"\\n": "\n", '\\"': '"', "\\'": "'", "\\\\": "\\", "\\/": "/"}


def _js_block(body: str) -> str:
    def rep(m):
        q = '"' if m.group(1) is not None else "'"
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        if not _HASLETTER.search(raw):
            return m.group(0)
        plain = re.sub(r"\\[n\"'\\/]", lambda x: _UNESC[x.group(0)], raw)
        out = tr(plain, "code")
        if out == plain:
            return m.group(0)
        enc = out.replace("\\", "\\\\").replace("\n", "\\n").replace(q, "\\" + q)
        return q + enc + q
    return _JSSTR.sub(rep, body)


def translate_html(html: str) -> str:
    off = [False]                       # <!--i18n:off--> ... <!--i18n:on-->: Bereich ist schon englisch

    def tok(m):
        s = m.group(0)
        if s.startswith("<!--"):
            if s == "<!--i18n:off-->":
                off[0] = True
            elif s == "<!--i18n:on-->":
                off[0] = False
            return s
        if off[0]:
            return s
        low = s[:9].lower()
        if low.startswith("<script"):
            sm = _SCRIPT.fullmatch(s)
            if not sm:
                return s
            open_tag, attrs, body, close = sm.groups()
            if "src=" in attrs:
                return s
            if "application/json" in attrs:
                return open_tag + _json_block(body) + close
            return open_tag + _js_block(body) + close
        if low.startswith(("<style", "<textarea")):
            return _tag_prefix(s)
        if s.startswith("<"):
            return _tag(s)
        return _text_node(s)
    return _TOKEN.sub(tok, html)


def _tag_prefix(s: str) -> str:
    i = s.index(">") + 1
    return _tag(s[:i]) + s[i:]


# ------------------------------------------------------------------ Middleware und Umschalter
class LanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = request.COOKIES.get(COOKIE)
        lang = lang if lang in LANGS else "de"
        request.lang = lang
        set_lang(lang)
        translation.activate("en" if lang == "en" else "de")
        try:
            response = self.get_response(request)
            if lang == "en" and not response.streaming:
                ctype = response.get("Content-Type", "")
                if ctype.startswith("text/html") and not 300 <= response.status_code < 400:
                    html = response.content.decode(response.charset or "utf-8")
                    response.content = translate_html(html).encode(response.charset or "utf-8")
                    if "Content-Length" in response:
                        response["Content-Length"] = str(len(response.content))
        finally:
            translation.deactivate()
            set_lang("de")
        response["Content-Language"] = lang
        response.headers.setdefault("Vary", "Cookie")
        return response


@login_not_required
@csrf_exempt            # setzt nur das Sprach-Cookie; so bleiben öffentliche Seiten frei von csrftoken
@require_POST
def set_language(request):
    lang = request.POST.get("lang")
    nxt = request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = "/"
    resp = HttpResponseRedirect(nxt)
    if lang in LANGS:
        resp.set_cookie(COOKIE, lang, max_age=365 * 24 * 3600, samesite="Lax", secure=request.is_secure(), httponly=False)
    return resp


def mail_text(s: str) -> str:
    """Mailtext zeilenweise übersetzen (nur wenn Englisch aktiv ist)."""
    if get_lang() != "en":
        return s
    return "\n".join(tr(line) for line in s.split("\n"))
