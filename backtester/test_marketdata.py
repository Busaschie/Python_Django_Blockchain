"""Tests für Cache, Sperren-Schutz und Binance-Archiv (alles mit nachgebauter Börse, ohne Netz)."""
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import ccxt
from django.test import TestCase

from . import binance_archive, marketdata
from .models import Candle, CandleCoverage, ExchangeBlock

DAY = 86_400_000
BAN_MESSAGE = ('binance GET https://api.binance.com/api/v3/klines 418 I\'m a teapot {"code":-1003,"msg":"Way too many '
               'requests; IP(74.220.51.139) banned until 1790959019894. Please use the websocket for live updates '
               'to avoid bans."}')


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def make_fake(history_days=20_000, max_candles=300, markets=None, tfs=None, fail_after=None, fail_with=None):
    market_data = markets if markets is not None else {"BTC/USDT": {"active": True}}
    frames = tfs or {"1d": "1d", "1h": "1h"}

    class Fake:
        calls, market_calls, since_log = 0, 0, []
        _markets = market_data
        timeframes = frames

        def __init__(self, cfg=None):
            pass

        def load_markets(self):
            Fake.market_calls += 1
            return self._markets

        def fetch_ohlcv(self, sym, tf, since=None, limit=None):
            if fail_after is not None and Fake.calls >= fail_after:
                raise fail_with
            Fake.calls += 1
            Fake.since_log.append(since)
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            t = max(since, now - history_days * DAY) // DAY * DAY
            out = []
            while t <= now and len(out) < min(limit, max_candles):
                out.append([t, 100.0 + t // DAY % 50, 101.0, 99.0, 100.5 + t // DAY % 50, 1.0])
                t += DAY
            return out
    return Fake


def zip_csv(rows, microseconds=False, header=False):
    buf = io.StringIO()
    if header:
        buf.write("open_time,open,high,low,close,volume,close_time,qv,n,tb,tq,ignore\n")
    for t, o, h, l, c, v in rows:
        t = t * 1000 if microseconds else t
        buf.write(f"{t},{o},{h},{l},{c},{v},{t},0,0,0,0,0\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("data.csv", buf.getvalue())
    return out.getvalue()


class CacheTests(TestCase):
    def setUp(self):
        marketdata.reset_state()
        p = mock.patch.object(binance_archive, "http_get", side_effect=binance_archive.ArchiveError("kein Netz"))
        p.start()
        self.addCleanup(p.stop)

    def get(self, fake, exchange="kraken", start=date(2024, 1, 1), end=date(2024, 12, 31), tf="1d"):
        with mock.patch.object(ccxt, exchange, fake):
            return marketdata.get_candles(exchange, "BTC", tf, start, end)

    def test_second_identical_request_needs_no_api_call(self):
        fake = make_fake()
        df1, _ = self.get(fake)
        first = fake.calls
        self.assertGreater(first, 1)
        df2, _ = self.get(fake)
        self.assertEqual(fake.calls, first)  # alles aus dem Cache
        self.assertEqual(len(df1), 366)
        self.assertTrue(df1.equals(df2))

    def test_extension_to_the_right_loads_only_the_missing_part(self):
        fake = make_fake()
        self.get(fake, end=date(2024, 6, 30))
        fake.since_log.clear()
        df, _ = self.get(fake, end=date(2024, 9, 30))
        self.assertEqual(df.index[-1].date(), date(2024, 9, 30))
        self.assertEqual(fake.since_log[0], ms(date(2024, 7, 1)))  # erst ab dem ersten fehlenden Tag

    def test_extension_to_the_left_loads_only_the_missing_part(self):
        fake = make_fake()
        self.get(fake, start=date(2024, 6, 1), end=date(2024, 12, 31))
        fake.since_log.clear()
        df, _ = self.get(fake, start=date(2024, 3, 1), end=date(2024, 12, 31))
        self.assertEqual(df.index[0].date(), date(2024, 3, 1))
        self.assertEqual(fake.since_log[0], ms(date(2024, 3, 1)))
        self.assertLess(max(fake.since_log), ms(date(2024, 6, 1)))
        self.assertEqual(len(df), 306)

    def test_subrange_of_cache_needs_no_api_call(self):
        fake = make_fake()
        self.get(fake)
        calls = fake.calls
        df, _ = self.get(fake, start=date(2024, 4, 1), end=date(2024, 6, 30))
        self.assertEqual(fake.calls, calls)
        self.assertEqual(len(df), 91)

    def test_short_history_is_remembered_and_not_requested_again(self):
        fake = make_fake(history_days=200)
        start = date.today() - timedelta(days=500)
        df, info = self.get(fake, start=start, end=date.today())
        self.assertIn("lieferte nur", info["note"])
        calls = fake.calls
        self.get(fake, start=start, end=date.today())
        self.assertEqual(fake.calls, calls)  # "davor gibt es nichts" ist gemerkt

    def test_markets_loaded_once_and_symbol_fallback(self):
        fake = make_fake(markets={"BTC/USD": {"active": True}})
        _, info = self.get(fake)
        self.assertEqual(info["symbol"], "BTC/USD")
        self.get(fake, end=date(2024, 6, 30))
        self.get(fake, end=date(2024, 3, 31))
        self.assertEqual(fake.market_calls, 1)

    def test_binance_does_not_load_markets(self):
        fake = make_fake()
        _, info = self.get(fake, exchange="binance")
        self.assertEqual(fake.market_calls, 0)
        self.assertEqual(info["symbol"], "BTC/USDT")

    def test_unsupported_timeframe(self):
        with self.assertRaisesMessage(ValueError, "4h nicht an"):
            self.get(make_fake(), tf="4h")


class BlockTests(TestCase):
    def setUp(self):
        marketdata.reset_state()
        p = mock.patch.object(binance_archive, "http_get", side_effect=binance_archive.ArchiveError("kein Netz"))
        p.start()
        self.addCleanup(p.stop)

    def test_ban_time_is_parsed_from_binance_message(self):
        until, reason = marketdata.classify(ccxt.DDoSProtection(BAN_MESSAGE), datetime.now(timezone.utc))
        self.assertEqual(until.replace(microsecond=0), datetime(2026, 10, 2, 16, 37, 4, tzinfo=timezone.utc))
        self.assertIn("IP-Sperre", reason)

    def test_other_classifications(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(marketdata.classify(ccxt.RateLimitExceeded("429 too many"), now)[0], now + timedelta(minutes=2))
        self.assertEqual(marketdata.classify(ccxt.ExchangeNotAvailable("Service unavailable from a restricted location"), now)[0],
                         now + timedelta(hours=1))
        self.assertIsNone(marketdata.classify(ccxt.ExchangeNotAvailable("timeout"), now))

    def test_ban_stops_all_further_requests(self):
        future = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp() * 1000)
        msg = BAN_MESSAGE.replace("1790959019894", str(future))
        fake = make_fake(fail_after=1, fail_with=ccxt.DDoSProtection(msg))
        with mock.patch.object(ccxt, "binance", fake):
            with self.assertRaises(marketdata.ExchangeBlocked) as cm:
                marketdata.get_candles("binance", "BTC", "1d", date(2024, 1, 1), date(2024, 12, 31))
            self.assertIn("gesperrt", str(cm.exception))
            self.assertIn("keine Anfragen", str(cm.exception))
            calls = fake.calls
            self.assertTrue(ExchangeBlock.objects.filter(exchange="binance").exists())
            for _ in range(3):  # weitere Läufe senden NICHTS mehr an die Börse
                with self.assertRaises(marketdata.ExchangeBlocked):
                    marketdata.get_candles("binance", "ETH", "1d", date(2024, 1, 1), date(2024, 12, 31))
            self.assertEqual(fake.calls, calls)

    def test_cached_data_is_still_served_while_blocked(self):
        fake = make_fake()
        with mock.patch.object(ccxt, "kraken", fake):
            marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 12, 31))
            marketdata.set_block("kraken", datetime.now(timezone.utc) + timedelta(hours=1), "Test")
            calls = fake.calls
            df, info = marketdata.get_candles("kraken", "BTC", "1d", date(2024, 2, 1), date(2024, 3, 31))
            self.assertEqual(len(df), 60)
            self.assertEqual(fake.calls, calls)

    def test_small_blocked_tail_gives_partial_data_with_note(self):
        fake = make_fake()
        with mock.patch.object(ccxt, "kraken", fake):
            marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 9, 20))
            marketdata.set_block("kraken", datetime.now(timezone.utc) + timedelta(hours=1), "zu viele Anfragen")
            df, info = marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 9, 30))
        self.assertEqual(df.index[-1].date(), date(2024, 9, 20))
        self.assertIn("Daten enden am 20.09.2024", info["note"])
        self.assertIn("gesperrt", info["note"])
        self.assertLessEqual(len(info["note"]), 200)

    def test_large_blocked_gap_aborts_instead_of_truncating(self):
        fake = make_fake()
        with mock.patch.object(ccxt, "kraken", fake):
            marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 6, 30))
            marketdata.set_block("kraken", datetime.now(timezone.utc) + timedelta(hours=1), "zu viele Anfragen")
            with self.assertRaises(marketdata.ExchangeBlocked):  # 1/3 des Zeitraums fehlt
                marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 9, 30))

    def test_expired_block_is_ignored(self):
        marketdata.set_block("kraken", datetime.now(timezone.utc) - timedelta(minutes=1), "alt")
        self.assertIsNone(marketdata.active_block("kraken"))
        fake = make_fake()
        with mock.patch.object(ccxt, "kraken", fake):
            df, _ = marketdata.get_candles("kraken", "BTC", "1d", date(2024, 1, 1), date(2024, 6, 30))
        self.assertEqual(len(df), 182)


class ArchiveTests(TestCase):
    """Binance-Archiv: Monats-/Tagesdateien, Mikrosekunden, fehlende Dateien, API nur für den Rest."""

    def setUp(self):
        marketdata.reset_state()

    def rows(self, start: date, end: date, offset=0):
        return [(ms(start) + i * DAY, 10 + i + offset, 11, 9, 10.5 + i + offset, 1.0)
                for i in range((end - start).days + 1)]

    def serve(self, files):
        def http_get(url):
            return files.get(url)
        return mock.patch.object(binance_archive, "http_get", side_effect=http_get)

    def test_parse_handles_header_and_microseconds(self):
        r = self.rows(date(2025, 3, 1), date(2025, 3, 3))
        self.assertEqual(binance_archive.parse(zip_csv(r, microseconds=True, header=True))[0][0], ms(date(2025, 3, 1)))
        self.assertEqual(len(binance_archive.parse(zip_csv(r))), 3)

    def test_history_comes_from_archive_and_api_only_fills_the_gap(self):
        today = date.today()
        first_month = date(today.year - 1, 3, 1)
        files, d = {}, first_month
        while d.replace(day=1) < today.replace(day=1):  # alle vollständigen Monate als Monatsdatei
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            files[binance_archive.monthly_url("BTCUSDT", "1d", d.year, d.month)] = zip_csv(
                self.rows(d, nxt - timedelta(days=1), offset=0))
            d = nxt
        # laufender Monat: Tagesdateien bis vorgestern (gestern fehlt noch -> API)
        for i in range(1, (today - timedelta(days=2)).day + 1):
            day = today.replace(day=i)
            files[binance_archive.daily_url("BTCUSDT", "1d", ms(day))] = zip_csv(self.rows(day, day))
        fake = make_fake()
        with self.serve(files), mock.patch.object(ccxt, "binance", fake):
            df, info = marketdata.get_candles("binance", "BTC", "1d", first_month, today)
        self.assertEqual(df.index[0].date(), first_month)
        self.assertEqual(df.index[-1].date(), today - timedelta(days=1))
        self.assertLessEqual(fake.calls, 2)  # nur die letzten Tage kamen von der API
        self.assertGreaterEqual(Candle.objects.filter(exchange="binance").count(), 300)

    def test_archive_works_while_api_is_banned(self):
        today = date.today()
        end_prev = today.replace(day=1) - timedelta(days=1)        # Ende des Vormonats
        month = (end_prev.replace(day=1) - timedelta(days=62)).replace(day=1)  # drei volle Monate
        files, d = {}, month
        while d <= end_prev:
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            files[binance_archive.monthly_url("BTCUSDT", "1d", d.year, d.month)] = zip_csv(
                self.rows(d, nxt - timedelta(days=1)))
            d = nxt
        marketdata.set_block("binance", datetime.now(timezone.utc) + timedelta(hours=3), "IP-Sperre")
        fake = make_fake()
        with self.serve(files), mock.patch.object(ccxt, "binance", fake):
            df, info = marketdata.get_candles("binance", "BTC", "1d", month, end_prev)
        self.assertEqual(fake.calls, 0)  # kein einziger API-Aufruf
        self.assertEqual(df.index[-1].date(), end_prev)
        self.assertEqual(info["note"], "")

    def test_missing_old_months_are_skipped_without_daily_requests(self):
        requested = []
        def http_get(url):
            requested.append(url)
            return None
        today = date.today()
        with mock.patch.object(binance_archive, "http_get", side_effect=http_get), \
                mock.patch.object(ccxt, "binance", make_fake(history_days=100)):
            with self.assertRaises(ValueError):  # Archiv leer, API reicht nicht so weit zurück
                marketdata.get_candles("binance", "BTC", "1d", today - timedelta(days=400), today - timedelta(days=300))
        self.assertTrue(all("/monthly/" in u for u in requested))  # keine Tagesdateien für alte Monate

    def test_archive_outage_falls_back_to_api(self):
        fake = make_fake()
        with mock.patch.object(binance_archive, "http_get", side_effect=binance_archive.ArchiveError("down")), \
                mock.patch.object(ccxt, "binance", fake):
            df, _ = marketdata.get_candles("binance", "BTC", "1d", date(2024, 1, 1), date(2024, 12, 31))
        self.assertEqual(len(df), 366)
        self.assertGreater(fake.calls, 0)
