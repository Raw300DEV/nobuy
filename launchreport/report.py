"""Launch Report: a creator-side report for one pump.fun coin.

    python -X utf8 report.py <mint>            text report
    python -X utf8 report.py <mint> --json     structured JSON
    python -X utf8 report.py <mint> --deep     + fresh-wallet / dev-funded checks (a few free Helius calls)

Data (all free):
  1. backend/launchdata/launches.db  (PumpPortal collector, opened READ-ONLY): coin + first 10 min of trades.
  2. Helius fallback for coins not in that DB (backend/sniper_study/hel.py, capped: 3 signature pages, 150 txs).
  3. DexScreener public API for the current price/liquidity.
Never prints keys. Never gives advice on evading bots.
"""
import json, os, sys, time, sqlite3, statistics, urllib.request, datetime, threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
DB_PATH = os.path.join(ROOT, 'backend', 'launchdata', 'launches.db')
STUDY = os.path.join(ROOT, 'backend', 'sniper_study')
CACHE = os.path.join(HERE, 'cache')
os.makedirs(CACHE, exist_ok=True)

WINDOW = 600          # report window: first 10 minutes
SNIPE_S = 3.0         # "sniper bot" = first buy within 3 s of create
REPEAT_MIN = 3        # "repeat sniper" = sniped >= 3 OTHER coins in our data
HEL_SIG_PAGES = 3
HEL_MAX_TXS = 150
SUPPLY = 1e9          # pump.fun fixed supply
WSOL = 'So11111111111111111111111111111111111111112'

_lock = threading.Lock()
_mem = {}


def _memo(key, ttl, fn):
    with _lock:
        v = _mem.get(key)
        if v and time.time() - v[0] < ttl:
            return v[1]
    val = fn()
    with _lock:
        _mem[key] = (time.time(), val)
    return val


def db():
    uri = 'file:' + DB_PATH.replace('\\', '/') + '?mode=ro'
    c = sqlite3.connect(uri, uri=True, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'launchreport/0.1', 'Accept': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


# ---------------------------------------------------------------- market data (DexScreener)
def sol_usd():
    def f():
        try:
            pairs = _http_json('https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112').get('pairs') or []
            pairs = [p for p in pairs if (p.get('quoteToken') or {}).get('symbol') in ('USDC', 'USDT') and p.get('baseToken', {}).get('symbol') in ('SOL', 'WSOL')]
            pairs.sort(key=lambda p: -((p.get('liquidity') or {}).get('usd') or 0))
            return float(pairs[0]['priceUsd']) if pairs else None
        except Exception:
            return None
    return _memo('solusd', 600, f)


def dexscreener(mint):
    def f():
        try:
            pairs = _http_json(f'https://api.dexscreener.com/latest/dex/tokens/{mint}').get('pairs') or []
        except Exception as e:
            return {'error': type(e).__name__}
        if not pairs:
            return {'listed': False}
        pairs.sort(key=lambda p: -((p.get('liquidity') or {}).get('usd') or 0))
        p = pairs[0]
        return {'listed': True, 'dex': p.get('dexId'), 'pair': p.get('pairAddress'), 'quote_symbol': (p.get('quoteToken') or {}).get('symbol'), 'price_usd': _f(p.get('priceUsd')),
                'mcap_usd': p.get('marketCap') or p.get('fdv'), 'liquidity_usd': (p.get('liquidity') or {}).get('usd'),
                'volume_24h_usd': (p.get('volume') or {}).get('h24'), 'change_24h_pct': (p.get('priceChange') or {}).get('h24'),
                'url': p.get('url'), 'fetched': int(time.time())}
    return _memo('dex:' + mint, 120, f)


def quote_price(mint, quote):
    """Price of a non-SOL quote token in SOL, from the coin's DexScreener pair (priceUsd / priceNative) and SOL/USD."""
    out = {'mint': quote, 'symbol': None, 'usd': None, 'sol_per_quote': None, 'note': 'quote amounts converted to SOL at the CURRENT quote price'}
    try:
        pairs = _http_json(f'https://api.dexscreener.com/latest/dex/tokens/{mint}').get('pairs') or []
        p = next((p for p in pairs if (p.get('quoteToken') or {}).get('address') == quote), None)
        if p:
            out['symbol'] = p['quoteToken'].get('symbol')
            pu, pn = _f(p.get('priceUsd')), _f(p.get('priceNative'))
            if pu and pn:
                out['usd'] = pu / pn
    except Exception:
        pass
    su = sol_usd()
    if out['usd'] and su:
        out['sol_per_quote'] = out['usd'] / su
    return out


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


# ---------------------------------------------------------------- bonding-curve price model
V_SOL0, V_TOK0 = 30.0, 1_073_000_000.0   # pump.fun initial virtual reserves (SOL, tokens)
V_SOL_DONE = 114.5                        # ~85 real SOL in the curve = curve complete (graduation)
GRAD_MCAP_SOL = 400.0                     # v2 rows carry real mcap; a SOL-quote curve completes at ~410 SOL mcap
QUOTE_NAMES = {WSOL: 'SOL', 'pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn': 'PUMP', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 'USDC'}


def simulate_curve(trades):
    """Fill mcap_sol per trade by replaying net SOL through the constant-product curve (logged trades carry no price).
    mcap = vSol^2 / k * supply, k = 30 * 1.073e9. vSol is capped at curve completion (~85 real SOL), after which trading
    moves to PumpSwap (not recorded). Trades logged with sol == 0 but a token amount get their SOL estimated from the
    curve (flag sol_est). Small drift if some trades were missed."""
    K = V_SOL0 * V_TOK0
    vs = V_SOL0
    if unpriced(trades):   # non-SOL quote pair: logged amounts are not SOL, don't model a price
        for x in trades:
            x['mcap_sol'] = None
        return trades
    for x in trades:
        if not x['sol'] and x['tokens']:
            v = max(V_SOL0 * 0.5, min(vs, V_SOL_DONE)); vt = K / v
            if x['side'] == 'buy' and vt - x['tokens'] > 1e6:
                x['sol'] = K / (vt - x['tokens']) - v; x['sol_est'] = True
            elif x['side'] == 'sell':
                x['sol'] = v - K / (vt + x['tokens']); x['sol_est'] = True
        vs += x['sol'] if x['side'] == 'buy' else -x['sol']
        v = max(V_SOL0 * 0.5, min(vs, V_SOL_DONE))
        x['v_sol'] = vs
        x['curve_done'] = vs >= V_SOL_DONE
        x['mcap_sol'] = v * v / K * SUPPLY
    return trades


def unpriced(trades):
    """Most logged trades carry sol == 0: seen on coins paired with a non-SOL quote token (e.g. PUMP); amounts unusable."""
    lg = [x for x in trades if not x['sig'].endswith(':dev')]
    return len(lg) >= 5 and sum(1 for x in lg if not x['sol']) >= 0.5 * len(lg)


def clean_trades(trades, creator, dev_tokens):
    """Drop the dev create-buy when it was logged twice (once as ':dev', once from the logs with sol 0)."""
    if not any(x['sig'].endswith(':dev') for x in trades):
        return trades
    out = []
    for x in trades:
        if (x['trader'] == creator and x['side'] == 'buy' and not x['sig'].endswith(':dev') and x['dt'] < 10
                and dev_tokens and abs(x['tokens'] - dev_tokens) < 1e-3 and not x['sol']):
            continue
        out.append(x)
    return out


# ---------------------------------------------------------------- loaders
def _capture_start():
    """Receive time of the first non-dev trade in the DB (trade logging start)."""
    c = db()
    try:
        return c.execute("select min(ts) from trades where sig like '%:%' and sig not like '%:dev'").fetchone()[0]
    finally:
        c.close()


def load_local(mint):
    c = db()
    try:
        t = c.execute('select * from tokens where mint=?', (mint,)).fetchone()
        if not t:
            return None
        rows = c.execute("select * from trades where mint=? and coalesce(pool,'') != 'pumpswap' and ts <= (select ts from tokens where mint=?) + 600 order by ts", (mint, mint)).fetchall()  # first 10 min, curve only
    finally:
        c.close()
    t = dict(t)
    t0 = t['ts']
    v2 = t.get('pool') == 'pump2'
    c0 = t.get('chain_ts')
    trades = []
    for r in rows:
        r = dict(r)
        trades.append({'dt': r['ts'] - t0, 'trader': r['trader'], 'side': r['side'], 'sol': r['sol'] or 0.0, 'tokens': r['tokens'] or 0.0,
                       'sol_missing': r['sol'] is None, 'mcap_sol': r.get('mcap_sol') if v2 else None, 'pool': r['pool'], 'sig': r['sig'] or '',
                       'chain_dt': (r['chain_ts'] - c0) if (r.get('chain_ts') is not None and c0) else None, 'ix': r.get('ix'),
                       'quote_amount': r.get('quote_amount')})
    devs = [w for w in dict.fromkeys([t['creator'], t.get('payer')]) if w]
    trades = clean_trades(trades, t['creator'], t['dev_tokens'])
    if v2 and any(x['mcap_sol'] for x in trades):      # real market cap from the curve reserves
        for x in trades:
            x['curve_done'] = (x['mcap_sol'] or 0) >= GRAD_MCAP_SOL and t.get('quote_mint') in (None, WSOL) and not t.get('mayhem')  # mayhem curves run higher
    else:
        simulate_curve(trades)
    raw = {}
    try:
        raw = json.loads(t['raw'] or '{}')
    except Exception:
        pass
    notes = []
    if t.get('pool') == 'bonk':
        notes.append('not a pump.fun coin (letsbonk pool); report may be incomplete')
    elif not v2:
        notes.append('collected by the v1 collector (before ~21:22 on 2026-11-05): low coverage, SOL amounts and market cap are modelled and may be wrong')
        cap_start = _memo('capstart', 600, _capture_start)
        if cap_start and t0 < cap_start + 1:
            notes.append('trade capture started after this coin was created; early trades are missing')
    if unpriced(trades):
        notes.append('trades have no SOL value (quote token without a price): timing and wallets are valid, SOL/market-cap figures are not')
    qm = t.get('quote_mint')
    quote = None
    if qm and qm != WSOL:
        quote = {'mint': qm, 'symbol': QUOTE_NAMES.get(qm), 'note': 'SOL values converted by the collector at Jupiter prices (time of trade)',
                 'sol_per_quote': 1}
    return {'coverage_note': '; '.join(notes) or None, 'source': 'launches.db' + (' (v2)' if v2 else ' (v1, low coverage)'),
            'collector': 'v2' if v2 else 'v1', 'mint': mint, 't0': t0, 'chain_t0': c0, 'creator': t['creator'], 'payer': t.get('payer'),
            'devs': devs, 'name': t['name'], 'symbol': t['symbol'], 'uri': t['uri'], 'dev_sol': t['dev_sol'], 'dev_tokens': t['dev_tokens'],
            'mcap0_sol': t['mcap_sol'], 'mayhem': bool(t['mayhem']) if t.get('mayhem') is not None else raw.get('is_mayhem_mode'),
            'quote': quote, 'trades': trades,
            'time_basis': ('receive time of chain-log events, sub-second (on-chain block seconds also kept)' if v2 else
                           'collector receive time (v1), sub-second'),
            'window_complete': time.time() - t0 >= WINDOW + 30, 'unpriced': unpriced(trades)}


def _hel():
    if STUDY not in sys.path:
        sys.path.insert(0, STUDY)
    import hel  # loads keys itself; never printed
    return hel


def load_helius(mint):
    """Earliest trades of an older coin from Helius (free plan, capped)."""
    p = os.path.join(CACHE, f'hel_{mint}.json')
    if os.path.exists(p):
        return json.load(open(p))
    hel = _hel()
    calls = 0
    sigs, before, capped = [], None, False
    for i in range(HEL_SIG_PAGES):
        prm = {'limit': 1000}
        if before:
            prm['before'] = before
        r = hel.rpc('getSignaturesForAddress', [mint, prm]).get('result') or []
        calls += 1
        sigs += r
        if len(r) < 1000:
            break
        before = r[-1]['signature']
        if i == HEL_SIG_PAGES - 1:
            capped = True
    if capped:
        return {'source': 'helius', 'mint': mint, 'error': f'coin has more than {HEL_SIG_PAGES*1000} transactions; the free fallback cannot reach its launch. Coins created after the collector started are covered fully.'}
    oldest = [s['signature'] for s in reversed(sigs) if not s.get('err')][:HEL_MAX_TXS]
    if not oldest:
        return {'source': 'helius', 'mint': mint, 'error': 'no transactions found for this address'}
    txs = [t for t in hel.post_txs(oldest) if t]
    calls += (len(oldest) + 99) // 100
    txs.sort(key=lambda t: (t['slot'], oldest.index(t['signature']) if t['signature'] in oldest else 0))
    create = next((t for t in txs if t.get('type') == 'CREATE'), None) or txs[0]
    creator = create.get('feePayer')
    # bonding curve = the account that hands the new coin to the dev in the create tx
    curve = None
    for tt in create.get('tokenTransfers') or []:
        if tt.get('mint') == mint and tt.get('fromUserAccount') and tt.get('toUserAccount') == creator:
            curve = tt['fromUserAccount']; break
    if not curve:
        for tt in create.get('tokenTransfers') or []:
            if tt.get('mint') == mint and tt.get('fromUserAccount'):
                curve = tt['fromUserAccount']; break
    # quote asset: SOL normally; some newer pump coins are paired with an SPL token (e.g. an xStock)
    quote = None
    for tt in create.get('tokenTransfers') or []:
        if tt.get('mint') not in (mint, WSOL) and tt.get('toUserAccount') == curve:
            quote = tt['mint']; break
    t0, s0 = create['timestamp'], create['slot']
    trades = []
    for t in txs:
        if t.get('transactionError'):
            continue
        trader, delta = None, 0.0
        for tt in t.get('tokenTransfers') or []:
            if tt.get('mint') != mint:
                continue
            if tt.get('fromUserAccount') == curve and tt.get('toUserAccount'):
                trader, delta = tt['toUserAccount'], delta + tt['tokenAmount']
            elif tt.get('toUserAccount') == curve and tt.get('fromUserAccount'):
                trader, delta = tt['fromUserAccount'], delta - tt['tokenAmount']
        if not trader or delta == 0:
            continue
        if quote:  # quote token moved between trader and curve (fees go elsewhere)
            q = sum(tt['tokenAmount'] for tt in t.get('tokenTransfers') or [] if tt.get('mint') == quote and
                    ((tt.get('fromUserAccount') == trader and tt.get('toUserAccount') == curve) or
                     (tt.get('fromUserAccount') == curve and tt.get('toUserAccount') == trader)))
        else:
            acc = next((a for a in t.get('accountData') or [] if a['account'] == curve), {})
            q = abs(acc.get('nativeBalanceChange', 0)) / 1e9
            if q == 0:
                ta = next((a for a in t.get('accountData') or [] if a['account'] == trader), {})
                q = abs(ta.get('nativeBalanceChange', 0) + (t.get('fee', 0) if trader == t.get('feePayer') else 0)) / 1e9
        trades.append({'dt': round((t['slot'] - s0) * 0.4, 2), 'dt_blocktime': t['timestamp'] - t0, 'trader': trader,
                       'side': 'buy' if delta > 0 else 'sell', 'sol': q, 'tokens': abs(delta), 'mcap_sol': None, 'pool': 'pump', 'sig': t['signature']})
    if quote:  # convert quote-token amounts to SOL at today's price (DexScreener); label clearly
        qinfo = quote_price(mint, quote)
        rate = qinfo.get('sol_per_quote')
        for x in trades:
            x['quote_amount'] = x['sol']
            x['sol'] = x['sol'] * rate if rate else 0.0
            x['mcap_sol'] = (x['sol'] / x['tokens'] * SUPPLY) if x['tokens'] and rate else None
    else:
        qinfo = None
        simulate_curve(trades)
    name = symbol = uri = None
    try:
        a = hel.rpc('getAsset', {'id': mint}).get('result') or {}
        calls += 1
        md = (a.get('content') or {}).get('metadata') or {}
        name, symbol = md.get('name'), md.get('symbol')
        uri = (a.get('content') or {}).get('json_uri')
    except Exception:
        pass
    dev_first = next((x for x in trades if x['trader'] == creator and x['side'] == 'buy'), None)
    out = {'source': 'helius', 'mint': mint, 't0': t0, 'creator': creator, 'devs': [creator], 'name': name, 'symbol': symbol, 'uri': uri,
           'dev_sol': dev_first['sol'] if dev_first and dev_first['dt'] <= 1 else 0.0,
           'dev_tokens': dev_first['tokens'] if dev_first and dev_first['dt'] <= 1 else 0.0,
           'mcap0_sol': None, 'mayhem': None, 'trades': trades, 'quote': qinfo,
           'time_basis': 'on-chain slots x 0.4 s (approx.)', 'window_complete': True,
           'helius_calls': calls, 'helius_txs': len(txs),
           'coverage_note': f'earliest {len(txs)} transactions only' + (' (window may be cut short)' if len(oldest) >= HEL_MAX_TXS else '')}
    if len(oldest) >= HEL_MAX_TXS and trades:
        out['covered_until_s'] = trades[-1]['dt']
    json.dump(out, open(p, 'w'))
    return out


def load(mint, allow_helius=True):
    d = load_local(mint)
    if d is not None:
        return d
    if not allow_helius:
        return {'source': None, 'mint': mint, 'error': 'not in launches.db (fallback disabled)'}
    return load_helius(mint)


# ---------------------------------------------------------------- reference data from the whole DB
def known_snipers():
    """wallet -> number of DISTINCT coins it bought within SNIPE_S of create (whole DB), refreshed every 10 min."""
    def f():
        c = db()
        try:
            rows = c.execute('''select x.trader w, count(distinct x.mint) n from trades x join tokens t on t.mint=x.mint
                                where x.side='buy' and x.trader != t.creator and x.trader != coalesce(t.payer, '') and x.ts - t.ts <= ?
                                group by x.trader''', (SNIPE_S,)).fetchall()
        finally:
            c.close()
        return {r['w']: r['n'] for r in rows}
    return _memo('snipers', 600, f)


def study_snipers():
    try:
        return {x['wallet']: x.get('n_coins') for x in json.load(open(os.path.join(STUDY, 'sniper_profiles.json')))}
    except Exception:
        return {}


def _cum_to_mcap(c):
    """Constant-product curve: market cap (SOL) after `c` net SOL has gone into the curve."""
    if c is None:
        return None
    vs = min(V_SOL0 + c, V_SOL_DONE)
    return vs * vs / (V_SOL0 * V_TOK0) * SUPPLY


def coin_stats(where, params):
    """Headline stats per coin, computed with the same rules as the full report (v2: real mcap; v1: curve model)."""
    c = db()
    try:
        toks = c.execute(f'select mint, ts t0, creator, payer, pool, quote_mint, name, symbol, dev_sol, dev_tokens from tokens t where {where}', params).fetchall()
        by = {}
        if toks:
            lo = min(t['t0'] for t in toks)
            q = f"""select x.mint, x.ts - t.ts dt, x.trader, x.side, x.sol, x.tokens, x.sig, x.mcap_sol from trades x join tokens t on t.mint = x.mint
                    where {where} and x.ts >= ? and x.ts - t.ts <= ? order by x.mint, x.ts"""
            for r in c.execute(q, tuple(params) + (lo - 5, WINDOW)):
                by.setdefault(r['mint'], []).append({'dt': r['dt'], 'trader': r['trader'], 'side': r['side'], 'sol': r['sol'] or 0.0,
                                                     'tokens': r['tokens'] or 0.0, 'sig': r['sig'] or '', 'mcap_sol': r['mcap_sol']})
    finally:
        c.close()
    out = []
    for t in toks:
        v2 = t['pool'] == 'pump2'
        tr = clean_trades(by.get(t['mint'], []), t['creator'], t['dev_tokens'])
        if not (v2 and any(x['mcap_sol'] for x in tr)):
            simulate_curve(tr)
        devs = {t['creator'], t['payer']} - {None}
        nd = [x for x in tr if x['trader'] not in devs]
        sells = [x['dt'] for x in tr if x['trader'] in devs and x['side'] == 'sell']
        out.append({'mint': t['mint'], 't0': t['t0'], 'creator': t['creator'], 'name': t['name'], 'symbol': t['symbol'], 'dev_sol': t['dev_sol'],
                    'v2': v2, 'pool': t['pool'], 'quote': QUOTE_NAMES.get(t['quote_mint'], t['quote_mint']) if t['quote_mint'] else None,
                    'trades': len(tr),
                    'snipers': len({x['trader'] for x in nd if x['side'] == 'buy' and x['dt'] <= SNIPE_S}),
                    'vol60': sum(x['sol'] for x in nd if x['dt'] <= 60),
                    'buyers60': len({x['trader'] for x in nd if x['side'] == 'buy' and x['dt'] <= 60}),
                    'peak': max((x['mcap_sol'] for x in tr if x['mcap_sol'] is not None), default=None),
                    'dev_sell': min(sells) if sells else None, 'unpriced': unpriced(tr)})
    return out


def day_stats(day):
    """Per-coin headline stats for every coin created on `day` (local date), at least 70 s old."""
    def f():
        start = datetime.datetime.strptime(day, '%Y-%m-%d').timestamp()
        cs = _memo('capstart', 600, _capture_start) or 0   # skip coins created before trade logging began
        rows = coin_stats('t.ts >= ? and t.ts < ? and t.ts < ?', (start, start + 86400, time.time() - 70))
        rows = [r for r in rows if not r['unpriced'] and r['pool'] != 'bonk']
        v2 = [r for r in rows if r['v2']]
        if len(v2) >= 30:   # prefer full-coverage v2 rows
            return v2
        return [r for r in rows if r['v2'] or r['t0'] >= cs]
    return _memo('day:' + day, 300, f)


def pct_rank(vals, v):
    vals = [x for x in vals if x is not None]
    if not vals or v is None:
        return None
    below = sum(1 for x in vals if x < v)
    eq = sum(1 for x in vals if x == v)
    return round(100.0 * (below + 0.5 * eq) / len(vals), 1)


# ---------------------------------------------------------------- deep checks (optional, few Helius calls)
def deep_wallet_checks(wallets, devs, t0, cap=15):
    """fresh = <10 lifetime txs or first tx < 24h before launch; dev-linked = first SOL into wallet came from the dev wallet."""
    devs = set(devs)
    key = os.path.join(CACHE, 'deep_' + sorted(devs)[0][:8] + '_' + str(int(t0)) + '.json')
    if os.path.exists(key):
        return json.load(open(key))
    hel = _hel()
    out, first_sigs = {}, {}
    for w in wallets[:cap]:
        try:
            r = hel.rpc('getSignaturesForAddress', [w, {'limit': 50}]).get('result') or []
        except Exception:
            continue
        if not r:
            continue
        oldest = r[-1]
        n = len(r)
        age_h = (t0 - (oldest.get('blockTime') or t0)) / 3600
        out[w] = {'txs_seen': n if n < 50 else '50+', 'first_tx_h_before_launch': round(age_h, 1) if n < 50 else None,
                  'fresh': n < 50 and (n < 10 or age_h < 24), 'dev_linked': False}
        if n < 50:
            first_sigs[w] = oldest['signature']
    if first_sigs:
        try:
            txs = hel.post_txs(list(first_sigs.values()))
            by = {t['signature']: t for t in txs if t}
            for w, s in first_sigs.items():
                t = by.get(s) or {}
                for nt in t.get('nativeTransfers') or []:
                    if nt['toUserAccount'] == w and nt['fromUserAccount'] in devs:
                        out[w]['dev_linked'] = True
                        out[w]['dev_link_proof'] = s
        except Exception:
            pass
    json.dump(out, open(key, 'w'))
    return out


# ---------------------------------------------------------------- the report
def _r(x, n=3):
    return None if x is None else round(x, n)


def build(mint, deep=False, allow_helius=True):
    d = load(mint, allow_helius=allow_helius)
    if d.get('error') or not d.get('trades'):
        return {'mint': mint, 'ok': False, 'error': d.get('error') or 'no trades recorded for this coin yet', 'source': d.get('source')}
    usd = sol_usd()
    creator = d['creator']
    devs = set(d.get('devs') or [creator])
    W = [x for x in d['trades'] if x['dt'] <= WINDOW]
    covered = min(WINDOW, d.get('covered_until_s', WINDOW))

    # ---- 1. summary
    mc = [(x['dt'], x['mcap_sol']) for x in W if x['mcap_sol']]
    peak = max(mc, key=lambda z: z[1]) if mc else (None, None)
    last = mc[-1] if mc else (None, None)
    pools = sorted({x['pool'] for x in W if x.get('pool')})
    summary = {
        'name': d['name'], 'symbol': d['symbol'], 'mint': mint, 'creator': creator,
        'created_ts': d['t0'], 'created_iso': datetime.datetime.fromtimestamp(d['t0']).astimezone().isoformat(timespec='seconds'),
        'dev_buy_sol': _r(d['dev_sol'], 4), 'dev_buy_pct_supply': _r(100 * (d['dev_tokens'] or 0) / SUPPLY, 2),
        'peak_mcap_sol_10m': _r(peak[1], 2), 'peak_mcap_usd_10m': _r(peak[1] * usd, 0) if peak[1] and usd else None,
        'time_to_peak_s': _r(peak[0], 1), 'start_mcap_sol': _r(mc[0][1], 2) if mc else None,
        'end_mcap_sol_10m': _r(last[1], 2), 'end_mcap_usd_10m': _r(last[1] * usd, 0) if last[1] and usd else None,
        'trades_10m': len(W), 'pools_seen': pools, 'graduated_in_window': any(x.get('curve_done') for x in W),
        'payer': d.get('payer'), 'dev_wallets': sorted(devs), 'collector': d.get('collector'),
        'quote': d.get('quote'),
        'curve_completed_at_s': _r(next((x['dt'] for x in W if x.get('curve_done')), None), 1),
        'mayhem_mode': d.get('mayhem'), 'sol_usd': usd, 'now': dexscreener(mint),
        'window_complete': d['window_complete'], 'covered_s': covered,
    }

    # ---- per-wallet aggregation
    wal = {}
    for x in W:
        w = wal.setdefault(x['trader'], {'wallet': x['trader'], 'first_buy': None, 'first_sell': None, 'buy_sol': 0.0, 'sell_sol': 0.0,
                                         'bought_tok': 0.0, 'sold_tok': 0.0, 'n_buy': 0, 'n_sell': 0})
        if x['side'] == 'buy':
            w['buy_sol'] += x['sol']; w['bought_tok'] += x['tokens']; w['n_buy'] += 1
            if w['first_buy'] is None:
                w['first_buy'] = x['dt']; w['first_buy_chain'] = x.get('chain_dt')
        else:
            w['sell_sol'] += x['sol']; w['sold_tok'] += x['tokens']; w['n_sell'] += 1
            if w['first_sell'] is None:
                w['first_sell'] = x['dt']
    dev = {'first_sell': None, 'buy_sol': 0.0, 'sell_sol': 0.0, 'bought_tok': 0.0, 'sold_tok': 0.0, 'n_sell': 0}
    for k in devs:   # creator + fee payer (often a relayer) both count as the dev
        w = wal.get(k)
        if not w:
            continue
        for f in ('buy_sol', 'sell_sol', 'bought_tok', 'sold_tok', 'n_sell'):
            dev[f] += w[f]
        if w['first_sell'] is not None and (dev['first_sell'] is None or w['first_sell'] < dev['first_sell']):
            dev['first_sell'] = w['first_sell']

    # ---- 2. first 60 s buyer mix
    ks, ss = known_snipers(), study_snipers()
    early = sorted([w for k, w in wal.items() if k not in devs and w['first_buy'] is not None and w['first_buy'] <= 60], key=lambda w: w['first_buy'])
    deepinfo = deep_wallet_checks([w['wallet'] for w in early], [creator], d['t0']) if deep and early else {}  # creator only: fee payers are often shared relayers
    buckets = {'0-3s': (-1e9, SNIPE_S), '3-10s': (SNIPE_S, 10), '10-60s': (10, 60)}
    mix = {}
    for b, (lo, hi) in buckets.items():
        ws = [w for w in early if lo < w['first_buy'] <= hi]
        # SOL = all buys by these wallets inside the bucket window
        sol = sum(x['sol'] for x in W if x['side'] == 'buy' and x['trader'] not in devs and lo < x['dt'] <= hi)
        mix[b] = {'wallets': len(ws), 'buy_sol': _r(sol, 3)}
    buyers = []
    for w in early:
        tags = []
        if w['first_buy'] <= SNIPE_S:
            tags.append('sniper bot')
        other = ks.get(w['wallet'], 0) - (1 if w['first_buy'] <= SNIPE_S and d['source'] == 'launches.db' else 0)
        if other >= REPEAT_MIN or w['wallet'] in ss:
            tags.append('repeat sniper')
        di = deepinfo.get(w['wallet'])
        if di and di.get('fresh'):
            tags.append('fresh wallet')
        if di and di.get('dev_linked'):
            tags.append('dev-linked')
        if not tags:
            tags.append('early buyer')
        held = (w['bought_tok'] - w['sold_tok']) / w['bought_tok'] if w['bought_tok'] else 0
        buyers.append({'wallet': w['wallet'], 'first_buy_s': _r(w['first_buy'], 2), 'first_buy_chain_s': w.get('first_buy_chain'), 'buy_sol': _r(w['buy_sol'], 4),
                       'first_sell_s': _r(w['first_sell'], 2), 'sell_sol': _r(w['sell_sol'], 4),
                       'pnl_sol_10m': _r(w['sell_sol'] - w['buy_sol'], 4) if w['n_sell'] else None,
                       'held_pct_at_10m': _r(100 * max(0.0, held), 1), 'snipes_elsewhere': max(0, other),
                       'tags': tags, 'deep': di})
    dev_sell = dev['first_sell']
    exits = sorted([b for b in buyers if b['first_sell_s'] is not None], key=lambda b: b['first_sell_s'])
    first_exits = [{'wallet': b['wallet'], 'tags': b['tags'], 'sold_at_s': b['first_sell_s'], 'held_for_s': _r(b['first_sell_s'] - b['first_buy_s'], 1),
                    'vs_dev_sell_s': _r(b['first_sell_s'] - dev_sell, 1) if dev_sell is not None else None, 'sell_sol': b['sell_sol']} for b in exits[:10]]
    tagcount = {}
    for b in buyers:
        for t in b['tags']:
            tagcount[t] = tagcount.get(t, 0) + 1
    snipers = [b for b in buyers if 'sniper bot' in b['tags']]
    has_chain = any(b['first_buy_chain_s'] is not None for b in buyers)
    if has_chain:
        same_second = sum(1 for b in buyers if b['first_buy_chain_s'] == 0)
    else:
        same_second = sum(1 for b in buyers if b['first_buy_s'] <= 0.5)
    buyer_mix = {'buckets': mix, 'tag_counts': tagcount, 'buyers': buyers, 'first_exits': first_exits,
                 'sniper_sol': _r(sum(b['buy_sol'] for b in snipers), 3),
                 'sniper_sold_by_10m': sum(1 for b in snipers if b['first_sell_s'] is not None),
                 'buys_within_0_5s': same_second, 'same_second_basis': 'on-chain second of the create' if has_chain else 'within 0.5s of create (receive time)', 'deep_checked': bool(deepinfo),
                 'labels': {'sniper bot': f'first buy <= {SNIPE_S:g}s after create',
                            'repeat sniper': f'also bought <= {SNIPE_S:g}s on {REPEAT_MIN}+ other coins in our data, or listed in our sniper study',
                            'fresh wallet': '<10 lifetime txs or first tx <24h before launch (only with --deep)',
                            'dev-linked': 'wallet first funded directly by the dev wallet, on-chain proof (only with --deep)'}}

    # ---- 3. dev behaviour
    dev_trades = [x for x in W if x['trader'] in devs]
    devb = {'bought_sol': _r(dev['buy_sol'], 4), 'sold_sol': _r(dev['sell_sol'], 4), 'sell_count': dev['n_sell'],
            'first_sell_s': _r(dev_sell, 2), 'net_sol_10m': _r(dev['sell_sol'] - dev['buy_sol'], 4),
            'trades': [{'t_s': _r(x['dt'], 2), 'wallet': x['trader'], 'side': x['side'], 'sol': _r(x['sol'], 4), 'tokens': _r(x['tokens'], 0)} for x in dev_trades[:20]]}
    if dev_sell is not None:
        fs = next(x for x in dev_trades if x['side'] == 'sell')
        devb['first_sell_sol'] = _r(fs['sol'], 4)
        devb['first_sell_pct_of_bag'] = _r(100 * fs['tokens'] / dev['bought_tok'], 1) if dev['bought_tok'] else None
        devb['sold_pct_of_bag_10m'] = _r(100 * dev['sold_tok'] / dev['bought_tok'], 1) if dev['bought_tok'] else None
        others = [x for x in W if x['trader'] not in devs]
        def cnt(lo, hi, side='buy'):
            return sum(1 for x in others if x['side'] == side and lo < x['dt'] <= hi)
        def vol(lo, hi, side='buy'):
            return sum(x['sol'] for x in others if x['side'] == side and lo < x['dt'] <= hi)
        after_buys = [x['dt'] for x in others if x['side'] == 'buy' and x['dt'] > dev_sell]
        gaps = [b - a for a, b in zip([dev_sell] + after_buys, after_buys + [covered])]
        stop = None
        prev = dev_sell
        for tnext in after_buys + [covered]:
            if tnext - prev >= 30:
                stop = prev
                break
            prev = tnext
        devb['after_sell'] = {
            'buys_30s_before': cnt(dev_sell - 30, dev_sell), 'buys_30s_after': cnt(dev_sell, dev_sell + 30),
            'buy_sol_30s_before': _r(vol(dev_sell - 30, dev_sell), 3), 'buy_sol_30s_after': _r(vol(dev_sell, dev_sell + 30), 3),
            'sells_30s_after': cnt(dev_sell, dev_sell + 30, 'sell'),
            'next_buy_after_s': _r(after_buys[0] - dev_sell, 1) if after_buys else None,
            'buys_after_to_10m': len(after_buys),
            'buying_stopped_at_s': _r(stop, 1) if stop is not None else None,
            'buying_stopped_within_s': _r(stop - dev_sell, 1) if stop is not None else None,
            'max_gap_after_s': _r(max(gaps), 1) if gaps else None,
        }

    # ---- 4. curve, 10 s buckets
    curve, seen, cb, cs, net = [], set(), 0, 0, 0.0
    for i in range(int(covered // 10) or 1):
        lo, hi = i * 10, (i + 1) * 10
        xs = [x for x in W if (lo < x['dt'] <= hi) or (i == 0 and x['dt'] <= 0)]
        bs = [x for x in xs if x['side'] == 'buy']
        sl = [x for x in xs if x['side'] == 'sell']
        cb += len(bs); cs += len(sl)
        seen.update(x['trader'] for x in xs)
        nf = sum(x['sol'] for x in bs) - sum(x['sol'] for x in sl)
        net += nf
        m = [x['mcap_sol'] for x in xs if x['mcap_sol']]
        curve.append({'t': hi, 'buys': len(bs), 'sells': len(sl), 'cum_buys': cb, 'cum_sells': cs, 'unique_traders': len(seen),
                      'buy_sol': _r(sum(x['sol'] for x in bs), 3), 'sell_sol': _r(sum(x['sol'] for x in sl), 3),
                      'net_sol': _r(nf, 3), 'cum_net_sol': _r(net, 3), 'mcap_close_sol': _r(m[-1], 2) if m else None,
                      'dev_sold': any(x['trader'] in devs and x['side'] == 'sell' for x in xs)})
    for i in range(1, len(curve)):  # carry mcap forward through quiet buckets
        if curve[i]['mcap_close_sol'] is None:
            curve[i]['mcap_close_sol'] = curve[i - 1]['mcap_close_sol']

    # ---- 5. comparison vs same day
    day = datetime.datetime.fromtimestamp(d['t0']).strftime('%Y-%m-%d')
    try:
        ds = day_stats(day)
        if not ds:
            c = db(); lt = c.execute('select max(ts) from tokens').fetchone()[0]; c.close()
            if lt:
                day = datetime.datetime.fromtimestamp(lt).strftime('%Y-%m-%d'); ds = day_stats(day)
    except Exception:
        ds = []
    me = {'snipers': len(snipers), 'vol60': sum(x['sol'] for x in W if x['dt'] <= 60 and x['trader'] not in devs),
          'buyers60': len(early), 'peak': peak[1]}
    comparison = {'day': day, 'coins_compared': len(ds), 'same_day': datetime.datetime.fromtimestamp(d['t0']).strftime('%Y-%m-%d') == day}
    comparison['enough_data'] = len(ds) >= 30
    if len(ds) >= 30:
        for k in (('snipers', 'buyers60') if d.get('unpriced') else ('snipers', 'vol60', 'buyers60', 'peak')):
            vals = [r[k] or 0 for r in ds]
            comparison[k] = {'value': _r(me[k], 3), 'percentile': pct_rank(vals, me[k] or 0), 'day_median': _r(statistics.median(vals), 3)}
        fast = [r['peak'] for r in ds if r['dev_sell'] is not None and r['dev_sell'] <= 30 and r['peak']]
        held = [r['peak'] for r in ds if r['dev_sell'] is None and r['peak'] and time.time() - r['t0'] > WINDOW]
        if len(fast) >= 20 and len(held) >= 20:
            comparison['dev_sell_effect'] = {'dev_sold_within_30s': {'n': len(fast), 'median_peak_sol': _r(statistics.median(fast), 2)},
                                             'dev_held_10m': {'n': len(held), 'median_peak_sol': _r(statistics.median(held), 2)}}

    rep = {'ok': True, 'mint': mint, 'unpriced': bool(d.get('unpriced')), 'generated': int(time.time()), 'source': d['source'], 'time_basis': d['time_basis'],
           'coverage_note': d.get('coverage_note'), 'helius_calls': d.get('helius_calls'),
           'summary': summary, 'buyer_mix': buyer_mix, 'dev': devb, 'curve': curve, 'comparison': comparison,
           'dev_history': dev_history(mint, creator, d['t0'])}
    rep['takeaways'] = takeaways(rep)
    if rep['unpriced']:
        rep['takeaways'].insert(0, "This coin's trades were logged without SOL amounts (it looks paired with a non-SOL token such as PUMP), so volumes and market cap are not shown. Wallet timing is still accurate.")
    return rep



# ---------------------------------------------------------------- dev history (step-0 backed: REPEAT DEV warning + facts)
CLAW_PATH = os.path.join(ROOT, 'backend', 'claw.db')
COND_PATH = os.path.join(ROOT, 'products', 'conditions', 'data.json')


def _hist_counts():
    def f():
        out = {}
        try:
            c = sqlite3.connect('file:' + CLAW_PATH.replace(os.sep, '/') + '?mode=ro', uri=True, timeout=10)
            for (k,) in c.execute('select bc_creator from mint_creators where bc_creator is not null'):
                out[k] = out.get(k, 0) + 1
            c.close()
        except Exception:
            pass
        return out
    return _memo('claw_hist', 86400, f)


def _base_rates():
    def f():
        try:
            d = json.load(open(COND_PATH, encoding='utf8'))
        except Exception:
            return None
        g = {r['group']: r for r in d.get('by_prior', [])}
        return {'n_coins': d.get('n_coins'), 'groups': {k: {kk: g[k].get(kk) for kk in ('n', 'med_snip10', 'med_buyers600', 'pct_10plus_buyers')} for k in g}}
    return _memo('base_rates', 3600, f)


def dev_history(mint, creator, t0, n_past=8):
    """What the market can see about the dev wallet as of the launch: prior launches, how they went, funding."""
    c = db()
    try:
        feat = c.execute('select * from dev_features where mint=?', (mint,)).fetchone()
        live = c.execute('select count(*) from tokens where creator=? and ts < ?', (creator, t0)).fetchone()[0]
        day = c.execute('select count(*) from tokens where creator=? and ts < ? and ts >= ?', (creator, t0, t0 - 86400)).fetchone()[0]
        past = c.execute('select mint, symbol, name, ts from tokens where creator=? and ts < ? order by ts desc limit ?', (creator, t0, n_past)).fetchall()
        coins = []
        for p in past:
            tr = c.execute("select ts, trader, side, mcap_sol from trades where mint=? and coalesce(pool,'') != 'pumpswap' and ts <= ? order by ts",
                           (p['mint'], p['ts'] + WINDOW)).fetchall()
            buyers = {x['trader'] for x in tr if x['side'] == 'buy' and x['trader'] != creator}
            ds = next((x['ts'] - p['ts'] for x in tr if x['side'] == 'sell' and x['trader'] == creator), None)
            pk = max((x['mcap_sol'] or 0 for x in tr), default=0)
            coins.append({'mint': p['mint'], 'symbol': p['symbol'], 'name': p['name'], 'ts': p['ts'], 'buyers_10m': len(buyers),
                          'peak_mcap_sol': _r(pk, 2) if pk else None, 'dev_first_sell_s': _r(ds, 1),
                          'window_complete': time.time() - p['ts'] > WINDOW})
    except sqlite3.OperationalError:
        feat, live, day, coins = None, 0, 0, []
    finally:
        c.close()
    hist = _hist_counts().get(creator, 0)
    if feat is not None:   # the enricher's count, captured seconds after launch, is the step-0 measured number
        prior = (feat['prior_launches_hist'] or 0) + (feat['prior_launches_live'] or 0)
    else:
        prior = hist + live
    done = [x for x in coins if x['window_complete']]
    fast = [x for x in done if x['dev_first_sell_s'] is not None and x['dev_first_sell_s'] <= 60]
    group = '0' if prior == 0 else '1-2' if prior <= 2 else '3-10' if prior <= 10 else '10+'
    br = _base_rates()
    out = {
        'creator': creator, 'prior_launches': prior, 'prior_in_our_history': hist, 'prior_live_seen': live, 'launches_24h_before': day,
        'warning': 'REPEAT DEV' if prior >= 1 else None,
        'past_coins': coins,
        'past_summary': {'n': len(done), 'dev_sold_within_60s': len(fast),
                         'median_buyers_10m': _r(statistics.median([x['buyers_10m'] for x in done]), 1) if done else None},
        'wallet': None,
        'base_rate': None,
    }
    if feat is not None:
        out['wallet'] = {'txs_before_launch': feat['n_sigs_before'], 'txs_capped': bool(feat['capped']), 'age_h': _r(feat['age_h'], 1),
                         'fresh': feat['n_sigs_before'] is not None and feat['n_sigs_before'] <= 5 and not feat['capped'],
                         'funder': feat['funder'], 'funder_sol': _r(feat['funder_sol'], 3), 'funder_txs': feat['funder_n_sigs'],
                         'funder_high_volume': None if feat['funder_hot'] is None else bool(feat['funder_hot']),
                         'sol_balance': _r(feat['bal_sol'], 3), 'checked_after_s': _r(feat['lag_s'], 1)}
    if br and group in br['groups']:
        out['base_rate'] = {'group': group, 'of_coins': br['n_coins'], **br['groups'][group], 'first_time': br['groups'].get('0')}
    return out

# ---------------------------------------------------------------- plain-English takeaways (rule-based)
def _s(x):
    return f'+{x:.0f}s' if x is not None and x >= 10 else (f'+{x:.1f}s' if x is not None else '?')


def takeaways(r):
    s, bm, dv, cp = r['summary'], r['buyer_mix'], r['dev'], r['comparison']
    out = []
    nsn = len([b for b in bm['buyers'] if 'sniper bot' in b['tags']])
    rep_ = bm['tag_counts'].get('repeat sniper', 0)
    if nsn:
        b3 = bm['buckets'].get('0-3s', {}).get('buy_sol')
        t = f"{nsn} wallet(s) bought within {SNIPE_S:g}s of create" + ('' if r.get('unpriced') else f", putting in {b3} SOL in those 3 seconds ({bm['sniper_sol']} SOL across all their buys in 10 min)")
        if rep_:
            t += f"; {rep_} early buyer(s) are repeat snipers we have seen on other launches"
        t += f". {bm['sniper_sold_by_10m']} of the {nsn} had sold some or all within 10 minutes."
        out.append(t)
    else:
        out.append(f'No wallet bought in the first {SNIPE_S:g}s. The early buyers came in later, which usually means human or slower bot traffic.')
    if bm['buys_within_0_5s'] >= 3:
        basis = 'in the same on-chain second as the create' if bm.get('same_second_basis', '').startswith('on-chain') else 'within 0.5s of create'
        out.append(f"{bm['buys_within_0_5s']} early buyers landed {basis}. That alone does not prove a bundle; it is common for sniper bots.")
    dh = r.get('dev_history') or {}
    if dh.get('prior_launches'):
        t = f"REPEAT DEV: this wallet launched {dh['prior_launches']} coin(s) before this one"
        if dh.get('launches_24h_before'):
            t += f" ({dh['launches_24h_before']} in the 24h before)"
        ps = dh.get('past_summary') or {}
        if ps.get('n'):
            t += f". Of the last {ps['n']} we recorded, the dev sold within 60s on {ps['dev_sold_within_60s']}, and they drew a median {ps['median_buyers_10m']:g} buyers in 10 min"
        b = dh.get('base_rate')
        if b and b.get('first_time'):
            t += f". Across {b['of_coins']:,} launches, coins from devs with {b['group']} prior launches got 10+ buyers {b['pct_10plus_buyers']:.0f}% of the time vs {b['first_time']['pct_10plus_buyers']:.0f}% for first-time devs"
        out.append(t + '.')
    if bm['tag_counts'].get('dev-linked'):
        out.append(f"{bm['tag_counts']['dev-linked']} early buyer(s) were funded directly by the dev wallet (on-chain proof in the buyer table). Buyers and trackers treat that as bundling.")
    if dv['first_sell_s'] is not None:
        a = dv.get('after_sell', {})
        t = f"The dev first sold at {_s(dv['first_sell_s'])}"
        if dv.get('first_sell_pct_of_bag') is not None and not r.get('unpriced'):
            t += f" ({dv['first_sell_pct_of_bag']:.0f}% of the dev bag, {dv['first_sell_sol']} SOL)"
        t += '.'
        if not a.get('buys_after_to_10m'):
            t += ' Nobody bought after that' + (' for the rest of the window.' if r['summary']['window_complete'] else ' (so far).')
        elif a.get('buying_stopped_within_s') == 0:
            t += f" The next buy took {a['next_buy_after_s']:.0f}s to arrive."
        elif a.get('buying_stopped_within_s') is not None and a['buying_stopped_within_s'] < 60:
            t += f" Buying stopped within {a['buying_stopped_within_s']:.0f}s (no buy for 30s+ after that)."
        elif a.get('buying_stopped_within_s') is not None:
            g = r['summary'].get('curve_completed_at_s')
            t += f" Buying continued for another {a['buying_stopped_within_s']:.0f}s" + (" until the coin completed its bonding curve." if g is not None and abs(g - a['buying_stopped_at_s']) < 15 else ".")
        else:
            t += f" Buying continued: {a.get('buys_30s_after', 0)} buys in the next 30s vs {a.get('buys_30s_before', 0)} in the 30s before."
        out.append(t)
        if dv['first_sell_s'] <= 60:
            fx = [e for e in bm['first_exits'] if e['vs_dev_sell_s'] is not None and e['vs_dev_sell_s'] < 0]
            if fx:
                out.append(f"{len(fx)} early buyer(s) sold before the dev did, so some of the exit pressure came first from bots, not only the dev.")
            out.append('Honest-dev note: a dev sale in the first minute is the single loudest exit signal on a new coin. If you plan to hold and build, holding the dev buy past the first minutes (or not taking a large dev buy at all) reads better to real buyers.')
    else:
        out.append('The dev did not sell during the first 10 minutes' + (' of what we saw.' if not s['window_complete'] else '.'))
    if s['peak_mcap_sol_10m']:
        t = f"Peak market cap in the first 10 min was {s['peak_mcap_sol_10m']} SOL"
        if s['peak_mcap_usd_10m']:
            t += f" (~${s['peak_mcap_usd_10m']:,.0f})"
        t += f" at {_s(s['time_to_peak_s'])}"
        if s['end_mcap_sol_10m'] and s['peak_mcap_sol_10m'] > 0:
            dd = 100 * (1 - s['end_mcap_sol_10m'] / s['peak_mcap_sol_10m'])
            t += f"; it ended the window {dd:.0f}% below the peak." if dd >= 5 else '; it ended the window near the peak.'
        out.append(t)
    if cp.get('snipers') and cp.get('vol60'):
        out.append(f"Versus {cp['coins_compared']} coins launched on {cp['day']}: sniper count is at the {cp['snipers']['percentile']:.0f}th percentile, "
                   f"60s volume at the {cp['vol60']['percentile']:.0f}th, unique 60s buyers at the {cp['buyers60']['percentile']:.0f}th.")
    e = cp.get('dev_sell_effect')
    if e:
        out.append(f"Across that day, coins whose dev sold within 30s had a median 10-min peak of {e['dev_sold_within_30s']['median_peak_sol']} SOL (n={e['dev_sold_within_30s']['n']}), "
                   f"vs {e['dev_held_10m']['median_peak_sol']} SOL for coins whose dev held 10 min (n={e['dev_held_10m']['n']}). Correlation, not proof.")
    if s['graduated_in_window']:
        out.append(f"The coin completed its bonding curve at about {_s(s['curve_completed_at_s'])}. Trades after that happen on PumpSwap and are not in this report.")
    now = s.get('now') or {}
    if now.get('listed') and now.get('mcap_usd'):
        out.append(f"Now (DexScreener, {now.get('dex')}): market cap ~${now['mcap_usd']:,.0f}, liquidity ~${(now.get('liquidity_usd') or 0):,.0f}.")
    return out


# ---------------------------------------------------------------- recent list (for the API)
def recent(n=50):
    c = db()
    try:
        mints = [r[0] for r in c.execute('select mint from tokens order by ts desc limit ?', (n,)).fetchall()]
    finally:
        c.close()
    if not mints:
        return []
    rows = coin_stats('t.mint in (%s)' % ','.join('?' * len(mints)), mints)
    now = time.time()
    out = [{'mint': r['mint'], 'name': r['name'], 'symbol': r['symbol'], 'created_ts': r['t0'], 'age_s': int(now - r['t0']),
            'window_complete': now - r['t0'] >= WINDOW, 'dev_buy_sol': _r(r['dev_sol'], 3), 'trades': r['trades'],
            'snipers': r['snipers'], 'buyers60': r['buyers60'], 'vol60_sol': _r(r['vol60'], 3),
            'peak_mcap_sol': _r(r['peak'], 2), 'dev_first_sell_s': _r(r['dev_sell'], 1), 'collector': 'v2' if r['v2'] else 'v1',
            'quote': r['quote']} for r in rows]
    return sorted(out, key=lambda x: -x['created_ts'])


# ---------------------------------------------------------------- text output
def text(r):
    if not r.get('ok'):
        return f"Launch Report: {r['mint']}\n  ERROR: {r.get('error')}"
    s, bm, dv, cp = r['summary'], r['buyer_mix'], r['dev'], r['comparison']
    L = []
    w = L.append
    w(f"LAUNCH REPORT  {s['name']} (${s['symbol']})")
    w(f"  mint {s['mint']}")
    w(f"  source: {r['source']} | time basis: {r['time_basis']}" + (f" | {r['coverage_note']}" if r.get('coverage_note') else ''))
    q = s.get('quote')
    if q:
        w(f"  NOTE: paired with {q.get('symbol') or q['mint'][:6]} (not SOL); " + (q.get('note') or f"amounts shown in SOL at the current {q.get('symbol') or 'quote'} price")
          + ('' if q.get('sol_per_quote') else ' - price unavailable, SOL figures are 0'))
    if not s['window_complete']:
        w('  NOTE: coin is younger than 10 min, report is partial')
    w('')
    w('1. SUMMARY')
    w(f"  creator        {s['creator']}")
    if s.get('payer') and s['payer'] != s['creator']:
        w(f"  fee payer      {s['payer']} (counted as dev)")
    w(f"  created        {s['created_iso']}")
    w(f"  dev buy        {s['dev_buy_sol']} SOL ({s['dev_buy_pct_supply']}% of supply)")
    if s['peak_mcap_sol_10m'] is None:
        w('  peak (10m)     n/a (trades unpriced)')
    else:
      w(f"  peak (10m)     {s['peak_mcap_sol_10m']} SOL" + (f" (~${s['peak_mcap_usd_10m']:,.0f})" if s['peak_mcap_usd_10m'] else '') + f" at {_s(s['time_to_peak_s'])}")
    w(f"  end of window  {s['end_mcap_sol_10m']} SOL, {s['trades_10m']} trades" + (' | left bonding curve' if s['graduated_in_window'] else ''))
    now = s.get('now') or {}
    if now.get('listed'):
        w(f"  now            ${(now.get('mcap_usd') or 0):,.0f} mcap, ${(now.get('liquidity_usd') or 0):,.0f} liq on {now.get('dex')} (DexScreener)")
    else:
        w(f"  now            not listed on DexScreener" + (f" ({now['error']})" if now.get('error') else ''))
    w('')
    w('2. FIRST 60 SECONDS')
    for b, v in bm['buckets'].items():
        w(f"  {b:7s} {v['wallets']:4d} new buyers  {v['buy_sol']:8.3f} SOL")
    w('  tags: ' + ', '.join(f'{k} {v}' for k, v in bm['tag_counts'].items()) + ('' if bm['deep_checked'] else '  (fresh/dev-linked need --deep)'))
    for b in bm['buyers'][:12]:
        sold = f"sold {_s(b['first_sell_s'])}" if b['first_sell_s'] is not None else 'held'
        w(f"   {b['wallet'][:6]}..{b['wallet'][-4:]}  in {_s(b['first_buy_s']):>7}  {b['buy_sol']:7.3f} SOL  {sold:12s} {', '.join(b['tags'])}")
    if len(bm['buyers']) > 12:
        w(f"   ... {len(bm['buyers']) - 12} more")
    if bm['first_exits']:
        w('  first exits:')
        for e in bm['first_exits'][:5]:
            rel = f" ({e['vs_dev_sell_s']:+.1f}s vs dev sell)" if e['vs_dev_sell_s'] is not None else ''
            w(f"   {e['wallet'][:6]}.. sold at {_s(e['sold_at_s'])} after holding {e['held_for_s']}s{rel}")
    w('')
    w('3. DEV')
    if dv['first_sell_s'] is None:
        w(f"  no dev sell in window (bought {dv['bought_sol']} SOL)")
    else:
        w(f"  first sell at {_s(dv['first_sell_s'])}: {dv['first_sell_sol']} SOL ({dv.get('first_sell_pct_of_bag')}% of bag); "
          f"{dv['sell_count']} sells, {dv['sold_sol']} SOL total, net {dv['net_sol_10m']:+} SOL")
        a = dv['after_sell']
        w(f"  buys 30s before/after: {a['buys_30s_before']}/{a['buys_30s_after']} ({a['buy_sol_30s_before']}/{a['buy_sol_30s_after']} SOL); "
          + (f"next buy +{a['next_buy_after_s']}s; " if a['next_buy_after_s'] is not None else 'no buys after; ')
          + (f"buying stopped within {a['buying_stopped_within_s']}s" if a['buying_stopped_within_s'] is not None and a['buys_after_to_10m'] else
             'buying did not stop for 30s+' if a['buys_after_to_10m'] else ''))
    w('')
    w('4. CURVE (10s buckets: buys/sells, unique traders, net SOL, mcap)')
    for c in r['curve'][:12]:
        bar = '#' * min(40, c['buys']) + '-' * min(20, c['sells'])
        w(f"  {c['t']:4d}s {c['buys']:3d}/{c['sells']:<3d} u{c['unique_traders']:<4d} {c['net_sol']:+8.3f}  {str(c['mcap_close_sol']):>7}  {bar}{' <dev sold' if c['dev_sold'] else ''}")
    if len(r['curve']) > 12:
        tail = r['curve'][-1]
        w(f"  ... at {tail['t']}s: {tail['cum_buys']} buys / {tail['cum_sells']} sells, {tail['unique_traders']} traders, cum net {tail['cum_net_sol']:+} SOL")
    w('')
    w(f"5. VS {cp['coins_compared']} COINS FROM {cp['day']}" + ('' if cp.get('same_day') else ' (latest day in DB, not the coin\'s day)'))
    if not cp.get('enough_data'):
        w('  not enough coins in the DB for that day yet (need 30)')
    for k, lab in (('snipers', 'snipers'), ('vol60', '60s volume SOL'), ('buyers60', 'unique 60s buyers'), ('peak', '10m peak SOL')):
        if cp.get(k):
            w(f"  {lab:18s} {cp[k]['value']:>9}  p{cp[k]['percentile']:<5} (day median {cp[k]['day_median']})")
    w('')
    w('6. TAKEAWAYS')
    for t in r['takeaways']:
        w('  - ' + t)
    return '\n'.join(L)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        sys.exit(1)
    for m in args:
        rep = build(m.strip(), deep='--deep' in sys.argv, allow_helius='--no-helius' not in sys.argv)
        print(json.dumps(rep, indent=1, default=str) if '--json' in sys.argv else text(rep))
