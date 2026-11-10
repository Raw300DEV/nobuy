"""LEASH web: public, read-only view of the agent and its leash.

    python -X utf8 -m uvicorn app:app --host 127.0.0.1 --port 8799     (from products/leash)
    LEASH_DATA=<dir> to serve another data dir (e.g. data_replay for a demo)
"""
import json, os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import leash
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title='LEASH')
from fastapi.middleware.cors import CORSMiddleware
# Public-tunnel hardening: GET/OPTIONS only, per-visitor rate limit (Cloudflare passes the visitor IP in CF-Connecting-IP).
import collections as _c
RATE_PER_MIN = 90
_hits = _c.defaultdict(list)
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import PlainTextResponse
class _Guard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return PlainTextResponse('read-only', status_code=405)
        ip = request.headers.get('cf-connecting-ip') or (request.client.host if request.client else '?')
        now = time.time(); h = [t for t in _hits[ip] if now - t < 60]; h.append(now); _hits[ip] = h
        if len(h) > RATE_PER_MIN:
            return PlainTextResponse('slow down', status_code=429)
        if len(_hits) > 5000:
            _hits.clear()
        return await call_next(request)
app.add_middleware(_Guard)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET'], allow_headers=['PAYMENT-SIGNATURE', 'X-PAYMENT'],
                   expose_headers=['PAYMENT-REQUIRED', 'PAYMENT-RESPONSE'])

# ---- x402: the paid data endpoint the agent (and anyone else) buys from
import intel
from x402.seller import Paywall, LOG as SALES_LOG
from x402.common import net as x402_net


def _merchant():
    if os.environ.get('LEASH_PAY_TO'):
        return os.environ['LEASH_PAY_TO']
    from solders.keypair import Keypair
    return str(Keypair.from_bytes(bytes(json.load(open(os.path.join(HERE, 'keys', 'merchant.json'))))).pubkey())


PAY_TO = _merchant()
wall = Paywall(PAY_TO)


@app.get('/x402/devcheck/{mint}')
async def x402_devcheck(mint: str, request: Request):
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', mint):
        return JSONResponse({'error': 'not a Solana address'}, status_code=400)
    return await wall.serve(request, intel.DEVCHECK_USD, 'ONLEASH dev check: dev record, dev sells, verdict for one pump.fun coin',
                            lambda: intel.devcheck(mint))


_chk = _c.defaultdict(list)


@app.get('/api/check/{mint}')
def free_check(mint: str, request: Request):
    """Free check for people on the site (8 a minute per visitor). Agents use the paid /x402/devcheck."""
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', mint):
        return JSONResponse({'error': 'not a Solana address'}, status_code=400)
    ip = request.headers.get('cf-connecting-ip') or (request.client.host if request.client else '?')
    now = time.time(); h = [t for t in _chk[ip] if now - t < 60]; h.append(now); _chk[ip] = h
    if len(h) > 8:
        return JSONResponse({'error': 'slow down'}, status_code=429)
    if len(_chk) > 5000:
        _chk.clear()
    r = intel.devcheck(mint)
    return {k: r.get(k) for k in ('mint', 'found', 'symbol', 'name', 'age_s', 'verdict', 'why', 'coin')}


@app.get('/x402/price')
def x402_price():
    n = x402_net()
    return {'network': n['caip2'], 'asset': 'USDC', 'pay_to': PAY_TO, 'facilitator': wall.fac,
            'endpoints': {'/x402/devcheck/{mint}': intel.DEVCHECK_USD}}


@app.get('/api/payments')
def payments(n: int = 50):
    if not os.path.exists(SALES_LOG):
        return []
    rows = [json.loads(l) for l in open(SALES_LOG, encoding='utf8') if l.strip()][-min(n, 200):][::-1]
    ex = x402_net()['explorer']
    return [{**r, 'explorer': ex.format(sig=r['tx'])} for r in rows]
FEED = os.path.join(leash.DATA, 'feed.jsonl')
STATS = os.path.join(leash.DATA, 'stats.json')
PUBLIC_CFG = ['mode', 'daily_sol', 'max_trade_sol', 'max_open', 'daily_data_usd', 'max_data_call_usd', 'loss_streak_pause',
              'pause_minutes', 'allowed_venues', 'block_dev_sold', 'block_factory_dev', 'block_mayhem', 'trusted_payees']


BLOCK = re.compile(r'(PUSS|DICK|COCK|CUM|FUCK|SHIT|NIG|FAG|RAPE|PORN|SEX|TITS|TITTY|BOOB|ANAL|CUNT|WHORE|SLUT|HITLER|NAZI|JEW|RETARD)', re.I)


def clean(e):
    for k in ('symbol', 'name'):
        if e.get(k) and BLOCK.search(e[k]):
            e[k] = '(filtered)'
    return e


def tail(n=120):
    if not os.path.exists(FEED):
        return []
    with open(FEED, 'rb') as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 200_000))
        lines = f.read().decode('utf8', 'ignore').splitlines()[1:]
    return [clean(json.loads(l)) for l in lines[-n:] if l.strip()][::-1]


@app.get('/api/state')
def state():
    s = leash.state(); cfg = leash.config()
    st = json.load(open(STATS)) if os.path.exists(STATS) else {}
    closed = s.get('closed', [])
    return JSONResponse({
        'now': time.time(), 'config': {k: cfg[k] for k in PUBLIC_CFG},
        'today': {'day': s['day'], 'spent_sol': round(s['spent_sol'], 4), 'spent_data_usd': round(s['spent_data_usd'], 4),
                  'allowed': s['allowed'], 'blocked': s['blocked']},
        'open': [clean({'mint': m, **{k: v for k, v in p.items() if k != 'devs'}}) for m, p in s['open'].items()],
        'paused_until': s['paused_until'], 'killed': s['killed'],
        'stats': st, 'recent_closed': closed[-20:][::-1],
    })


@app.get('/api/feed')
def feed(n: int = 120):
    return JSONResponse(tail(min(n, 400)))


@app.get('/')
def index():
    return FileResponse(os.path.join(HERE, 'web', 'index.html'))
