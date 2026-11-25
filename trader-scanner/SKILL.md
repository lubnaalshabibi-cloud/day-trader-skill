---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of US-listed common
  stocks, ordinary shares and ADRs from Nasdaq Trader's symbol directories,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars, then writes
  trader/watchlist.json for trader-news and trader-entry. The intraday
  analyzer cross-checks both Yahoo hosts, rejects stale, incomplete or
  conflicting bars, shows the data's age, computes VWAP, relative volume,
  ATR, RSI and MACD, finds opening-range breakouts, momentum continuation,
  VWAP reclaims and pullbacks (confirmed or developing), ranks them, and
  prints the plan net of $1 a transaction. Analysis only. Free Yahoo data,
  bar by bar. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
5,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols TSLA,NVDA,AMD  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:50 AM ET -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM ET
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
python3 "${CLAUDE_SKILL_DIR}/intraday.py" --symbols NVDA,AMD,TSLA     # data-checked setups, analysis only
python3 "${CLAUDE_SKILL_DIR}/intraday.py" --watchlist trader/watchlist.json --catalysts news.json
```

Flags: `--min-price 5` and `--min-volume` (daily 1,000,000, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (New York time) to pin the clock, `--offline PATH`
to read saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:30) |

## The universe

`universe` downloads `nasdaqlisted.txt` and `otherlisted.txt` from
nasdaqtrader.com/dynamic/SymDir and keeps a row when, in this order:

1. It is not a test issue, not an ETF, not NextShares.
2. It is a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one class
   letter after a dot (BRK.B). A `$` preferred (ABR$D) is out, and so is a
   `.U`, `.W` or `.R` suffix (units, warrants, rights).
3. Out, whatever else the name says: warrants; SPAC units and rights ("Units,
   each consisting of", "Rights to receive"); notes, ETNs, debentures; a %
   coupon; "Depositary Shares" that are not American or Global (those carry a
   preferred).
4. In: the name calls it common stock, common shares or units, ordinary
   shares, shares of beneficial interest, ADS / ADR / American or Global
   depositary shares, or partnership or trust units.
5. Out: the name still says unit, right, bond, preferred or preference.
6. In: everything else (plain names like "AMETEK, Inc.").

On the Oct 19 2026 files this keeps 5,943. The reel said 5,259: that count
came from an ad hoc filter (names that say common or ordinary, no ADRs). All
5,259 of those are in this list; the 684 extra are mostly ADRs, closed-end
funds and partnership units. The Oct 20 files gave 5,942.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so 5,942
symbols take 298 requests. On Oct 20 2026 that took 145 seconds with the
polite pause (0.3 s). Liquidity comes from the spark meta: last price at
least `--min-price` and last session's volume at least `--min-volume`. Run it
before the open or after the close: during the session the volume is the
day's so far, and today's daily bar is left out until 4:00 PM.

The setups are computed from daily closes only (spark has no highs or lows),
so they are close cousins of the reel's `refs/scan.py`, which used highs and
lows:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one (KO on
Oct 16 broke its 20-day closing high, not its 20-day intraday high). Hits are
listed most traded first (price times volume).

## The intraday analyzer (during the session, analysis only)

`intraday.py` is for a short list (the ORB hits, the daily watchlist, the
names you trade): it fetches a month of 5-minute bars for every symbol from
BOTH query1 and query2, plus today's bars from both (four requests a symbol;
20 symbols take about a minute).

Data checks. Any failure is `NO TRADE  data: <reason>` for that symbol:

- **Cross-check.** Every regular-session bar must agree on both hosts: open,
  high, low and close within max(1 cent, 0.05 %), volume within 5 % (the
  newest 3 bars of today are skipped for volume). A bar one host lacks is a
  conflict. Live, a conflict is re-fetched once; still conflicting, rejected.
  Today's bars are also fetched with `range=1d` from both hosts and must
  match: Yahoo caches per URL, and on Nov 25 2026 query2 served WMT a
  different 9:30 bar for `range=1mo` than for `range=1d`. Four requests a
  symbol.
  This is not theoretical: at 12:29 PM ET on Nov 25 2026 the two hosts
  served different 9:30 bars for NVDA (high 145.00 vs 143.62) and META, and
  a minute later both served the 145.00 version. Which one was right is not
  knowable from Yahoo alone, so neither is used while they disagree.
- **Candles.** High under the open or close, low over them, a price at or
  under zero, negative volume, a bar with some prices missing, two different
  bars at one time.
- **Complete.** Every bar from 9:30 to the cutoff today.
- **Opening range memory.** The range high and low are kept per day in
  `trader/state/or_history.json`; a later scan that sees a different range is
  rejected (settled bars must not change).
- **Timestamps.** 5m bars, the right symbol, New York's UTC offset, a last
  trade not in the future, live at most `--max-age` (300) seconds old, the
  two hosts within 120 s of each other.
- **Feed problem.** More than half the symbols rejected: nothing is confirmed.

Latency: each symbol shows its last trade time and its age at fetch, and the
header the median and the worst. That age is a lower bound on the delay.

Indicators (None when there is not enough data): session VWAP, RVOL (volume
through the last bar over the average through the same bar on at least 5
full prior sessions), ATR 14 on 5-minute and on daily bars, RSI 14, MACD
12/26/9, EMA 9 and 20.

Setups, each CONFIRMED or DEVELOPING (rules in the script's header):
opening-range breakout, momentum continuation, VWAP reclaim, pullback in an
uptrend. Confirmed also needs every indicator present, a stop at least a
quarter ATR away, at least one share inside the max loss after commissions,
net reward:risk at or over `min_reward_risk`, and the time inside the entry
window. Anything short of that is DEVELOPING with the reason listed.

Costs: `commission_per_trade_usd` (default $1.00) on the buy and on the sell.
Each signal prints shares, net at the stop, net at the target (2R), net
reward:risk and the breakeven price. Planned numbers: a stop can fill worse
and the spread is not in the bars.

Ranking, 0 to 100: momentum 30, liquidity 25 (dollars traded, RVOL),
catalysts 20 (`--catalysts`, trader-news `--json` output; without news a gap
of 2 % or more counts 6 and says so), net reward:risk 25. Confirmed first.

Writes `trader/intraday.json`; its `hits[]` can feed `--watchlist` of
trader-entry. Exit 0 when something is confirmed or developing.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:30,
9:35 and 9:40 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all. Each stage 2 chart is fetched from
   BOTH query1 and query2 and the bars must agree (re-fetched once); a chart
   the hosts disagree on is not used and is listed in `host_conflicts`.
   `--single-source` turns this off. Live, the scan stops when Yahoo's newest
   trade is older than `--max-age` (600) seconds, and prints the data's age.

Timing, live on Tue Oct 20 2026 (a strong morning): 100 liquid names at
10:01 AM took 35 seconds (5 spark requests, 70 v8 charts, 51 of 91 usable
charts broke out). The whole universe at 10:03 AM took about 4 minutes (298
spark requests, then 200 v8 charts): 2,448 usable charts, 1,782 candidates,
the 200 most traded checked, 119 of those broke out. For a fast answer, scan
the daily watchlist or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:30 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:50 AM, on a
weekend or holiday, after the close, or after `no_entries_after` (an expired
setup) it prints one plain line and writes nothing. The 1:00 PM early closes
are in the calendar.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 4,980 of 5,259 charts (279
  failed or had no data)". Never round a partial scan up to "the whole market".
- NYSE holidays and early closes for 2026 and 2027 (nyse.com, read 2026-10-13).

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:50 AM ET (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news TICKER` (what is moving
   it), then `/trader-entry TICKER` (the entry bar by bar). Never pick for them.

## Example: the reel

Monday Oct 19 2026, scan at 10:15 AM over the reel's 234-ticker wall
(`tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:15" --min-price 0
Fetched 233 of 234 charts (1 failed or had no data); 231 usable; 95 candidates, 95 confirmed with real highs and lows.
42 of 231 charts closed above their opening-range high by 10:15 AM:
   9:50 AM  AMC         $2.97  range 2.90 to 2.96  +0.50% over
  ...
  10:15 AM  TSLA      $364.18  range 354.47 to 362.18  +0.55% over
```

The reel's wall had no price floor (AMC at $2.97 is one of the 42), hence
`--min-price 0`; with the default $5 floor it is 41. TSLA's range is the 9:30
to 9:40 bars, high 362.18, low 354.47; the 10:10 bar closed at 364.18, 0.55 %
over the high, at 10:15 AM. The user picks TSLA and Claude hands it to
`/trader-news TSLA`, then `/trader-entry TSLA`. In the reel the universe was
5,259 US stocks; this tool's rule gives 5,943 on the same files.

The pre-market scan that morning (closes through Fri Oct 16, the reel's daily
charts) tags BLK, CI, XOM and HD a 20-day breakout, CCL a bull flag, F, JPM
and GE a reclaim of the 20-day average, and BAC, MS, RIVN and PLUG oversold,
as the reel did. On closes it also tags KO (breakout), RCL (flag) and PNC (an
oversold bounce).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a realized P&L or a return. intraday.py prints PLANNED net
  amounts at the stop and the target; call them planned, never results.
- NO TRADE on data is final for that scan: never work around it by reading
  one host only.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
