"""The paid data the agent buys over x402: a dev check for one pump.fun coin.

GET /x402/devcheck/{mint}   price DEVCHECK_USD (USDC, x402 exact on Solana)
GET /x402/price             free: what each endpoint costs + where payments go

Answer = the dev wallet's record (prior launches, launches in the last 24h, how its last coins went, how often it
sold within 60 s, wallet freshness and funder) + the coin's first-seconds facts + a plain verdict.
Data: our launch collector DB (read-only) via products/launchreport/report.py.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'products', 'launchreport'))
sys.path.insert(0, os.path.join(HERE, 'launchreport'))
import report  # noqa: E402

DEVCHECK_USD = float(os.environ.get('LEASH_DEVCHECK_USD', '0.005'))


def devcheck(mint: str) -> dict:
    c = report.db()
    try:
        t = c.execute('select mint, ts, creator, payer, symbol, name, dev_sol, coalesce(mayhem,0) mayhem from tokens where mint=?',
                      (mint,)).fetchone()
        if not t:
            return {'mint': mint, 'found': False, 'verdict': 'UNKNOWN', 'why': ['Coin not in our launch data (we record new pump.fun launches live).']}
        tr = c.execute('select ts, trader, side, sol from trades where mint=? order by ts limit 4000', (mint,)).fetchall()
    finally:
        c.close()
    devs = {t['creator'], t['payer']}
    dev_sells = [x['ts'] - t['ts'] for x in tr if x['side'] == 'sell' and x['trader'] in devs]
    buyers = {x['trader'] for x in tr if x['side'] == 'buy' and x['trader'] not in devs}
    early = {x['trader'] for x in tr if x['side'] == 'buy' and x['trader'] not in devs and x['ts'] - t['ts'] <= 3}
    h = report.dev_history(mint, t['creator'], t['ts'])
    ps = h.get('past_summary') or {}
    why, verdict = [], 'CLEAR'
    if dev_sells:
        why.append(f'Dev already sold, first sell at +{dev_sells[0]:.0f}s.'); verdict = 'AVOID'
    if (h.get('prior_launches') or 0) >= 100:
        why.append(f"Factory dev: {h['prior_launches']:,} coins launched before this one."); verdict = 'AVOID'
    elif h.get('prior_launches'):
        why.append(f"Repeat dev: {h['prior_launches']} prior launches ({h.get('launches_24h_before') or 0} in the last 24h)."); verdict = 'CAUTION' if verdict == 'CLEAR' else verdict
    if ps.get('n') and ps.get('dev_sold_within_60s', 0) * 2 >= ps['n']:
        why.append(f"On {ps['dev_sold_within_60s']} of their last {ps['n']} coins the dev sold within 60s."); verdict = 'AVOID'
    if t['mayhem']:
        why.append('Mayhem-mode coin (different curve).'); verdict = 'CAUTION' if verdict == 'CLEAR' else verdict
    if not why:
        why.append('First-time dev wallet, no dev sell seen.' if not h.get('prior_launches') else 'No red flags in the dev record.')
    return {
        'mint': mint, 'found': True, 'symbol': t['symbol'], 'name': t['name'], 'age_s': round(time.time() - t['ts']),
        'verdict': verdict, 'why': why,
        'coin': {'buyers': len(buyers), 'buyers_first_3s': len(early), 'dev_buy_sol': round(t['dev_sol'] or 0, 3),
                 'dev_sold': bool(dev_sells), 'dev_first_sell_s': round(dev_sells[0], 1) if dev_sells else None},
        'dev': {k: h.get(k) for k in ('creator', 'prior_launches', 'launches_24h_before', 'past_summary', 'wallet', 'base_rate')},
        'source': 'onleash launch intel', 'checked_at': round(time.time()),
    }
