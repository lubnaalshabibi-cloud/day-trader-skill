#!/usr/bin/env python3
"""Intraday Analyzer for trader-scanner: data-checked intraday setups. Analysis only. Standard library only.

    python3 intraday.py --symbols NVDA,AMD,META          # live, both Yahoo hosts, now
    python3 intraday.py --watchlist trader/watchlist.json
    python3 intraday.py --symbols TSLA --catalysts news.json --json
    python3 intraday.py --offline bundle.json.gz         # a saved two-host bundle, as of its fetch time
    python3 intraday.py --offline bundle.json.gz --at 11:00   # replay the same bundle at 11:00 AM ET

It never places, sizes for a broker, or sends an order. It prints rule matches,
not advice, and says NO TRADE whenever the data is not good enough.

Data checks (any one of these rejects a symbol: NO TRADE, data):
  cross-check   every regular-session 5-minute bar is fetched from BOTH
                query1 and query2. Open, high, low and close must agree
                within max(1 cent, 0.05%); volume within 5% (the last 3 bars of
                today are skipped for volume: late prints still land). A bar
                one host has and the other does not is a conflict.
                A live conflict is re-fetched once; if it persists, reject.
                Today's bars are ALSO fetched with range=1d from both hosts
                (Yahoo caches per URL: on Nov 25 2026 query2 served WMT a
                different 9:30 bar for range=5d/1mo than for 1d) and must
                match too: four answers for today, two for the history.
  candles       high below the open or close, low above them, a price at or
                under zero, negative volume, a bar with some prices missing,
                two different bars at the same time.
  complete      every 5-minute bar from 9:30 to the cutoff today is there.
  opening range the range high and low are remembered per day
                (trader/state/or_history.json). A later scan that sees a
                different range for the same day is rejected: settled bars
                must not change.
  timestamps    Yahoo's chart meta must say 5m bars, the requested symbol and
                New York's UTC offset; the last trade time must not be in the
                future; live, the last trade must be at most --max-age
                seconds (300) old, and the two hosts at most 120 s apart.
A scan where more than half the symbols are rejected is treated as a feed
problem: nothing is CONFIRMED.

Latency: the age of the last trade at fetch time (fetch time minus Yahoo's
regularMarketTime) is printed per symbol, with the worst and the median. It is
a lower bound on the real delay: Yahoo's free feed is unofficial, and the bars
are 5-minute bars, complete only when the next one starts.

Indicators (on query2's bars once both hosts agree; None when there is not
enough data, and a signal is never CONFIRMED on a missing indicator):
  VWAP      today's session, typical price (H+L+C)/3 weighted by volume
  RVOL      today's volume through the last bar / the average volume through
            the same bar of the prior sessions (at least 5 full ones)
  ATR       14-bar Wilder ATR on 5-minute bars, and 14-day ATR on daily bars
            built from complete prior sessions
  RSI       14-bar Wilder RSI on 5-minute closes
  MACD      12/26/9 on 5-minute closes; EMA 9 and EMA 20 for the trend
Indicators run across sessions (regular hours only), as most charting does.

Setups, long side only, each CONFIRMED or DEVELOPING:
  orb         a completed bar CLOSED above the opening-range high, the price
              is still above it and above VWAP, the breakout bar's volume is
              at least the median bar's, RVOL >= 1, within the last 6 bars.
              Developing: under the range high by at most half an ATR, above VWAP.
              Stop: the range midpoint.
  momentum    above VWAP, EMA 9 over EMA 20, MACD histogram positive, RSI 50
              to 80, up on the day; confirmed when the last bar closes above
              the 6 bars before it on at least average volume, RVOL >= 1.2 and
              a rising histogram. Stop: the low of the last 6 bars.
  vwap_reclaim  2+ of the 6 bars before closed under VWAP, the bar before
              too, and the last bar closed above it; confirmed on average
              volume, a close in the upper half and a rising histogram.
              Stop: the low of the last 3 bars.
  pullback    an uptrend (EMA 9 over 20, above VWAP) that made its high at
              least 1 ATR over the open, pulled back half an ATR or more,
              held VWAP and touched EMA 20 or VWAP; confirmed when the last
              bar closes above the bar before's high and over EMA 9.
              Stop: the pullback low.
Every target is 2R. Confirmed also needs: a stop at least a quarter ATR away,
1 share or more inside your max loss, net reward:risk at least your minimum,
and the time inside your entry window.

Costs: --commission (default 1.00, or commission_per_trade_usd in
trader/config.json) is charged per transaction, so twice a round trip. Shares
= (max loss - 2 commissions) / risk a share, capped by max_order_usd. Net at
the stop = shares x risk + 2 commissions; net at the target = shares x reward
- 2 commissions; net reward:risk is the ratio. Planned numbers, not results:
a stop can fill worse, and the spread is not in Yahoo's bars.

Ranking (0 to 100, confirmed first, then developing): momentum 30 (move from
the open in daily ATRs, MACD, RSI, distance over VWAP), liquidity 25 (dollars
traded today, RVOL), catalysts 20 (--catalysts: trader-news --json output or
{"TSLA": ["earnings"]}; without news, a gap of 2%+ counts a little) and
reward:risk 25 (net, of the best signal).

Writes trader/intraday.json (hits[] is a watchlist trader-entry can read).
Exit code 0 when anything is confirmed or developing, 1 otherwise.
"""

import argparse
import datetime
import gzip
import importlib.util
import json
import math
import os
import sys
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_scan():
    spec = importlib.util.spec_from_file_location("trader_scan_core", os.path.join(HERE, "scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()
ScanError = scan.ScanError

BAR = 5
HOSTS = {"query1": "https://query1.finance.yahoo.com", "query2": "https://query2.finance.yahoo.com"}
PRIMARY = "query2"
EXTRA = ("query1/1d", "query2/1d")
TOL_ABS = 0.011
TOL_BPS = 5.0
VOL_PCT = 5.0
VOL_SKIP_RECENT = 3
MAX_AGE = 300
MAX_SKEW = 120
FRESH_BARS = 6
MIN_PRIOR_SESSIONS = 5
ATR_N = 14
COMMISSION = 1.00
RISK_USD = 50.0
MIN_RR = 1.0
REJECT_SHARE = 0.5
NOTE = ("Analysis only: rule matches on free, unofficial, delayed data, not advice and not orders. "
        "Nothing here is sent to a broker.")
SETUP_WORDS = {"orb": "opening-range breakout", "momentum": "momentum continuation",
               "vwap_reclaim": "VWAP reclaim", "pullback": "pullback in an uptrend"}
HIGH_IMPACT = {"earnings", "guidance", "deal", "merger", "acquisition", "fda", "upgrade", "downgrade",
               "analyst", "halt", "offering", "bankruptcy", "ceo", "regulatory"}


class DataError(Exception):
    pass


# ---------------------------------------------------------------- bars

def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def extract(series, cutoff):
    """Regular-session bars on the 5-minute grid, complete by `cutoff` (naive New York).
    -> ({date: [bar]}, duplicates [time], off_grid count). A bar keeps None values."""
    days, dups, off = {}, [], 0
    o, h, l, v = series.get("o"), series.get("h"), series.get("l"), series.get("v")
    for i, ts in enumerate(series["t"]):
        et = scan.ts_to_eastern(ts)
        s = scan.session(et.date())
        if s is None or et.time() < scan.OPEN or et.time() >= s[1]:
            continue
        if et.second or et.minute % BAR:
            off += 1
            continue
        if et + datetime.timedelta(minutes=BAR) > cutoff:
            continue
        b = {"t": et, "o": _f(o[i]) if o else None, "h": _f(h[i]) if h else None,
             "l": _f(l[i]) if l else None, "c": _f(series["c"][i]), "v": _f(v[i]) if v else None}
        d = days.setdefault(et.date(), {})
        if et in d and any(d[et][k] != b[k] for k in "ohlcv"):
            dups.append(et)
        d[et] = b
    return {d: [m[k] for k in sorted(m)] for d, m in days.items()}, dups, off


def candle_problem(b):
    vals = [b[k] for k in "ohlc"]
    if all(x is None for x in vals):
        return None  # an empty bar: no trades, handled as missing
    if any(x is None for x in vals):
        return "some prices missing"
    o, h, l, c = vals
    if min(vals) <= 0:
        return "a price at or under zero"
    if h + 1e-9 < max(o, c, l) or l - 1e-9 > min(o, c, h):
        return "high %.4f / low %.4f do not contain open %.4f and close %.4f" % (h, l, o, c)
    if b["v"] is not None and b["v"] < 0:
        return "negative volume"
    return None


def expected_slots(day, cutoff):
    s = scan.session(day)
    if s is None:
        return 0
    end = min(cutoff, datetime.datetime.combine(day, s[1]))
    return max(0, int((end - scan.slot(day, 0)).total_seconds() // (BAR * 60)))


def full_session(bars, day):
    """A prior session is full when every slot to the close has a price."""
    s = scan.session(day)
    if s is None:
        return False
    want = expected_slots(day, datetime.datetime.combine(day, s[1]))
    good = [b for b in bars if b["c"] is not None]
    return len(good) == want and all(b["t"] == scan.slot(day, k) for k, b in enumerate(good))


# ---------------------------------------------------------------- validation

def tolerance(x, y):
    return max(TOL_ABS, TOL_BPS / 1e4 * max(abs(x), abs(y)))


def compare_hosts(d1, d2, today, n_range, names=("query1", "query2")):
    """Two hosts' {date: [bar]} -> list of conflicts (dicts). The oldest day may be cut
    differently by each host's range window, so a day only one host has is skipped when
    it is the oldest one."""
    out = []
    days = sorted(set(d1) | set(d2))
    oldest = days[0] if days else None
    for d in days:
        if (d in d1) != (d in d2):
            if d != oldest:
                out.append({"kind": "missing day", "date": d.isoformat(),
                            "detail": "%s only in %s" % (d.isoformat(), names[0] if d in d1 else names[1])})
            continue
        m1 = {b["t"]: b for b in d1[d]}
        m2 = {b["t"]: b for b in d2[d]}
        recent = sorted(m1)[-VOL_SKIP_RECENT:] if d == today else []
        for t in sorted(set(m1) | set(m2)):
            a, b = m1.get(t), m2.get(t)
            in_range = d == today and t < scan.slot(d, n_range)
            where = t.strftime("%Y-%m-%d %H:%M")
            if a is None or b is None:
                out.append({"kind": "missing bar", "at": where, "opening_range": in_range,
                            "detail": "bar %s only in %s" % (where, names[0] if a else names[1])})
                continue
            for k, word in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
                x, y = a[k], b[k]
                if (x is None) != (y is None):
                    out.append({"kind": "price", "at": where, "field": word, names[0]: x, names[1]: y,
                                "opening_range": in_range,
                                "detail": "%s %s: %s vs %s" % (where, word, _px(x), _px(y))})
                elif x is not None and abs(x - y) > tolerance(x, y):
                    out.append({"kind": "price", "at": where, "field": word, names[0]: round(x, 4),
                                names[1]: round(y, 4), "opening_range": in_range,
                                "detail": "%s %s: %s %s vs %s %s" % (where, word, names[0], _px(x), names[1], _px(y))})
            if t in recent:
                continue
            x, y = a["v"], b["v"]
            if x is not None and y is not None and abs(x - y) > max(100.0, VOL_PCT / 100.0 * max(x, y)):
                out.append({"kind": "volume", "at": where, "field": "volume", names[0]: x, names[1]: y,
                            "opening_range": in_range,
                            "detail": "%s volume: %s vs %s" % (where, "{:,.0f}".format(x), "{:,.0f}".format(y))})
    return out


def check_meta(meta, sym, now_utc):
    """Chart meta sanity -> list of problems."""
    out = []
    g = meta.get("dataGranularity")
    if g and g != "5m":
        out.append("bars are %s, not 5m" % g)
    ys = (meta.get("symbol") or "").upper()
    if ys and ys != scan.to_yahoo(sym):
        out.append("Yahoo answered for %s, not %s" % (ys, sym))
    off = meta.get("gmtoffset")
    if off is not None:
        want = scan.eastern_offset(now_utc) * 3600
        if int(off) != want:
            out.append("UTC offset %+d s, New York is %+d s" % (int(off), want))
    tz = meta.get("exchangeTimezoneName")
    if tz and tz != "America/New_York":
        out.append("exchange time zone %s" % tz)
    cur = meta.get("currency")
    if cur and cur != "USD":
        out.append("priced in %s" % cur)
    return out


def validate(sym, s1, s2, day, cutoff, n_range, fetched, live, max_age=MAX_AGE, max_skew=MAX_SKEW, extra=None):
    """Two hosts' series for one symbol (plus `extra` {name: series}, e.g. the range=1d answers,
    compared on today's bars only) -> report dict:
    {"ok", "reasons", "warnings", "latency", "days" ({date: [bar]} of the primary host)}"""
    reasons, warns = [], []
    rep = {"ok": False, "reasons": reasons, "warnings": warns, "latency": {}, "days": None,
           "conflicts": []}
    series = {"query1": s1, "query2": s2}
    for name, s in series.items():
        if s is None:
            reasons.append("no data from %s, so nothing to cross-check against" % name)
    if reasons:
        return rep
    parsed = {}
    for name, s in series.items():
        f_utc = fetched.get(name)
        for p in check_meta(s.get("meta") or {}, sym, f_utc or scan.eastern_to_utc(cutoff)):
            reasons.append("%s: %s" % (name, p))
        days, dups, off = extract(s, cutoff)
        if dups:
            reasons.append("%s: two different bars at %s" % (name, dups[0].strftime("%H:%M")))
        if off > 1:
            warns.append("%s: %d bars off the 5-minute grid were dropped" % (name, off))
        bad = None
        for d in sorted(days):
            for b in days[d]:
                p = candle_problem(b)
                if p:
                    bad = "%s: impossible candle at %s (%s)" % (name, b["t"].strftime("%Y-%m-%d %H:%M"), p)
                    break
            if bad:
                break
        if bad:
            reasons.append(bad)
        parsed[name] = days
        rmt = (s.get("meta") or {}).get("regularMarketTime")
        lat = {"fetched_at": f_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if f_utc else None}
        if rmt:
            rmt_utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(rmt))
            lat["last_trade_et"] = scan.ts_to_eastern(rmt).strftime("%Y-%m-%d %H:%M:%S")
            if f_utc:
                age = (f_utc - rmt_utc).total_seconds()
                lat["age_s"] = int(age)
                if age < -60:
                    reasons.append("%s: the last trade time is %d s in the future of the fetch" % (name, -age))
                elif live and age > max_age:
                    reasons.append("%s: stale, the last trade is %d s old (limit %d s)" % (name, age, max_age))
        else:
            reasons.append("%s: no last trade time (regularMarketTime)" % name)
        rep["latency"][name] = lat
    a1 = rep["latency"]["query1"].get("age_s")
    a2 = rep["latency"]["query2"].get("age_s")
    t1 = (s1.get("meta") or {}).get("regularMarketTime")
    t2 = (s2.get("meta") or {}).get("regularMarketTime")
    if t1 and t2:
        skew = abs(int(t1) - int(t2))
        rep["latency"]["host_skew_s"] = skew
        if skew > max_skew:
            reasons.append("the hosts' last trades are %d s apart (limit %d s): one feed is behind" % (skew, max_skew))
    ages = [a for a in (a1, a2) if a is not None]
    rep["latency"]["age_s"] = max(ages) if ages else None

    today_p = parsed[PRIMARY].get(day, [])
    want = expected_slots(day, cutoff)
    for name in ("query1", "query2"):
        tb = [b for b in parsed[name].get(day, []) if b["c"] is not None]
        if len(tb) != want or any(b["t"] != scan.slot(day, k) for k, b in enumerate(tb)):
            have = {b["t"] for b in tb}
            miss = [scan.slot(day, k) for k in range(want) if scan.slot(day, k) not in have]
            reasons.append("%s: incomplete, %d of %d bars today%s" % (
                name, len(tb), want, (", first missing %s" % miss[0].strftime("%H:%M")) if miss else ""))
    conflicts = compare_hosts(parsed["query1"], parsed["query2"], day, n_range)
    if extra is None:
        warns.append("today's bars were not checked against a range=1d answer")
    for name, s in sorted((extra or {}).items()):
        if s is None:
            reasons.append("no data from %s" % name)
            continue
        e_days = extract(s, cutoff)[0]
        conflicts += compare_hosts({day: parsed[PRIMARY].get(day, [])}, {day: e_days.get(day, [])}, day, n_range,
                                   names=(PRIMARY, name))
    rep["conflicts"] = conflicts
    if conflicts:
        orc = [c for c in conflicts if c.get("opening_range")]
        pick = orc or conflicts
        first = next((c for c in pick if c.get("field") in ("high", "low")), pick[0])  # what sets the range
        reasons.append("%s between Yahoo answers (%d bar%s differ; first: %s)" % (
            "the opening range conflicts" if orc else "conflicting candles", len(conflicts),
            "" if len(conflicts) == 1 else "s", first["detail"]))
    rep["ok"] = not reasons
    rep["days"] = parsed[PRIMARY]
    rep["today_bars"] = len(today_p)
    return rep


# ---------------------------------------------------------------- opening-range memory

def or_values(today, n_range):
    rng = today[:n_range]
    return round(max(b["h"] for b in rng), 4), round(min(b["l"] for b in rng), 4)


def check_history(hist, day, sym, n_range, oh, ol, cutoff):
    """-> problem text or None, and records this observation."""
    key = day.isoformat()
    seen = hist.setdefault(key, {}).setdefault(sym, [])
    prev = [x for x in seen if x.get("or_minutes") == n_range * BAR]
    problem = None
    if prev:
        p = prev[-1]
        if abs(p["or_high"] - oh) > tolerance(p["or_high"], oh) or abs(p["or_low"] - ol) > tolerance(p["or_low"], ol):
            problem = ("the opening range changed since the %s scan (high %s -> %s, low %s -> %s): settled bars "
                       "must not change" % (p["at"], _px(p["or_high"]), _px(oh), _px(p["or_low"]), _px(ol)))
    seen.append({"at": cutoff.strftime("%H:%M"), "or_minutes": n_range * BAR, "or_high": oh, "or_low": ol})
    del seen[:-10]
    for k in sorted(hist)[:-5]:
        del hist[k]
    return problem


# ---------------------------------------------------------------- indicators

def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / float(n)
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd(closes, fast=12, slow=26, sig=9):
    """-> (macd list, signal list, histogram list), None where not ready."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    start = next((i for i, x in enumerate(line) if x is not None), None)
    sigl = [None] * len(closes)
    if start is not None:
        s = ema(line[start:], sig)
        sigl[start:] = s
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sigl)]
    return line, sigl, hist


def atr(bars, n=ATR_N):
    """Wilder ATR on bars with h, l, c (oldest first). None when there are not n+1 bars."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / float(n)
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def vwap_series(today):
    out, pv, vv = [], 0.0, 0.0
    for b in today:
        v = b["v"] or 0.0
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * v
        vv += v
        out.append(pv / vv if vv else None)
    return out


def rvol(today, prior):
    """prior = [[bar] of full sessions]. None under MIN_PRIOR_SESSIONS."""
    k = len(today)
    usable = [d for d in prior if len(d) >= k]
    if len(usable) < MIN_PRIOR_SESSIONS or k == 0:
        return None
    base = sum(sum(b["v"] or 0 for b in d[:k]) for d in usable) / float(len(usable))
    if base <= 0:
        return None
    return sum(b["v"] or 0 for b in today) / base


def daily_bar(bars):
    good = [b for b in bars if b["c"] is not None]
    return {"o": good[0]["o"], "h": max(b["h"] for b in good), "l": min(b["l"] for b in good), "c": good[-1]["c"]}


def indicators(days, day):
    """{date: [bar]} (validated) -> context dict for the setups."""
    today = [b for b in days.get(day, []) if b["c"] is not None]
    prior_days = [d for d in sorted(days) if d < day]
    full = [d for d in prior_days if full_session(days[d], d)]
    allbars = [b for d in sorted(days) if d <= day for b in days[d] if b["c"] is not None]
    closes = [b["c"] for b in allbars]
    e9, e20 = ema(closes, 9), ema(closes, 20)
    _, _, hist = macd(closes)
    dailies = [daily_bar(days[d]) for d in full]
    prev_close = None
    if prior_days:
        pb = [b for b in days[prior_days[-1]] if b["c"] is not None]
        prev_close = pb[-1]["c"] if pb else None
    vw = vwap_series(today) if today else []
    vols = [b["v"] or 0 for b in today]
    return {
        "today": today, "vwap": vw, "ema9": e9[-1] if e9 else None, "ema20": e20[-1] if e20 else None,
        "macd_hist": hist[-1] if hist else None, "macd_hist_prev": hist[-2] if len(hist) > 1 else None,
        "rsi": scan.rsi(closes[-200:]) if len(closes) >= 15 else None,
        "atr5": atr(allbars[-200:]), "atr_d": atr(dailies), "rvol": rvol(today, [days[d] for d in full]),
        "prior_sessions": len(full), "prev_close": prev_close,
        "median_vol": _median(vols[:-1]) if len(vols) > 1 else None,
        "dollar_volume": sum(b["c"] * (b["v"] or 0) for b in today),
    }


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


# ---------------------------------------------------------------- setups

def _sig(setup, status, entry, stop, why, trigger=None, notes=None):
    return {"setup": setup, "status": status, "entry": round(entry, 2), "stop": round(stop, 2),
            "trigger": trigger, "why": why, "notes": list(notes or [])}


def setup_orb(ctx, n_range):
    t, vw, a5 = ctx["today"], ctx["vwap"], ctx["atr5"]
    if len(t) <= n_range or a5 is None or vw[-1] is None:
        return None
    oh, ol = max(b["h"] for b in t[:n_range]), min(b["l"] for b in t[:n_range])
    mid = (oh + ol) / 2.0
    last = t[-1]
    k = next((i for i in range(n_range, len(t)) if t[i]["c"] > oh), None)
    if k is None:
        if oh - last["c"] <= 0.5 * a5 and last["c"] > vw[-1]:
            return _sig("orb", "developing", oh + 0.01, mid,
                        "%s under the range high %s; triggers on a 5-minute close above it"
                        % (_px(oh - last["c"]), _px(oh)), trigger="close above %s" % _px(oh))
        return None
    if last["c"] <= oh:
        return None  # broke out and fell back in: a failed breakout, not a setup
    age = len(t) - 1 - k
    med = _median([b["v"] or 0 for b in t[:k]]) or 0
    checks = []
    if age >= FRESH_BARS:
        checks.append("the breakout was %d bars ago (at %s): stale" % (age, scan.clock_label(t[k]["t"] + datetime.timedelta(minutes=BAR))))
    if (t[k]["v"] or 0) < med:
        checks.append("breakout bar volume under the median bar")
    if last["c"] <= vw[-1]:
        checks.append("under VWAP")
    if ctx["rvol"] is not None and ctx["rvol"] < 1.0:
        checks.append("RVOL %.2f under 1" % ctx["rvol"])
    if age >= FRESH_BARS:
        return None if last["c"] > oh + 2 * a5 else _sig(
            "orb", "developing", last["c"], mid, "broke out earlier and still holds the range high", notes=checks)
    stop = mid if mid < last["c"] else ol
    return _sig("orb", "developing" if checks else "confirmed", last["c"], stop,
                "closed above the range high %s at %s" % (_px(oh), scan.clock_label(t[k]["t"] + datetime.timedelta(minutes=BAR))),
                notes=checks)


def setup_momentum(ctx):
    t, vw, a5 = ctx["today"], ctx["vwap"], ctx["atr5"]
    need = (ctx["ema9"], ctx["ema20"], ctx["macd_hist"], ctx["rsi"], a5, vw[-1] if vw else None)
    if len(t) < 7 or any(x is None for x in need):
        return None
    last = t[-1]
    trend = (last["c"] > vw[-1] and ctx["ema9"] > ctx["ema20"] and ctx["macd_hist"] > 0
             and 50 < ctx["rsi"] < 80 and last["c"] > t[0]["o"])
    if not trend:
        return None
    look = t[-7:-1]
    ph = max(b["h"] for b in look)
    stop = min(b["l"] for b in t[-6:]) - 0.01
    if last["c"] > ph:
        checks = []
        if (last["v"] or 0) < (ctx["median_vol"] or 0):
            checks.append("last bar volume under the median bar")
        if ctx["rvol"] is not None and ctx["rvol"] < 1.2:
            checks.append("RVOL %.2f under 1.2" % ctx["rvol"])
        if ctx["macd_hist_prev"] is not None and ctx["macd_hist"] < ctx["macd_hist_prev"]:
            checks.append("MACD histogram falling")
        return _sig("momentum", "developing" if checks else "confirmed", last["c"], stop,
                    "closed above the prior 6 bars' high %s, trend up" % _px(ph), notes=checks)
    if ph - last["c"] <= 0.5 * a5:
        return _sig("momentum", "developing", ph + 0.01, stop, "trend up, %s under the 30-minute high %s"
                    % (_px(ph - last["c"]), _px(ph)), trigger="close above %s" % _px(ph))
    return None


def setup_vwap_reclaim(ctx):
    t, vw, a5 = ctx["today"], ctx["vwap"], ctx["atr5"]
    if len(t) < 8 or a5 is None or vw[-1] is None:
        return None
    last, prev = t[-1], t[-2]
    below = sum(1 for i in range(len(t) - 7, len(t) - 1) if vw[i] is not None and t[i]["c"] < vw[i])
    if below < 2:
        return None
    stop = min(b["l"] for b in t[-3:]) - 0.01
    hist, hp = ctx["macd_hist"], ctx["macd_hist_prev"]
    rising = hist is not None and hp is not None and hist > hp
    if prev["c"] < vw[-2] and last["c"] > vw[-1]:
        checks = []
        if (last["v"] or 0) < (ctx["median_vol"] or 0):
            checks.append("reclaim bar volume under the median bar")
        rng = last["h"] - last["l"]
        if rng > 0 and (last["c"] - last["l"]) / rng < 0.5:
            checks.append("closed in the lower half of the bar")
        if not rising:
            checks.append("MACD histogram not rising")
        return _sig("vwap_reclaim", "developing" if checks else "confirmed", last["c"], stop,
                    "closed back above VWAP %s after %d of 6 bars under it" % (_px(vw[-1]), below), notes=checks)
    if last["c"] < vw[-1] and vw[-1] - last["c"] <= 0.25 * a5 and rising:
        return _sig("vwap_reclaim", "developing", vw[-1] + 0.01, stop,
                    "%s under VWAP %s with the histogram rising" % (_px(vw[-1] - last["c"]), _px(vw[-1])),
                    trigger="close above VWAP (%s now)" % _px(vw[-1]))
    return None


def setup_pullback(ctx):
    t, vw, a5 = ctx["today"], ctx["vwap"], ctx["atr5"]
    if len(t) < 6 or a5 is None or vw[-1] is None or ctx["ema9"] is None or ctx["ema20"] is None:
        return None
    last = t[-1]
    if not (ctx["ema9"] > ctx["ema20"] and last["c"] > vw[-1]):
        return None
    j = max(range(len(t)), key=lambda i: (t[i]["h"], -i))
    if j >= len(t) - 2:
        return None  # the high is the last bar or the one before: no pullback yet
    hod = t[j]["h"]
    if hod - t[0]["o"] < a5:
        return None
    after = t[j + 1:]
    low = min(b["l"] for b in after)
    support = max(ctx["ema20"], vw[-1])
    if hod - low < 0.5 * a5 or low < vw[-1] - 0.25 * a5 or low > support + 0.25 * a5:
        return None
    stop = low - 0.01
    why = "pulled back %s from the high %s to %s, held VWAP" % (_px(hod - low), _px(hod), _px(low))
    if last["c"] > t[-2]["h"] and last["c"] > ctx["ema9"]:
        notes = [] if ctx["rvol"] is None or ctx["rvol"] >= 1.0 else ["RVOL %.2f under 1" % ctx["rvol"]]
        return _sig("pullback", "developing" if notes else "confirmed", last["c"], stop,
                    why + "; the last bar closed over the bar before's high", notes=notes)
    return _sig("pullback", "developing", last["h"] + 0.01, stop, why,
                trigger="close above %s (the last bar's high)" % _px(last["h"]))


# ---------------------------------------------------------------- costs and the plan

def plan_trade(entry, stop, risk_usd, commission, max_order=None, r_mult=2.0):
    """Long trade -> sizing and planned numbers, net of commission per transaction (x2)."""
    risk = round(entry - stop, 4)
    target = round(entry + r_mult * risk, 2)
    out = {"entry": round(entry, 2), "stop": round(stop, 2), "target": target,
           "risk_per_share": round(risk, 2), "commission_per_trade": commission,
           "commissions_round_trip": round(2 * commission, 2)}
    if risk <= 0:
        out.update(shares=0, why_zero="the stop is not under the entry")
        return out
    budget = risk_usd - 2 * commission
    shares = int(math.floor(budget / risk + 1e-9)) if budget > 0 else 0
    if max_order:
        shares = min(shares, int(math.floor(max_order / entry + 1e-9)))
    out["shares"] = max(shares, 0)
    if out["shares"] <= 0:
        out["why_zero"] = ("one share risks %s plus %s in commissions, more than %s" %
                           (_money(risk), _money(2 * commission), _money(risk_usd)))
        return out
    q = out["shares"]
    gl, gr = q * risk, q * (target - entry)
    out.update({
        "gross_loss_at_stop": round(gl, 2), "gross_at_target": round(gr, 2),
        "net_loss_at_stop": round(gl + 2 * commission, 2), "net_at_target": round(gr - 2 * commission, 2),
        "gross_reward_risk": round(r_mult, 2),
        "net_reward_risk": round((gr - 2 * commission) / (gl + 2 * commission), 2),
        "breakeven": round(entry + 2 * commission / q, 2),
        "position_usd": round(q * entry, 2),
    })
    return out


def finalize(sig, ctx, opts, window_ok, window_note):
    """Attach the plan; demote a confirmed signal that fails a hard requirement."""
    p = plan_trade(sig["entry"], sig["stop"], opts["risk_usd"], opts["commission"], opts.get("max_order"))
    sig["plan"] = p
    if sig["status"] != "confirmed":
        return sig
    demote = []
    missing = [k for k in ("vwap", "rvol", "atr5", "rsi", "macd_hist")
               if (ctx["vwap"][-1] if k == "vwap" else ctx[k]) is None]
    if missing:
        demote.append("insufficient data to confirm: no %s" % ", ".join(m.upper().replace("_HIST", "") for m in missing))
    if ctx["atr5"] and sig["entry"] - sig["stop"] < 0.25 * ctx["atr5"]:
        demote.append("stop inside the noise (under a quarter ATR)")
    if not p.get("shares"):
        demote.append(p.get("why_zero", "no shares fit"))
    elif p["net_reward_risk"] < opts["min_rr"] - 1e-9:
        demote.append("net reward:risk %.2f under your minimum %s" % (p["net_reward_risk"], _n(opts["min_rr"])))
    if not window_ok:
        demote.append(window_note)
    if demote:
        sig["status"] = "developing"
        sig["notes"].extend(demote)
    return sig


# ---------------------------------------------------------------- ranking

def load_catalysts(path, now_utc, hours=24):
    """trader-news --json output, {"SYM": [tags]} or [SYM] -> {SYM: {"tags": set, "items": n}}."""
    if not path:
        return {}
    if not os.path.exists(path):
        raise ScanError("no catalysts file at %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    out = {}

    def add(sym, tags):
        r = out.setdefault(str(sym).upper(), {"tags": set(), "items": 0})
        r["tags"].update(str(x).lower() for x in tags or [])
        r["items"] += 1
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        since = now_utc - datetime.timedelta(hours=hours)
        for it in doc["items"]:
            when = it.get("time")
            if when:
                try:
                    w = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                    w = (w - w.utcoffset()).replace(tzinfo=None) if w.utcoffset() is not None else w
                    if w < since or w > now_utc + datetime.timedelta(minutes=10):
                        continue
                except ValueError:
                    pass
            for s in set(it.get("touches") or []) | ({it["ticker"]} if it.get("ticker") else set()):
                add(s, it.get("tags"))
    elif isinstance(doc, dict):
        for s, tags in doc.items():
            add(s, tags if isinstance(tags, list) else [tags])
    elif isinstance(doc, list):
        for s in doc:
            add(s, [])
    return out


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score(ctx, best, cat):
    t = ctx["today"]
    last = t[-1]["c"]
    comp = {}
    m = 0.0
    if ctx["atr_d"] and ctx["prev_close"]:
        m += 12 * _clamp((last / t[0]["o"] - 1) / (ctx["atr_d"] / ctx["prev_close"]))
    h, hp = ctx["macd_hist"], ctx["macd_hist_prev"]
    if h is not None and h > 0:
        m += 8 if hp is not None and h > hp else 5
    r = ctx["rsi"]
    if r is not None:
        m += 6 if 55 <= r <= 75 else (3 if 50 <= r < 55 or 75 < r <= 80 else 0)
    if ctx["vwap"] and ctx["vwap"][-1] and ctx["atr5"]:
        m += 4 * _clamp((last - ctx["vwap"][-1]) / ctx["atr5"])
    comp["momentum"] = round(m, 1)
    dv = ctx["dollar_volume"]
    liq = 15 * _clamp(math.log10(dv / 5e6) / 2) if dv > 0 else 0.0
    if ctx["rvol"] is not None:
        liq += 10 * _clamp((ctx["rvol"] - 1) / 2)
    comp["liquidity"] = round(liq, 1)
    c = 0.0
    cat_note = None
    if cat:
        c = 20 if cat["tags"] & HIGH_IMPACT else (12 if cat["tags"] else 6)
        cat_note = "news: " + (", ".join(sorted(cat["tags"])) if cat["tags"] else "%d headline(s)" % cat["items"])
    elif ctx["prev_close"]:
        gap = (t[0]["o"] / ctx["prev_close"] - 1) * 100
        if abs(gap) >= 2:
            c = 6
            cat_note = "gap %+.1f%% (no news file read)" % gap
    comp["catalyst"] = c
    rr = (best or {}).get("plan", {}).get("net_reward_risk")
    comp["reward_risk"] = round(25 * _clamp((rr - 1) / 2), 1) if rr is not None else 0.0
    total = round(sum(comp.values()), 1)
    return total, comp, cat_note


# ---------------------------------------------------------------- fetching

def fetch_host(host, sym, rng):
    """One v8 chart from one named host -> (series, fetched_at UTC). One retry on 429/5xx."""
    url = HOSTS[host] + scan.chart_path(sym, rng, "%dm" % BAR)
    last = None
    for attempt in (0, 1):
        try:
            text = scan.http_get(url)
            got = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            return scan.parse_chart(text, sym), got
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            e.close()
            if e.code == 429 or e.code >= 500:
                if attempt == 0:
                    scan.SLEEP(2.0)
                    continue
            break
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            if attempt == 0:
                scan.SLEEP(1.0)
                continue
        except ScanError as e:
            last = str(e)
            break
    raise DataError("%s: %s" % (host, last))


def load_bundle(path):
    """A two-host bundle: {"fetched_at": ISO UTC, "hosts": {"query1": {SYM: v8}, "query2": {...}}}."""
    if not os.path.exists(path):
        raise ScanError("no offline bundle at %s" % path)
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
        else:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(doc, dict) or not isinstance(doc.get("hosts"), dict):
        raise ScanError("%s is not a two-host bundle ({\"fetched_at\", \"hosts\": {\"query1\", \"query2\"}})" % path)
    fetched = scan.parse_now(doc["fetched_at"]) if doc.get("fetched_at") else None
    data = {}
    for host in HOSTS:
        for key, name in (("hosts", host), ("hosts_1d", host + "/1d")):
            for sym, chart in ((doc.get(key) or {}).get(host) or {}).items():
                try:
                    data.setdefault(sym.upper().replace("-", "."), {})[name] = scan.parse_chart(chart, sym)
                except ScanError:
                    data.setdefault(sym.upper().replace("-", "."), {})[name] = None
    return data, fetched


# ---------------------------------------------------------------- the run

def analyze(data, symbols, day, cutoff, n_range, fetched, live, opts, hist=None, catalysts=None):
    """Pure (no network). data = {SYM: {"query1": series, "query2": series}}.
    -> (rows, summary). `hist` (dict) is updated in place when given."""
    rows = []
    window_ok, window_note = True, ""
    nb, na = opts.get("no_entries_before"), opts.get("no_entries_after")
    if nb and cutoff.time() < nb:
        window_ok, window_note = False, "before your first entry time %s" % nb.strftime("%H:%M")
    if na and cutoff.time() > na:
        window_ok, window_note = False, "after your last entry time %s" % na.strftime("%H:%M")
    for sym in symbols:
        got = data.get(sym) or {}
        row = {"symbol": sym, "status": "NO TRADE", "data_ok": False}
        extra = {k: got.get(k) for k in EXTRA} if any(k in got for k in EXTRA) else None
        rep = validate(sym, got.get("query1"), got.get("query2"), day, cutoff, n_range,
                       {h: (got.get("_fetched") or {}).get(h, fetched) for h in HOSTS}, live,
                       opts.get("max_age", MAX_AGE), opts.get("max_skew", MAX_SKEW), extra)
        if got.get("_errors"):
            rep["reasons"][:0] = got["_errors"]
            rep["ok"] = False
        if got.get("_refetched"):
            rep["warnings"].append("the hosts disagreed on the first fetch and agreed on the re-fetch")
        row["latency"] = rep["latency"]
        row["warnings"] = rep["warnings"]
        if rep["ok"]:
            today = [b for b in rep["days"].get(day, []) if b["c"] is not None]
            oh, ol = or_values(today, n_range)
            row["or_high"], row["or_low"] = oh, ol
            if hist is not None:
                prob = check_history(hist, day, sym, n_range, oh, ol, cutoff)
                if prob:
                    rep["reasons"].append(prob)
                    rep["ok"] = False
        row["data_reasons"] = rep["reasons"]
        if not rep["ok"]:
            row["reason"] = "data: " + rep["reasons"][0]
            rows.append(row)
            continue
        row["data_ok"] = True
        ctx = indicators(rep["days"], day)
        last = ctx["today"][-1]
        row.update({
            "price": round(last["c"], 2), "bar_close_et": (last["t"] + datetime.timedelta(minutes=BAR)).strftime("%H:%M"),
            "vwap": _r(ctx["vwap"][-1], 2), "rvol": _r(ctx["rvol"], 2), "atr_5m": _r(ctx["atr5"], 3),
            "atr_daily": _r(ctx["atr_d"], 2), "rsi": _r(ctx["rsi"], 1), "macd_hist": _r(ctx["macd_hist"], 4),
            "ema9": _r(ctx["ema9"], 2), "ema20": _r(ctx["ema20"], 2), "prev_close": _r(ctx["prev_close"], 2),
            "change_pct": _r((last["c"] / ctx["prev_close"] - 1) * 100, 2) if ctx["prev_close"] else None,
            "dollar_volume": int(ctx["dollar_volume"]), "prior_sessions": ctx["prior_sessions"],
        })
        sigs = []
        for fn in (lambda: setup_orb(ctx, n_range), lambda: setup_momentum(ctx),
                   lambda: setup_vwap_reclaim(ctx), lambda: setup_pullback(ctx)):
            s = fn()
            if s:
                sigs.append(finalize(s, ctx, opts, window_ok, window_note))
        rank = {"confirmed": 0, "developing": 1}
        sigs.sort(key=lambda s: (rank[s["status"]], -(s["plan"].get("net_reward_risk") or -9)))
        row["signals"] = sigs
        best = sigs[0] if sigs else None
        row["score"], row["score_parts"], row["catalyst"] = score(ctx, best, (catalysts or {}).get(sym))
        row["status"] = best["status"].upper() if best else "NO SETUP"
        rows.append(row)
    checked = len(rows)
    rejected = sum(1 for r in rows if not r["data_ok"])
    feed_bad = checked > 0 and rejected / float(checked) > REJECT_SHARE
    if feed_bad:
        for r in rows:
            for s in r.get("signals", []):
                if s["status"] == "confirmed":
                    s["status"] = "developing"
                    s["notes"].append("feed problem: most symbols failed the data checks")
            if r["status"] == "CONFIRMED":
                r["status"] = "DEVELOPING"
    order = {"CONFIRMED": 0, "DEVELOPING": 1, "NO SETUP": 2, "NO TRADE": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r.get("score") or 0), r["symbol"]))
    ages = [r["latency"].get("age_s") for r in rows if r.get("latency") and r["latency"].get("age_s") is not None]
    summary = {"symbols": checked, "data_rejected": rejected, "feed_problem": feed_bad,
               "confirmed": sum(1 for r in rows if r["status"] == "CONFIRMED"),
               "developing": sum(1 for r in rows if r["status"] == "DEVELOPING"),
               "max_age_s": max(ages) if ages else None, "median_age_s": _median(ages) if ages else None,
               "entry_window_ok": window_ok}
    return rows, summary


def fetch_all(symbols, rng, pause, a=None):
    """Both hosts for every symbol, one re-fetch on a conflict. -> data dict."""
    data = {}
    for i, sym in enumerate(symbols):
        if i and pause:
            scan.SLEEP(pause)
        data[sym] = _fetch_pair(sym, rng)
    return data


def _fetch_pair(sym, rng):
    got = {"_fetched": {}, "_errors": []}
    for host in ("query1", "query2"):
        try:
            got[host], got["_fetched"][host] = fetch_host(host, sym, rng)
        except DataError as e:
            got["_errors"].append(str(e))
    if rng != "1d":
        for host in ("query1", "query2"):
            try:
                got[host + "/1d"] = fetch_host(host, sym, "1d")[0]
            except DataError as e:
                got[host + "/1d"] = None
                got["_errors"].append("%s range=1d: %s" % (host, e))
    return got


def _r(x, n):
    return None if x is None else round(x, n)


def _px(x):
    return "n/a" if x is None else "{:,.2f}".format(x)


def _money(x):
    return ("-$" if x < 0 else "$") + "{:,.2f}".format(abs(x))


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _age(s):
    if s is None:
        return "n/a"
    return "%ds" % s if s < 120 else "%dm%02ds" % (s // 60, s % 60)


# ---------------------------------------------------------------- report

def render(doc, show):
    s = doc["summary"]
    L = []
    L.append("Intraday analysis, %s, cutoff %s ET, %d-minute range, 5-minute bars, Yahoo query1 + query2 cross-checked."
             % (doc["day"], doc["cutoff"], doc["or_minutes"]))
    L.append("Data: %d symbols, %d rejected by the data checks. Last-trade age at fetch: median %s, worst %s%s."
             % (s["symbols"], s["data_rejected"], _age(s["median_age_s"]), _age(s["max_age_s"]),
                "" if doc["live"] else " (replay: age not enforced)"))
    L.append("Costs: %s a transaction, %s a round trip; sized to %s max loss including commissions."
             % (_money(doc["costs"]["commission_per_trade"]), _money(2 * doc["costs"]["commission_per_trade"]),
                _money(doc["costs"]["risk_usd"])))
    if s["feed_problem"]:
        L.append("FEED PROBLEM: more than half the symbols failed the data checks. No trade recommendations.")
    if not s["entry_window_ok"]:
        L.append("Outside your entry window: nothing is confirmed.")
    L.append("")
    for r in doc["symbols"][:show]:
        if not r["data_ok"]:
            L.append("%-6s NO TRADE  %s" % (r["symbol"], r["reason"]))
            continue
        L.append("%-6s %-10s score %5.1f  $%s  %+.2f%%  VWAP %s  RVOL %s  RSI %s  ATR5 %s  MACDh %s"
                 % (r["symbol"], r["status"], r["score"], _px(r["price"]), r["change_pct"] or 0.0, _px(r["vwap"]),
                    "n/a" if r["rvol"] is None else "%.2f" % r["rvol"], "n/a" if r["rsi"] is None else "%.1f" % r["rsi"],
                    "n/a" if r["atr_5m"] is None else "%.2f" % r["atr_5m"],
                    "n/a" if r["macd_hist"] is None else "%+.3f" % r["macd_hist"]))
        L.append("       data: last trade %s (age %s), hosts %ss apart; bar to %s; parts %s%s"
                 % (r["latency"]["query2"].get("last_trade_et", "n/a")[11:], _age(r["latency"].get("age_s")),
                    r["latency"].get("host_skew_s", "?"), r["bar_close_et"],
                    " ".join("%s %s" % (k, v) for k, v in r["score_parts"].items()),
                    ("; " + r["catalyst"]) if r["catalyst"] else ""))
        for sg in r["signals"]:
            p = sg["plan"]
            L.append("       %-10s %-22s %s" % (sg["status"].upper(), SETUP_WORDS[sg["setup"]], sg["why"]))
            if p.get("shares"):
                L.append("         entry %s stop %s target %s (2R): %d sh, net at stop -%s, net at target +%s, "
                         "net R:R %.2f, breakeven %s"
                         % (_px(p["entry"]), _px(p["stop"]), _px(p["target"]), p["shares"],
                            _money(p["net_loss_at_stop"]), _money(p["net_at_target"]), p["net_reward_risk"],
                            _px(p["breakeven"])))
            else:
                L.append("         entry %s stop %s: %s" % (_px(p["entry"]), _px(p["stop"]), p.get("why_zero")))
            if sg["trigger"]:
                L.append("         trigger: %s" % sg["trigger"])
            for n in sg["notes"]:
                L.append("         - %s" % n)
        for w in r["warnings"]:
            L.append("       warning: %s" % w)
    if len(doc["symbols"]) > show:
        L.append("... and %d more (--show N)." % (len(doc["symbols"]) - show))
    L.append("")
    L.append(NOTE + " Re-run before acting: a signal is stale once the price moves on.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Intraday Analyzer (trader-scanner): data-checked setups, analysis only.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="comma-separated tickers")
    g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
    ap.add_argument("--offline", metavar="FILE", help="a two-host bundle (.json or .json.gz) instead of the network")
    ap.add_argument("--now", help='pretend it is this time ("2026-11-25 12:28" New York, or ...Z)')
    ap.add_argument("--at", help="cut the clock at this New York time, HH:MM (a replay: age is not enforced)")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--range", default="1mo", help="Yahoo history for RVOL / ATR (1mo; 5d is too short for RVOL)")
    ap.add_argument("--max-age", type=int, default=MAX_AGE, help="live: reject a last trade older than this (s)")
    ap.add_argument("--commission", type=float, help="$ per transaction (default 1.00 or config)")
    ap.add_argument("--risk-usd", type=float, help="max loss per trade incl. commissions (default config or 50)")
    ap.add_argument("--catalysts", metavar="FILE", help="trader-news --json output, or {\"SYM\": [tags]}")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--state", default="trader/state/or_history.json", help="opening ranges seen today")
    ap.add_argument("--no-history", action="store_true", help="do not compare with or record earlier scans")
    ap.add_argument("--out", default="trader/intraday.json")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--pause", type=float, default=scan.PAUSE)
    ap.add_argument("--show", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except ScanError as e:
        print("intraday.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("intraday.py: stopped", file=sys.stderr)
        return 1


def run(a):
    cfg = scan.read_config(a.config)
    at = scan.parse_hhmm(a.at) if a.at else None
    until = scan.parse_hhmm(cfg.get("no_entries_after") or "15:30", "no_entries_after")
    before = scan.parse_hhmm(cfg.get("no_entries_before") or "09:45", "no_entries_before")
    data, fetched = None, None
    if a.offline:
        data, fetched = load_bundle(a.offline)
        now = scan.parse_now(a.now) if a.now else fetched
        if now is None:
            raise ScanError("the bundle has no fetched_at: pass --now")
    else:
        now = scan.parse_now(a.now)
        if a.now:
            raise ScanError("--now needs --offline: live data is always as of now")
    if a.symbols:
        symbols = [s for s in a.symbols.upper().replace(" ", ",").split(",") if s]
    elif a.watchlist:
        symbols = scan.symbols_from_file(a.watchlist)
    elif data is not None:
        symbols = sorted(data)
    else:
        raise ScanError("give --symbols or --watchlist (this checks each chart twice; keep the list short)")
    symbols = list(dict.fromkeys(symbols))
    day, cutoff = scan.orb_window(now, at, a.or_minutes, None)
    live = at is None
    if data is None:
        data = fetch_all(symbols, a.range, a.pause)
        for sym in symbols:
            got = data[sym]
            if got.get("query1") and got.get("query2"):
                rep = validate(sym, got["query1"], got["query2"], day, cutoff, a.or_minutes // BAR,
                               got["_fetched"], live, a.max_age,
                               extra={k: got.get(k) for k in EXTRA if k in got} or None)
                if rep["conflicts"]:
                    scan.SLEEP(2.0)
                    again = _fetch_pair(sym, a.range)
                    again["_refetched"] = True
                    data[sym] = again
    opts = {
        "commission": a.commission if a.commission is not None else float(cfg.get("commission_per_trade_usd", COMMISSION)),
        "risk_usd": a.risk_usd if a.risk_usd is not None else float(cfg.get("max_loss_per_trade_usd") or RISK_USD),
        "max_order": cfg.get("max_order_usd"), "min_rr": float(cfg.get("min_reward_risk") or MIN_RR),
        "no_entries_before": before, "no_entries_after": until, "max_age": a.max_age,
    }
    hist = None
    if not a.no_history:
        hist = {}
        if os.path.exists(a.state):
            try:
                with open(a.state, encoding="utf-8") as fh:
                    hist = json.load(fh)
            except ValueError:
                hist = {}
    cats = load_catalysts(a.catalysts, now)
    rows, summary = analyze(data, symbols, day, cutoff, a.or_minutes // BAR, fetched, live, opts, hist, cats)
    if hist is not None and not a.no_write:
        scan.write_json(a.state, hist)
    doc = {"created_by": "trader-scanner intraday", "mode": "analysis-only", "orders": "none",
           "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "day": day.isoformat(),
           "cutoff": cutoff.strftime("%H:%M"), "live": live, "or_minutes": a.or_minutes, "source": "yahoo query1+query2",
           "costs": {"commission_per_trade": opts["commission"], "risk_usd": opts["risk_usd"]},
           "summary": summary, "symbols": rows, "note": NOTE,
           "hits": [{"symbol": r["symbol"], "setups": [s["setup"] for s in r["signals"]], "status": r["status"]}
                    for r in rows if r["status"] in ("CONFIRMED", "DEVELOPING")]}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline)
    if not a.no_write:
        scan.write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(render(doc, a.show))
    return 0 if doc["hits"] else 1


if __name__ == "__main__":
    sys.exit(main())
