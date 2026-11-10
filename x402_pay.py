"""The agent's x402 wallet: pay for a 402'd URL, but only if the leash allows the spend.

    python -X utf8 x402_pay.py <url>          one paid call (prints verdict + tx link, never the key)
    python -X utf8 x402_pay.py --balance      agent wallet USDC + SOL

The agent key lives in keys/agent.json (made locally, never printed). Fund it with a little USDC; no SOL needed for
payments because the facilitator pays the network fee.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import leash  # noqa: E402
from x402.buyer import fetch_paid  # noqa: E402
from x402.common import ata, net, units_to_usd  # noqa: E402


def agent_key() -> Keypair:
    return Keypair.from_bytes(bytes(json.load(open(os.path.join(HERE, 'keys', 'agent.json')))))


def gate(purpose: str, payee: str = 'launch intel'):
    """Turns an x402 requirement into a leash 'pay' check. Unknown assets/networks are blocked before the leash."""
    def ask(req, resource):
        n = net()
        if req.get('network') != n['caip2'] or req.get('asset') != n['usdc']:
            return False, [f"Wrong network or asset ({req.get('network')} / {req.get('asset')})."]
        r = leash.check({'type': 'pay', 'to': req['payTo'], 'payee': payee, 'usd': units_to_usd(req['amount']),
                         'purpose': purpose, 'url': resource.get('url')})
        return r['allowed'], r['reasons']
    return ask


def pay(url: str, purpose: str, payee: str = 'launch intel') -> dict:
    return fetch_paid(url, agent_key(), gate(purpose, payee))


def balance() -> dict:
    n = net(); kp = agent_key(); owner = str(kp.pubkey())
    rpc = lambda m, p: httpx.post(n['rpc'], json={'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p}, timeout=20).json().get('result')
    sol = (rpc('getBalance', [owner]) or {}).get('value', 0) / 1e9
    tb = rpc('getTokenAccountBalance', [str(ata(owner, n['usdc']))])
    usdc = float(tb['value']['uiAmountString']) if tb else 0.0
    return {'network': n['name'], 'agent': owner, 'sol': sol, 'usdc': usdc}


if __name__ == '__main__':
    if sys.argv[1:] == ['--balance']:
        print(json.dumps(balance(), indent=1))
    else:
        r = pay(sys.argv[1], 'manual test call')
        out = {k: r.get(k) for k in ('status', 'paid', 'blocked', 'settlement')}
        b = r.get('body') or {}
        out['body'] = b if not r.get('paid') else {'paid': b.get('paid'), 'verdict': (b.get('data') or {}).get('verdict')}
        print(json.dumps(out, indent=1))
