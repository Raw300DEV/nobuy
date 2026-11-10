"""The LEASH demo agent: watches every new pump.fun coin from our collector, decides at +10 s, pays (on paper) for
a data check over x402, asks the leash before every buy, and paper-trades what gets through.

    python -X utf8 products/leash/agent.py                 # live paper mode (polls the collector DB)
    python -X utf8 products/leash/agent.py --replay 3600   # replay the last hour fast, into data_replay/

Paper only. Live trading is not built and the agent refuses mode=live: the 18k-launch backtest (backtest.py)
shows every buy rule we tried loses money after fees.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
REPLAY = '--replay' in sys.argv
if REPLAY:
    os.environ['LEASH_DATA'] = os.path.join(HERE, 'data_replay')
    import shutil
    shutil.rmtree(os.environ['LEASH_DATA'], ignore_errors=True)
sys.path.insert(0, HERE)
import leash  # noqa: E402
import x402_pay  # noqa: E402

INTEL_URL = os.environ.get('LEASH_INTEL_URL', 'http://127.0.0.1:8799/x402/devcheck/{mint}')

DB = os.path.join(ROOT, 'backend', 'launchdata', 'launches.db')
WSOL = 'So11111111111111111111111111111111111111112'
FEED = os.path.join(leash.DATA, 'feed.jsonl')
STATS = os.path.join(leash.DATA, 'stats.json')
FEE = 0.0125            # pump.fun fee per side (protocol + creator), paper fills pay it both ways
DECIDE_S = 10
TP, SL, HOLD_S = 1.5, 0.75, 180
DATA_PRICE_USD = 0.001  # paper price of one x402 data check
DATA_PAYEE = ('launch intel', '5tP9AdEw6R3d2EkCzHQA6FHGSEQaxE5MsXT6TMoRc3ca')   # our x402 seller (keys/merchant.json)


def db():
    return sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10)


def log(ev: dict):
    ev['t'] = round(leash.clock(), 1)
    with open(FEED, 'a', encoding='utf8') as f:
        f.write(json.dumps(ev) + '\n')


def facts(c, mint, t0, creator, payer, dev_sol, mayhem, now):
    devs = {creator, payer}
    tr = c.execute("select ts, trader, side, sol, mcap_sol from trades where mint=? and ts<=? order by ts", (mint, now)).fetchall()
    buyers = {w for t, w, s, sol, mc in tr if s == 'buy' and w not in devs}
    snipers = {w for t, w, s, sol, mc in tr if s == 'buy' and w not in devs and t - t0 <= 3}
    f = c.execute("select n_sigs_before, capped, prior_launches_hist, prior_launches_live from dev_features where mint=?", (mint,)).fetchone()
    mc = next((x[4] for x in reversed(tr) if x[4]), None)
    return {
        'buyers': len(buyers), 'snipers': len(snipers), 'dev_buy': round(dev_sol or 0, 3),
        'dev_sold': any(s == 'sell' and w in devs for t, w, s, sol, m in tr),
        'net_sol': round(sum(sol or 0 for t, w, s, sol, m in tr if w not in devs and s == 'buy') - sum(sol or 0 for t, w, s, sol, m in tr if w not in devs and s == 'sell'), 3),
        'prior': (f[2] or 0) + (f[3] or 0) if f else None, 'fresh': bool(f and f[0] is not None and f[0] <= 5 and not f[1]),
        'mayhem': bool(mayhem), 'mcap_sol': round(mc, 2) if mc else None,
    }


def wants(f) -> str | None:
    """The agent's own (simple) buy rule. The leash does the safety checks, not this."""
    if f['buyers'] >= 5 and (f['mcap_sol'] or 0) > 0:
        return f"{f['buyers']} buyers in the first {DECIDE_S}s"
    return None


class Agent:
    def __init__(self):
        self.pending = {}       # mint -> token row
        self.cursor = leash.clock() - 30
        self.looked = 0
        self.stats = json.load(open(STATS)) if os.path.exists(STATS) else {'looked': 0, 'wanted': 0, 'paper_pnl_sol': 0.0, 'trades': 0, 'wins': 0}

    def poll_new(self, c, now):
        rows = c.execute("""select mint, ts, symbol, name, creator, payer, dev_sol, coalesce(mayhem,0) from tokens
                            where pool='pump2' and (quote_mint is null or quote_mint=?) and ts>? and ts<=? order by ts""",
                         (WSOL, self.cursor, now)).fetchall()
        for r in rows:
            self.pending[r[0]] = r
            self.cursor = max(self.cursor, r[1])

    def decide(self, c, now):
        for mint, row in list(self.pending.items()):
            _, t0, sym, name, creator, payer, dev_sol, mayhem = row
            if now < t0 + DECIDE_S:
                continue
            del self.pending[mint]
            self.stats['looked'] += 1
            f = facts(c, mint, t0, creator, payer, dev_sol, mayhem, now)
            why = wants(f)
            if not why:
                continue
            self.stats['wanted'] += 1
            cfg = leash.config()
            act = {'type': 'buy', 'mint': mint, 'symbol': sym, 'name': name, 'sol': cfg['max_trade_sol'], 'venue': 'pump.fun', 'facts': f}
            # 1) free checks first: if the leash would block the buy anyway, don't pay for data
            pre = leash.check(act, commit=False)
            if pre['allowed']:
                # 2) pay for the data check over x402. paper = booked only; devnet/mainnet = a real USDC payment on-chain
                xm = cfg.get('x402_mode', 'paper')
                if xm == 'paper' or REPLAY:
                    pay = leash.check({'type': 'pay', 'to': DATA_PAYEE[1], 'payee': DATA_PAYEE[0], 'usd': DATA_PRICE_USD, 'purpose': f'deep check {sym}'})
                    log({'kind': 'pay', 'allowed': pay['allowed'], 'reasons': pay['reasons'], 'usd': DATA_PRICE_USD, 'payee': DATA_PAYEE[0], 'symbol': sym, 'mint': mint})
                    if not pay['allowed']:
                        continue
                else:
                    try:
                        r = x402_pay.pay(INTEL_URL.format(mint=mint), f'dev check {sym}')
                    except Exception as e:  # network/facilitator trouble: no data, no buy
                        log({'kind': 'pay', 'allowed': False, 'reasons': [f'Payment error: {str(e)[:120]}'], 'usd': None, 'payee': DATA_PAYEE[0], 'symbol': sym, 'mint': mint})
                        continue
                    body = r.get('body') or {}
                    paid = body.get('paid') or {}
                    usd = (paid.get('usd') if paid else None) or (int(r['requirement']['amount']) / 1e6 if r.get('requirement') else None)
                    reasons = r.get('blocked') or ([] if r.get('paid') else [f"Payment failed: {(body.get('error') if isinstance(body, dict) else body) or r.get('status')}"])
                    log({'kind': 'pay', 'allowed': bool(r.get('paid')), 'reasons': reasons, 'usd': usd, 'payee': DATA_PAYEE[0], 'symbol': sym,
                         'mint': mint, 'tx': paid.get('tx'), 'explorer': paid.get('explorer'), 'network': xm})
                    if not r.get('paid'):
                        continue
                    d = body.get('data') or {}
                    if d.get('verdict') == 'AVOID':
                        log({'kind': 'buy', 'allowed': False, 'reasons': ['Paid dev check says AVOID: ' + ' '.join(d.get('why', []))], 'why': why,
                             'symbol': sym, 'name': name, 'mint': mint, 'sol': act['sol'], 'facts': f})
                        continue
            # 3) ask to buy (books the spend if allowed)
            r = leash.check(act)
            log({'kind': 'buy', 'allowed': r['allowed'], 'reasons': r['reasons'], 'why': why, 'symbol': sym, 'name': name, 'mint': mint,
                 'sol': act['sol'], 'facts': f})
            if r['allowed']:
                leash.open_position(mint, {'symbol': sym, 'entry_t': now, 'entry_mc': f['mcap_sol'], 'sol': act['sol'],
                                           'devs': [creator, payer], 'last_seen': now})

    def manage(self, c, now):
        for mint, p in list(leash.state()['open'].items()):
            tr = c.execute("select ts, trader, side, mcap_sol from trades where mint=? and ts>? and ts<=? and mcap_sol is not null order by ts",
                           (mint, p['entry_t'], now)).fetchall()
            exit_r, why = None, None
            for t, w, s, mc in tr:
                r = mc / p['entry_mc']
                if r >= TP: exit_r, why = r, f'take profit {TP}x'
                elif r <= SL: exit_r, why = r, f'stop loss {SL}x'
                elif s == 'sell' and w in p['devs']: exit_r, why = r, 'dev sold'
                elif t - p['entry_t'] >= HOLD_S: exit_r, why = r, f'{HOLD_S}s time stop'
                if exit_r is not None:
                    break
            if exit_r is None and now - p['entry_t'] >= HOLD_S:
                last = tr[-1][3] if tr else p['entry_mc']
                exit_r, why = last / p['entry_mc'], f'{HOLD_S}s time stop'
            if exit_r is None:
                continue
            pnl = p['sol'] * (exit_r * (1 - FEE) ** 2 - 1)
            leash.close_position(mint, {'mint': mint, 'symbol': p['symbol'], 'pnl_sol': round(pnl, 5), 'x': round(exit_r, 3), 'why': why, 't': round(now)})
            self.stats['paper_pnl_sol'] += pnl; self.stats['trades'] += 1; self.stats['wins'] += pnl > 0
            log({'kind': 'exit', 'symbol': p['symbol'], 'mint': mint, 'x': round(exit_r, 3), 'pnl_sol': round(pnl, 5), 'why': why})

    def step(self, now):
        c = db()
        try:
            self.poll_new(c, now); self.decide(c, now); self.manage(c, now)
        finally:
            c.close()
        json.dump({**self.stats, 'updated': round(now)}, open(STATS, 'w'))


def main():
    if leash.config()['mode'] != 'paper':
        sys.exit('LEASH agent runs in paper mode only. Live trading is not built (see backtest.py results).')
    if REPLAY:
        span = int(sys.argv[sys.argv.index('--replay') + 1])
        t = time.time() - span - 60
        end = time.time() - 60
        leash.clock = lambda: t
        a = Agent(); a.cursor = t
        while t < end:
            a.step(t)
            t += 2
            leash.clock = (lambda v: (lambda: v))(t)
        s = leash.state()
        print(json.dumps(a.stats), 'open', len(s['open']), 'allowed', s['allowed'], 'blocked', s['blocked'])
        return
    a = Agent()
    print('LEASH paper agent running. Feed:', FEED)
    while True:
        try:
            a.step(time.time())
        except Exception as e:  # keep the loop alive; the collector DB can be busy
            print('step error', e)
        time.sleep(2)


if __name__ == '__main__':
    main()
