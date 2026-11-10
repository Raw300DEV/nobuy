"""Backtest buy rules for the LEASH paper agent on every SOL-paired pump.fun launch our collector recorded.

    python -X utf8 products/leash/backtest.py

For each coin: decide at +D seconds using only what was known by then, enter on the first trade after D+LAT
(latency), exit on take-profit / stop-loss / dev sell / time stop (inside the 600 s the collector records).
Returns include pump.fun fees both ways (FEE each side). Market cap on the curve is proportional to price, so
return = exit_mcap / entry_mcap.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
import sys
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DB = os.path.join(ROOT, 'backend', 'launchdata', 'launches.db')
WSOL = 'So11111111111111111111111111111111111111112'
FEE = 0.0125     # pump.fun protocol + creator fee per side, roughly
LAT = 1.5        # seconds from decision to fill


def load(limit=None):
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    toks = c.execute(f"""select t.mint, t.ts, t.creator, t.payer, t.dev_sol, coalesce(t.mayhem,0),
                          f.n_sigs_before, f.capped, f.prior_launches_hist, f.prior_launches_live, f.funder_hot, f.lag_s
                         from tokens t left join dev_features f on f.mint=t.mint
                         where t.pool='pump2' and (t.quote_mint is null or t.quote_mint=?) and t.ts < ?
                         order by t.ts {'limit %d' % limit if limit else ''}""", (WSOL, __import__('time').time() - 700)).fetchall()
    out = []
    for mint, ts, creator, payer, dev_sol, mayhem, nsig, capped, ph, pl, fhot, lag in toks:
        tr = c.execute("select ts, trader, side, sol, mcap_sol from trades where mint=? and ts<=? and mcap_sol is not null order by ts",
                       (mint, ts + 600)).fetchall()
        if len(tr) < 2:
            continue
        out.append(dict(mint=mint, t0=ts, creator=creator, devs={creator, payer}, dev_sol=dev_sol or 0, mayhem=mayhem,
                        fresh=(nsig is not None and nsig <= 5 and not capped), prior=(ph or 0) + (pl or 0) if nsig is not None else None,
                        fhot=fhot, lag=lag, tr=[(t - ts, w, s, sol or 0, mc) for t, w, s, sol, mc in tr]))
    return out


def feats(c, D):
    """What the agent knows at +D seconds."""
    early = [x for x in c['tr'] if x[0] <= D]
    buyers = {w for t, w, s, sol, mc in early if s == 'buy' and w not in c['devs']}
    sn = {w for t, w, s, sol, mc in early if s == 'buy' and w not in c['devs'] and t <= 3}
    dev_sold = any(s == 'sell' and w in c['devs'] for t, w, s, sol, mc in early)
    buy_sol = sum(sol for t, w, s, sol, mc in early if s == 'buy' and w not in c['devs'])
    sell_sol = sum(sol for t, w, s, sol, mc in early if s == 'sell' and w not in c['devs'])
    mc_now = early[-1][4] if early else None
    return dict(buyers=len(buyers), snipers=len(sn), dev_sold=dev_sold, net=buy_sol - sell_sol, buy_sol=buy_sol, mc=mc_now)


def trade(c, D, tp, sl, hold):
    tr = c['tr']
    entry = next((x for x in tr if x[0] >= D + LAT), None)
    if not entry:
        return None
    e_t, e_mc = entry[0], entry[4]
    for t, w, s, sol, mc in tr:
        if t <= e_t:
            continue
        r = mc / e_mc
        if r >= tp or r <= sl or (s == 'sell' and w in c['devs']) or t - e_t >= hold:
            return (r * (1 - FEE) * (1 - FEE)) - 1
    last = tr[-1][4] / e_mc   # no trade after entry inside the window: exit at the last print
    return (last * (1 - FEE) ** 2) - 1


RULES = {
    'everything': lambda f, c: True,
    'fresh dev': lambda f, c: c['fresh'],
    'first-time dev': lambda f, c: c['prior'] == 0,
    'repeat dev (10+)': lambda f, c: (c['prior'] or 0) >= 10,
    'dev buy >= 1.5': lambda f, c: c['dev_sol'] >= 1.5,
    'dev buy >= 5': lambda f, c: c['dev_sol'] >= 5,
    '5+ buyers, dev held': lambda f, c: f['buyers'] >= 5 and not f['dev_sold'],
    '10+ buyers, dev held': lambda f, c: f['buyers'] >= 10 and not f['dev_sold'],
    'net inflow >= 3 SOL, dev held': lambda f, c: f['net'] >= 3 and not f['dev_sold'],
    'first-time + 5 buyers + dev held': lambda f, c: c['prior'] == 0 and f['buyers'] >= 5 and not f['dev_sold'],
    'first-time + dev buy>=1.5 + dev held': lambda f, c: c['prior'] == 0 and c['dev_sol'] >= 1.5 and not f['dev_sold'],
    'fresh + net>=3 + dev held': lambda f, c: c['fresh'] and f['net'] >= 3 and not f['dev_sold'],
}


def run(coins, D=10, tp=1.5, sl=0.75, hold=180):
    res = defaultdict(list)
    for c in coins:
        f = feats(c, D)
        r = None
        for name, rule in RULES.items():
            try:
                ok = rule(f, c)
            except TypeError:
                ok = False
            if ok:
                if r is None:
                    r = trade(c, D, tp, sl, hold)
                if r is not None:
                    res[name].append(r)
    return res


def show(res, title):
    print(f'\n{title}')
    print(f"  {'rule':38} {'n':>6} {'win%':>6} {'avg':>8} {'median':>8} {'total per 1 SOL each':>22}")
    for name in RULES:
        v = res.get(name, [])
        if len(v) < 20:
            print(f'  {name:38} {len(v):>6}   (too few)')
            continue
        print(f"  {name:38} {len(v):>6} {100 * sum(x > 0 for x in v) / len(v):>5.0f}% {100 * statistics.mean(v):>7.1f}% {100 * statistics.median(v):>7.1f}% {sum(v):>21.1f}")


if __name__ == '__main__':
    coins = load()
    print(f'{len(coins):,} SOL-paired launches with trades')
    half = len(coins) // 2
    for D, tp, sl, hold in [(10, 1.5, 0.75, 180), (20, 1.5, 0.75, 180), (10, 2.0, 0.6, 300), (30, 1.3, 0.85, 120)]:
        show(run(coins, D, tp, sl, hold), f'decide +{D}s, take profit {tp}x, stop {sl}x, time stop {hold}s')
    # out-of-sample check on the best-looking setting
    if '--split' in sys.argv:
        show(run(coins[:half]), 'FIRST HALF (in-sample)')
        show(run(coins[half:]), 'SECOND HALF (out-of-sample)')
