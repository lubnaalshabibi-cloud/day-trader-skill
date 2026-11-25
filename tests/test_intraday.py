"""trader-scanner intraday.py: data checks, indicators, setups, costs, ranking. Analysis only."""

import copy
import datetime
import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

intra = load("trader-scanner", "intraday")
scan = intra.scan

BUNDLE = os.path.join(FIX, "trader-scanner", "two-host-1mo5m-2026-11-25-1229.json.gz")  # real, both hosts
DT = datetime.datetime
D = datetime.date
DAY = D(2026, 11, 25)


def ts(et):
    return int((scan.eastern_to_utc(et) - DT(1970, 1, 1)).total_seconds())


def utc(et):
    return scan.eastern_to_utc(et)


def sessions_before(day, n):
    out, d = [], day
    while len(out) < n:
        d -= datetime.timedelta(days=1)
        if scan.session(d):
            out.append(d)
    return sorted(out)


def make_series(today_bars, prior=20, base=100.0, last_trade=None, sym="TEST", day=DAY):
    """A v8-like series: `prior` full quiet sessions, then today's bars [(o, h, l, c, v)] from 9:30."""
    t, o, h, l, c, v = [], [], [], [], [], []
    for k, d in enumerate(sessions_before(day, prior)):
        for i in range(78):
            px = base + math.sin((k * 78 + i) / 7.0) * 0.5
            t.append(ts(scan.slot(d, i)))
            o.append(px)
            h.append(px + 0.2)
            l.append(px - 0.2)
            c.append(px + 0.05)
            v.append(1000.0)
    for i, (bo, bh, bl, bc, bv) in enumerate(today_bars):
        t.append(ts(scan.slot(day, i)))
        o.append(bo)
        h.append(bh)
        l.append(bl)
        c.append(bc)
        v.append(bv)
    end = scan.slot(day, len(today_bars))
    meta = {"symbol": sym, "dataGranularity": "5m", "gmtoffset": -18000, "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "regularMarketTime": ts(last_trade or (end - datetime.timedelta(seconds=10)))}
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "meta": meta}


def orb_day():
    """A clean opening-range breakout: range 100.0 to 101.0 for 9:30 to 9:40, then a breakout at 10:20."""
    bars = [(100.2, 101.0, 100.0, 100.5, 5000), (100.5, 100.9, 100.1, 100.6, 3000), (100.6, 100.8, 100.2, 100.4, 3000)]
    px = 100.4
    for i in range(7):  # 9:45 to 10:15, drifting up under the range high
        bars.append((px, px + 0.25, px - 0.1, px + 0.07, 2500))
        px += 0.07
    bars.append((px, px + 0.9, px - 0.05, px + 0.8, 9000))  # 10:20 bar closes above 101.0
    return bars


def pair(today_bars, **kw):
    s = make_series(today_bars, **kw)
    return {"query1": copy.deepcopy(s), "query2": s}


def cutoff_for(bars):
    return scan.slot(DAY, len(bars))


def fetched_for(bars, seconds=5):
    f = utc(cutoff_for(bars)) + datetime.timedelta(seconds=seconds)
    return {"query1": f, "query2": f}


OPTS = {"commission": 1.0, "risk_usd": 50.0, "max_order": 4000, "min_rr": 1.0,
        "no_entries_before": datetime.time(9, 45), "no_entries_after": datetime.time(15, 30)}


def check(p, bars, live=True, **kw):
    return intra.validate("TEST", p["query1"], p["query2"], DAY, cutoff_for(bars), 3, fetched_for(bars), live, **kw)


class DataChecks(unittest.TestCase):
    def setUp(self):
        self.bars = orb_day()
        self.p = pair(self.bars)

    def test_identical_hosts_pass(self):
        r = check(self.p, self.bars)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["latency"]["age_s"], 15)          # last trade 10 s before the cutoff, fetched 5 s after
        self.assertEqual(r["latency"]["host_skew_s"], 0)

    def test_an_opening_range_conflict_is_rejected(self):
        k = self.p["query1"]["t"].index(ts(scan.slot(DAY, 0)))
        self.p["query1"]["h"][k] = 101.38                     # the NVDA case: 145.00 vs 143.62
        r = check(self.p, self.bars)
        self.assertFalse(r["ok"])
        self.assertIn("the opening range conflicts between Yahoo answers", r["reasons"][0])
        self.assertIn("2026-11-25 09:30 high: query1 101.38 vs query2 101.00", r["reasons"][0])

    def test_a_later_candle_conflict_is_rejected(self):
        k = self.p["query1"]["t"].index(ts(scan.slot(DAY, 5)))
        self.p["query1"]["l"][k] -= 0.30                     # still a possible candle, just a different one
        r = check(self.p, self.bars)
        self.assertIn("conflicting candles", r["reasons"][0])

    def test_a_historical_conflict_is_rejected(self):
        self.p["query2"]["l"][100] -= 1.0                     # a bar from an earlier session
        r = check(self.p, self.bars)
        self.assertFalse(r["ok"])
        self.assertIn("conflicting candles", " ".join(r["reasons"]))

    def test_the_range_1d_answers_must_match_today(self):
        one_day = {"query1/1d": copy.deepcopy(self.p["query2"]), "query2/1d": copy.deepcopy(self.p["query2"])}
        r = check(self.p, self.bars, extra=one_day)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["warnings"], [])
        k = one_day["query2/1d"]["t"].index(ts(scan.slot(DAY, 0)))
        one_day["query2/1d"]["l"][k] += 0.19                  # the WMT case: 132.40 vs 132.59
        r = check(self.p, self.bars, extra=one_day)
        self.assertIn("09:30 low: query2 100.00 vs query2/1d 100.19", r["reasons"][0])
        r = check(self.p, self.bars)
        self.assertIn("today's bars were not checked against a range=1d answer", r["warnings"])

    def test_inside_the_tolerance_is_agreement(self):
        k = self.p["query1"]["t"].index(ts(scan.slot(DAY, 5)))
        self.p["query1"]["c"][k] += 0.01                      # a cent: rounding, not a conflict
        self.assertTrue(check(self.p, self.bars)["ok"])

    def test_volume_on_the_last_bars_may_still_move(self):
        self.p["query1"]["v"][-1] *= 1.5                      # late prints on the newest bar: allowed
        self.assertTrue(check(self.p, self.bars)["ok"])
        self.p["query1"]["v"][-6] *= 1.5                      # an older bar: not allowed
        r = check(self.p, self.bars)
        self.assertIn("volume", r["reasons"][0])

    def test_an_impossible_candle_is_rejected(self):
        for s in self.p.values():
            k = s["t"].index(ts(scan.slot(DAY, 4)))
            s["h"][k] = s["c"][k] - 0.5                       # high under the close
        r = check(self.p, self.bars)
        self.assertIn("impossible candle at 2026-11-25 09:50", " ".join(r["reasons"]))

    def test_a_missing_bar_is_incomplete(self):
        for s in self.p.values():
            k = s["t"].index(ts(scan.slot(DAY, 6)))
            for key in ("t", "o", "h", "l", "c", "v"):
                del s[key][k]
        r = check(self.p, self.bars)
        self.assertIn("incomplete, 10 of 11 bars today, first missing 10:00", " ".join(r["reasons"]))

    def test_one_host_missing_is_no_cross_check(self):
        r = intra.validate("TEST", None, self.p["query2"], DAY, cutoff_for(self.bars), 3,
                           fetched_for(self.bars), True)
        self.assertIn("no data from query1", r["reasons"][0])

    def test_stale_data_is_rejected_live_but_disclosed_in_a_replay(self):
        old = scan.slot(DAY, len(self.bars)) - datetime.timedelta(minutes=12)
        p = pair(self.bars, last_trade=old)
        r = check(p, self.bars)
        self.assertIn("stale, the last trade is 725 s old (limit 300 s)", " ".join(r["reasons"]))
        r = check(p, self.bars, live=False)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["latency"]["age_s"], 725)

    def test_a_future_trade_time_is_rejected(self):
        p = pair(self.bars, last_trade=scan.slot(DAY, len(self.bars)) + datetime.timedelta(minutes=5))
        self.assertIn("in the future of the fetch", " ".join(check(p, self.bars)["reasons"]))

    def test_hosts_out_of_sync(self):
        self.p["query1"]["meta"]["regularMarketTime"] -= 200
        r = check(self.p, self.bars)
        self.assertIn("the hosts' last trades are 200 s apart", " ".join(r["reasons"]))

    def test_meta_must_match(self):
        self.p["query1"]["meta"]["dataGranularity"] = "1m"
        self.p["query2"]["meta"]["gmtoffset"] = -14400           # daylight time in November: wrong
        text = " ".join(check(self.p, self.bars)["reasons"])
        self.assertIn("query1: bars are 1m, not 5m", text)
        self.assertIn("query2: UTC offset -14400 s, New York is -18000 s", text)


class OpeningRangeMemory(unittest.TestCase):
    def test_a_changed_range_is_rejected_once(self):
        hist = {}
        bars = orb_day()
        cut = cutoff_for(bars)
        self.assertIsNone(intra.check_history(hist, DAY, "NVDA", 3, 145.00, 141.22, cut))
        self.assertIsNone(intra.check_history(hist, DAY, "NVDA", 3, 145.00, 141.22, cut))
        prob = intra.check_history(hist, DAY, "NVDA", 3, 143.62, 141.22, cut)
        self.assertIn("the opening range changed since the 10:25 scan (high 145.00 -> 143.62", prob)
        self.assertIsNone(intra.check_history(hist, DAY, "NVDA", 3, 143.62, 141.22, cut))  # steady again
        self.assertIsNone(intra.check_history(hist, DAY, "NVDA", 6, 146.00, 141.00, cut))  # other range length

    def test_analyze_uses_the_memory(self):
        bars = orb_day()
        hist = {"2026-11-25": {"TEST": [{"at": "10:00", "or_minutes": 15, "or_high": 101.5, "or_low": 100.0}]}}
        rows, _ = intra.analyze({"TEST": dict(pair(bars), _fetched=fetched_for(bars))}, ["TEST"], DAY,
                                cutoff_for(bars), 3, None, True, OPTS, hist)
        self.assertEqual(rows[0]["status"], "NO TRADE")
        self.assertIn("opening range changed since the 10:00 scan", rows[0]["reason"])


class Indicators(unittest.TestCase):
    def test_ema_and_macd(self):
        self.assertEqual(intra.ema([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])
        line, sig, hist = intra.macd([float(x) for x in range(1, 60)])
        self.assertIsNone(hist[32])
        self.assertIsNotNone(hist[33])                       # 26 + 9 - 1 bars
        self.assertAlmostEqual(line[-1], 7.0, places=6)      # a straight line: EMA gap = (26 - 12) / 2
        self.assertAlmostEqual(hist[-1], 0.0, places=6)

    def test_atr(self):
        bars = [{"h": 11.0, "l": 9.0, "c": 10.0}] * 15
        self.assertAlmostEqual(intra.atr(bars), 2.0)
        self.assertIsNone(intra.atr(bars[:14]))

    def test_vwap(self):
        today = [{"h": 11.0, "l": 9.0, "c": 10.0, "v": 100}, {"h": 13.0, "l": 11.0, "c": 12.0, "v": 300}]
        self.assertEqual(intra.vwap_series(today), [10.0, 11.5])

    def test_rvol_needs_five_full_sessions(self):
        today = [{"v": 200.0}] * 3
        prior = [[{"v": 100.0}] * 78] * 5
        self.assertAlmostEqual(intra.rvol(today, prior), 2.0)
        self.assertIsNone(intra.rvol(today, prior[:4]))

    def test_context_on_synthetic_bars(self):
        bars = orb_day()
        ctx = intra.indicators(intra.extract(make_series(bars), cutoff_for(bars))[0], DAY)
        self.assertEqual(len(ctx["today"]), 11)
        self.assertEqual(ctx["prior_sessions"], 20)
        self.assertAlmostEqual(ctx["rvol"], sum(b[4] for b in bars) / 11000.0)
        for k in ("atr5", "atr_d", "rsi", "macd_hist", "ema9", "ema20"):
            self.assertIsNotNone(ctx[k], k)


def ctx_of(closes, **over):
    """A hand-made context for the setup rules: bars from (o, h, l, c, v)."""
    today = [{"t": scan.slot(DAY, i), "o": o, "h": h, "l": l, "c": c, "v": v} for i, (o, h, l, c, v) in enumerate(closes)]
    ctx = {"today": today, "vwap": [100.0] * len(today), "atr5": 0.5, "atr_d": 3.0, "rvol": 2.0,
           "ema9": 101.0, "ema20": 100.5, "macd_hist": 0.2, "macd_hist_prev": 0.1, "rsi": 65.0,
           "median_vol": 1000.0, "prev_close": 99.0, "dollar_volume": 5e7, "prior_sessions": 20}
    ctx.update(over)
    return ctx


class Setups(unittest.TestCase):
    def test_orb_confirmed_then_stale(self):
        bars = orb_day()
        ctx = intra.indicators(intra.extract(make_series(bars), cutoff_for(bars))[0], DAY)
        s = intra.setup_orb(ctx, 3)
        self.assertEqual((s["setup"], s["status"], s["entry"], s["stop"]), ("orb", "confirmed", 101.69, 100.5))
        self.assertIn("closed above the range high 101.00 at 10:25 AM", s["why"])
        later = bars + [(101.3, 101.5, 101.2, 101.4, 3000)] * 6
        ctx = intra.indicators(intra.extract(make_series(later), cutoff_for(later))[0], DAY)
        s = intra.setup_orb(ctx, 3)
        self.assertEqual(s["status"], "developing")
        self.assertIn("6 bars ago", s["notes"][0])

    def test_orb_developing_under_the_range(self):
        bars = orb_day()[:-1]
        ctx = intra.indicators(intra.extract(make_series(bars), cutoff_for(bars))[0], DAY)
        ctx["vwap"][-1] = 100.0
        s = intra.setup_orb(ctx, 3)
        self.assertEqual((s["status"], s["entry"]), ("developing", 101.01))
        self.assertEqual(s["trigger"], "close above 101.00")

    def test_a_failed_breakout_is_no_setup(self):
        bars = orb_day() + [(101.2, 101.3, 100.4, 100.6, 4000)]
        ctx = intra.indicators(intra.extract(make_series(bars), cutoff_for(bars))[0], DAY)
        self.assertIsNone(intra.setup_orb(ctx, 3))

    def test_momentum(self):
        rows = [(100.0 + i * 0.2, 100.3 + i * 0.2, 99.9 + i * 0.2, 100.2 + i * 0.2, 1000) for i in range(8)]
        rows[-1] = (101.4, 102.2, 101.35, 102.1, 1500)        # closes over the prior 6 bars' high
        s = intra.setup_momentum(ctx_of(rows))
        self.assertEqual((s["status"], s["entry"]), ("confirmed", 102.1))
        self.assertEqual(s["stop"], round(min(r[2] for r in rows[-6:]) - 0.01, 2))
        s = intra.setup_momentum(ctx_of(rows, rvol=1.1, macd_hist=0.05))
        self.assertEqual(s["status"], "developing")
        self.assertEqual(s["notes"], ["RVOL 1.10 under 1.2", "MACD histogram falling"])
        self.assertIsNone(intra.setup_momentum(ctx_of(rows, rsi=85.0)))   # extended: no setup

    def test_vwap_reclaim(self):
        rows = [(100.5, 100.6, 100.0, 100.2, 1000)] * 3 + [(99.9, 100.0, 99.5, 99.7, 1000)] * 4
        rows.append((99.8, 100.5, 99.7, 100.4, 1400))         # back over VWAP 100.00, upper half
        s = intra.setup_vwap_reclaim(ctx_of(rows))
        self.assertEqual((s["status"], s["entry"], s["stop"]), ("confirmed", 100.4, 99.49))
        rows[-1] = (99.8, 99.95, 99.75, 99.9, 900)             # 10 cents under VWAP, histogram rising
        s = intra.setup_vwap_reclaim(ctx_of(rows))
        self.assertEqual((s["status"], s["entry"]), ("developing", 100.01))

    def test_pullback(self):
        rows = [(100.0, 100.3, 99.9, 100.2, 1000), (100.2, 100.8, 100.1, 100.7, 1000),
                (100.7, 101.3, 100.6, 101.2, 1000),           # the high, 101.30 (1.3 over the open)
                (101.1, 101.15, 100.6, 100.7, 900), (100.7, 100.8, 100.45, 100.6, 800),
                (100.6, 101.0, 100.55, 100.95, 1100)]          # closes over the bar before's high, over EMA 9
        s = intra.setup_pullback(ctx_of(rows, ema9=100.8, ema20=100.5))
        self.assertEqual((s["status"], s["entry"], s["stop"]), ("confirmed", 100.95, 100.44))
        rows[-1] = (100.6, 100.75, 100.5, 100.7, 900)
        s = intra.setup_pullback(ctx_of(rows, ema9=100.8, ema20=100.5))
        self.assertEqual((s["status"], s["entry"]), ("developing", 100.76))


class Costs(unittest.TestCase):
    def test_commission_per_transaction(self):
        p = intra.plan_trade(100.0, 99.0, 50.0, 1.0)
        self.assertEqual(p["shares"], 48)                     # (50 - 2) / 1.00
        self.assertEqual((p["gross_loss_at_stop"], p["net_loss_at_stop"]), (48.0, 50.0))
        self.assertEqual((p["target"], p["gross_at_target"], p["net_at_target"]), (102.0, 96.0, 94.0))
        self.assertEqual(p["net_reward_risk"], 1.88)
        self.assertEqual(p["breakeven"], 100.04)              # $2 over 48 shares
        free = intra.plan_trade(100.0, 99.0, 50.0, 0.0)
        self.assertEqual((free["shares"], free["net_reward_risk"]), (50, 2.0))

    def test_max_order_caps_and_too_wide_is_zero(self):
        self.assertEqual(intra.plan_trade(100.0, 99.9, 50.0, 1.0, max_order=4000)["shares"], 40)
        z = intra.plan_trade(100.0, 40.0, 50.0, 1.0)
        self.assertEqual(z["shares"], 0)
        self.assertIn("one share risks $60.00 plus $2.00 in commissions", z["why_zero"])

    def test_confirmed_needs_net_reward_risk_and_shares(self):
        bars = orb_day()
        ctx = intra.indicators(intra.extract(make_series(bars), cutoff_for(bars))[0], DAY)
        s = intra.finalize(intra.setup_orb(ctx, 3), ctx, dict(OPTS, min_rr=1.95), True, "")
        self.assertEqual(s["status"], "developing")
        self.assertIn("net reward:risk 1.88 under your minimum 1.95", s["notes"])
        s = intra.finalize(intra.setup_orb(ctx, 3), ctx, dict(OPTS, risk_usd=2.5), True, "")
        self.assertIn("plus $2.00 in commissions", s["notes"][-1])

    def test_confirmed_needs_every_indicator(self):
        bars = orb_day()
        ctx = intra.indicators(intra.extract(make_series(bars, prior=3), cutoff_for(bars))[0], DAY)
        self.assertIsNone(ctx["rvol"])
        s = intra.setup_orb(ctx, 3)
        s = intra.finalize(s, ctx, OPTS, True, "")
        self.assertEqual(s["status"], "developing")
        self.assertIn("insufficient data to confirm: no RVOL", " ".join(s["notes"]))


class Ranking(unittest.TestCase):
    def test_catalysts_from_trader_news(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "news.json")
            with open(p, "w") as fh:
                json.dump({"items": [
                    {"ticker": "TSLA", "touches": ["TSLA"], "tags": ["earnings"], "time": "2026-11-25T13:00:00+00:00"},
                    {"ticker": "AMD", "touches": ["AMD", "NVDA"], "tags": [], "time": "2026-11-25T14:00:00Z"},
                    {"ticker": "KO", "touches": ["KO"], "tags": ["deal"], "time": "2026-11-20T14:00:00Z"}]}, fh)
            cats = intra.load_catalysts(p, DT(2026, 11, 25, 17, 0))
        self.assertEqual(sorted(cats), ["AMD", "NVDA", "TSLA"])  # KO's is five days old
        self.assertEqual(cats["TSLA"]["tags"], {"earnings"})

    def test_score_parts(self):
        rows = [(100.0 + i * 0.2, 100.3 + i * 0.2, 99.9 + i * 0.2, 100.2 + i * 0.2, 1000) for i in range(8)]
        ctx = ctx_of(rows)
        best = {"plan": {"net_reward_risk": 2.0}}
        total, parts, note = intra.score(ctx, best, {"tags": {"earnings"}, "items": 1})
        self.assertEqual(parts["catalyst"], 20)
        self.assertEqual(parts["reward_risk"], 12.5)
        self.assertEqual(note, "news: earnings")
        self.assertAlmostEqual(total, sum(parts.values()), places=1)
        _, parts, note = intra.score(ctx, best, None)
        self.assertEqual((parts["catalyst"], note), (0.0, None))   # gap under 2 %: nothing claimed


class FeedProblem(unittest.TestCase):
    def test_most_symbols_rejected_means_nothing_confirmed(self):
        bars = orb_day()
        good = dict(pair(bars, sym="A"), _fetched=fetched_for(bars))
        bad1 = dict(pair(bars, sym="B"), _fetched=fetched_for(bars))
        bad2 = dict(pair(bars, sym="C"), _fetched=fetched_for(bars))
        for b in (bad1, bad2):
            b["query1"]["c"][-3] += 1.0
        rows, summ = intra.analyze({"A": good, "B": bad1, "C": bad2}, ["A", "B", "C"], DAY, cutoff_for(bars), 3,
                                   None, True, dict(OPTS), None)
        self.assertTrue(summ["feed_problem"])
        self.assertEqual(summ["confirmed"], 0)
        a = [r for r in rows if r["symbol"] == "A"][0]
        self.assertEqual(a["status"], "DEVELOPING")
        self.assertIn("feed problem", " ".join(a["signals"][0]["notes"]))

    def test_outside_the_entry_window_nothing_is_confirmed(self):
        bars = orb_day()
        rows, summ = intra.analyze({"A": dict(pair(bars, sym="A"), _fetched=fetched_for(bars))}, ["A"], DAY,
                                   cutoff_for(bars), 3, None, True,
                                   dict(OPTS, no_entries_after=datetime.time(10, 0)), None)
        self.assertFalse(summ["entry_window_ok"])
        self.assertEqual(rows[0]["status"], "DEVELOPING")
        self.assertIn("after your last entry time 10:00", rows[0]["signals"][0]["notes"])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = intra.main(argv)
    return code, out.getvalue(), err.getvalue()


class LivePath(unittest.TestCase):
    """run() with a fake network: both hosts per symbol, one re-fetch on a conflict."""

    def go(self, flaky_times):
        bars = orb_day()
        now = utc(cutoff_for(bars)) + datetime.timedelta(seconds=5)
        calls = []

        def fake(host, sym, rng):
            calls.append((host, sym, rng))
            s = make_series(bars, sym=sym)
            if host == "query1" and rng == "1mo" and \
                    sum(1 for c in calls if c[0] == "query1" and c[2] == "1mo") <= flaky_times:
                k = s["t"].index(ts(scan.slot(DAY, 0)))
                s["h"][k] += 1.38
            return s, now
        old = intra.fetch_host, scan.parse_now, scan.SLEEP
        real = scan.parse_now
        intra.fetch_host, scan.SLEEP = fake, (lambda x: None)
        scan.parse_now = lambda text: real(text) if text else now
        d = tempfile.mkdtemp()
        try:
            code, out, err = run(["--symbols", "ORB", "--json", "--no-write", "--config", os.path.join(d, "x"),
                                  "--state", os.path.join(d, "or.json")])
        finally:
            intra.fetch_host, scan.parse_now, scan.SLEEP = old
        return code, json.loads(out), calls

    def test_agreement_on_the_refetch_is_used_with_a_warning(self):
        code, doc, calls = self.go(1)
        self.assertEqual(len(calls), 8)                       # 1mo and 1d from both hosts, twice
        self.assertEqual([c[2] for c in calls[:4]], ["1mo", "1mo", "1d", "1d"])
        r = doc["symbols"][0]
        self.assertTrue(r["data_ok"])
        self.assertIn("the hosts disagreed on the first fetch and agreed on the re-fetch", r["warnings"])
        self.assertEqual(r["status"], "CONFIRMED")
        self.assertEqual(doc["hits"], [{"symbol": "ORB", "setups": ["orb"], "status": "CONFIRMED"}])
        self.assertEqual(code, 0)

    def test_a_persistent_conflict_is_no_trade(self):
        code, doc, calls = self.go(9)
        self.assertEqual(len(calls), 8)                       # one re-fetch, no more
        self.assertEqual(doc["symbols"][0]["status"], "NO TRADE")
        self.assertEqual(doc["hits"], [])
        self.assertEqual(code, 1)


class RealBundle(unittest.TestCase):
    """Real Yahoo charts from both hosts, 12:29 PM ET Wed Nov 25 2026."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--offline", BUNDLE, "--config", os.path.join(self.d, "none.json"),
                     "--state", os.path.join(self.d, "or.json"), "--out", os.path.join(self.d, "intraday.json")]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d)

    def test_the_hosts_really_disagreed_on_nvda_and_meta(self):
        code, out, _ = run(self.base + ["--json"])
        doc = json.loads(out)
        rows = {r["symbol"]: r for r in doc["symbols"]}
        self.assertEqual(sorted(s for s, r in rows.items() if not r["data_ok"]), ["META", "NVDA"])
        self.assertIn("2026-11-25 09:30 high: query1 145.00 vs query2 143.62", rows["NVDA"]["reason"])
        self.assertIn("the opening range conflicts", rows["META"]["reason"])
        self.assertEqual(doc["summary"]["data_rejected"], 2)
        self.assertFalse(doc["summary"]["feed_problem"])
        self.assertEqual((doc["day"], doc["cutoff"], doc["live"]), ("2026-11-25", "12:29", True))
        self.assertEqual(doc["mode"], "analysis-only")
        self.assertEqual(doc["orders"], "none")

    def test_latency_is_disclosed(self):
        code, out, _ = run(self.base + ["--json"])
        rows = {r["symbol"]: r for r in json.loads(out)["symbols"]}
        lat = rows["AAPL"]["latency"]
        self.assertEqual(lat["query2"]["last_trade_et"], "2026-11-25 12:28:57")
        self.assertLessEqual(lat["age_s"], 300)
        self.assertIn("host_skew_s", lat)

    def test_vwap_matches_the_raw_bars(self):
        code, out, _ = run(self.base + ["--json", "--symbols", "AAPL"])
        row = json.loads(out)["symbols"][0]
        data, _ = intra.load_bundle(BUNDLE)
        s = data["AAPL"]["query2"]
        pv = vv = 0.0
        for i, t in enumerate(s["t"]):
            et = scan.ts_to_eastern(t)
            if et.date() == DAY and not et.minute % 5 and not et.second and et.time() >= scan.OPEN \
                    and et + datetime.timedelta(minutes=5) <= DT(2026, 11, 25, 12, 29) and s["c"][i] is not None:
                pv += (s["h"][i] + s["l"][i] + s["c"][i]) / 3.0 * s["v"][i]
                vv += s["v"][i]
        self.assertAlmostEqual(row["vwap"], round(pv / vv, 2), places=2)
        self.assertEqual(row["bar_close_et"], "12:25")
        self.assertGreaterEqual(row["prior_sessions"], 15)

    def test_text_report_and_files(self):
        code, out, _ = run(self.base)
        self.assertIn("Yahoo query1 + query2 cross-checked", out)
        self.assertIn("6 symbols, 2 rejected by the data checks", out)
        self.assertIn("NVDA   NO TRADE  data: the opening range conflicts", out)
        self.assertIn("$1.00 a transaction, $2.00 a round trip", out)
        self.assertIn("not advice and not orders", out)
        with open(os.path.join(self.d, "intraday.json")) as fh:
            doc = json.load(fh)
        self.assertTrue(all(h["status"] in ("CONFIRMED", "DEVELOPING") for h in doc["hits"]))
        with open(os.path.join(self.d, "or.json")) as fh:
            hist = json.load(fh)
        self.assertEqual(sorted(hist["2026-11-25"]), ["AAPL", "AMD", "JPM", "TSLA"])  # only validated ranges

    def test_a_replay_does_not_enforce_age(self):
        code, out, _ = run(self.base + ["--json", "--at", "11:00", "--no-history"])
        doc = json.loads(out)
        self.assertFalse(doc["live"])
        self.assertEqual(doc["cutoff"], "11:00")

    def test_plain_errors(self):
        code, _, err = run(["--symbols", "AAPL", "--now", "2026-11-25 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("--now needs --offline", err)
        code, _, err = run(self.base + ["--at", "09:40", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("the 15-minute opening range runs 9:30 to 9:45 AM", err)


if __name__ == "__main__":
    unittest.main()
