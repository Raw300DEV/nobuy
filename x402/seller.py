"""Seller side: put a price on a FastAPI route.

    from x402.seller import Paywall
    wall = Paywall(pay_to=<merchant pubkey>)

    @app.get('/x402/devcheck/{mint}')
    async def devcheck(mint: str, request: Request):
        return await wall.serve(request, 0.005, 'Dev check for one pump.fun coin', lambda: build_answer(mint))

No PAYMENT-SIGNATURE header -> 402 with the price in PAYMENT-REQUIRED.
With one -> facilitator /verify, run the handler, facilitator /settle, then 200 + data + PAYMENT-RESPONSE (tx sig).
The handler only runs on a verified payment, and the data is only returned after settlement lands.
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from .common import FACILITATOR, H_REQUIRED, H_RESPONSE, H_SIGNATURE, b64d, b64e, net, usd_to_units

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'x402_sales.jsonl')


class Paywall:
    def __init__(self, pay_to: str, network: str | None = None, facilitator: str = FACILITATOR):
        self.pay_to = pay_to
        self.net = net(network)
        self.fac = facilitator.rstrip('/')
        self._fee_payer = None
        self._fp_at = 0
        self._lock = threading.Lock()

    async def fee_payer(self) -> str:
        """The facilitator's sponsor key for this network, from /supported (cached 10 min)."""
        if self._fee_payer and time.time() - self._fp_at < 600:
            return self._fee_payer
        async with httpx.AsyncClient(timeout=15) as c:
            kinds = (await c.get(self.fac + '/supported')).json()['kinds']
        for k in kinds:
            if k.get('x402Version') == 2 and k.get('scheme') == 'exact' and k.get('network') == self.net['caip2']:
                self._fee_payer, self._fp_at = k['extra']['feePayer'], time.time()
                return self._fee_payer
        raise RuntimeError(f"facilitator has no exact/v2 for {self.net['caip2']}")

    async def requirements(self, request: Request, usd: float, description: str) -> dict:
        req = {
            'scheme': 'exact', 'network': self.net['caip2'], 'amount': str(usd_to_units(usd)), 'asset': self.net['usdc'],
            'payTo': self.pay_to, 'maxTimeoutSeconds': 60,
            'extra': {'feePayer': await self.fee_payer(), 'name': 'USDC'},
        }
        resource = {'url': str(request.url), 'description': description, 'mimeType': 'application/json'}
        return {'x402Version': 2, 'resource': resource, 'accepts': [req]}

    def _402(self, pr: dict, error: str, extra_headers: dict | None = None):
        body = {**pr, 'error': error}
        h = {H_REQUIRED: b64e(body), 'Access-Control-Expose-Headers': f'{H_REQUIRED}, {H_RESPONSE}'}
        h.update(extra_headers or {})
        return JSONResponse(body, status_code=402, headers=h)

    async def serve(self, request: Request, usd: float, description: str, handler):
        pr = await self.requirements(request, usd, description)
        sig = request.headers.get(H_SIGNATURE) or request.headers.get('X-PAYMENT')
        if not sig:
            return self._402(pr, f'{H_SIGNATURE} header is required')
        try:
            payload = b64d(sig)
        except Exception:
            return self._402(pr, 'payment header is not base64 JSON')
        mine = pr['accepts'][0]
        acc = payload.get('accepted') or {}
        # our own requirements are what get verified; the client's copy only has to agree on what matters
        for k in ('scheme', 'network', 'amount', 'asset', 'payTo'):
            if str(acc.get(k)) != str(mine[k]):
                return self._402(pr, f'payment is for a different {k}')
        body = {'x402Version': 2, 'paymentPayload': payload, 'paymentRequirements': mine}
        async with httpx.AsyncClient(timeout=45) as c:
            v = (await c.post(self.fac + '/verify', json=body)).json()
            if not v.get('isValid'):
                return self._402(pr, f"verify failed: {v.get('invalidReason') or v}")
            data = handler()
            s = (await c.post(self.fac + '/settle', json=body)).json()
        if not s.get('success'):
            return self._402(pr, f"settle failed: {s.get('errorReason') or s}", {H_RESPONSE: b64e(s)})
        rec = {'t': round(time.time(), 1), 'usd': usd, 'resource': pr['resource']['url'], 'payer': s.get('payer') or v.get('payer'),
               'tx': s.get('transaction'), 'network': s.get('network')}
        with self._lock, open(LOG, 'a', encoding='utf8') as f:
            f.write(json.dumps(rec) + '\n')
        return JSONResponse({'data': data, 'paid': {'usd': usd, 'tx': s.get('transaction'),
                                                    'explorer': self.net['explorer'].format(sig=s.get('transaction'))}},
                            headers={H_RESPONSE: b64e(s), 'Access-Control-Expose-Headers': H_RESPONSE})
