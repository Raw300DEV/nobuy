"""Buyer side: the agent pays for a 402'd URL, but only if the leash says yes first.

    from x402.buyer import fetch_paid
    r = fetch_paid(url, keypair, ask=leash_gate)   # ask(requirement) -> (ok, reasons)

Flow: GET -> 402 + PAYMENT-REQUIRED -> ask the leash (price caps, daily budget, trusted payee, lookalike check)
-> build a partially signed v0 tx (compute limit, compute price, USDC TransferChecked, memo nonce) with the
facilitator as fee payer -> retry with PAYMENT-SIGNATURE -> 200 + data + settlement tx sig.
Nothing is signed if the leash blocks.
"""
from __future__ import annotations

import secrets
import struct

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from .common import (COMPUTE_BUDGET, H_REQUIRED, H_RESPONSE, H_SIGNATURE, MEMO_PROGRAM, NETWORKS, TOKEN_PROGRAM,
                     USDC_DECIMALS, ata, b64d, b64e)

CU_LIMIT = 40_000
CU_PRICE_MICROLAMPORTS = 1          # well under the facilitator's 5 lamports/CU fast-path cap


def _rpc(url: str, method: str, params: list):
    r = httpx.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, timeout=20).json()
    if 'error' in r:
        raise RuntimeError(r['error'])
    return r['result']


def rpc_for(caip2: str) -> str:
    return next(n['rpc'] for n in NETWORKS.values() if n['caip2'] == caip2)


def build_payment_tx(req: dict, payer: Keypair, rpc: str | None = None) -> str:
    """Base64 partially signed versioned tx paying req['amount'] of req['asset'] to req['payTo']."""
    fee_payer = Pubkey.from_string(req['extra']['feePayer'])
    mint = Pubkey.from_string(req['asset'])
    owner = payer.pubkey()
    if fee_payer == owner:
        raise ValueError('fee payer cannot be the paying wallet')
    bh = req['extra'].get('recentBlockhash') or _rpc(rpc or rpc_for(req['network']), 'getLatestBlockhash',
                                                      [{'commitment': 'finalized'}])['value']['blockhash']
    decimals = int(req['extra'].get('decimals', USDC_DECIMALS))
    memo = (req['extra'].get('memo') or secrets.token_hex(16)).encode()
    ixs = [
        Instruction(COMPUTE_BUDGET, bytes([2]) + struct.pack('<I', CU_LIMIT), []),
        Instruction(COMPUTE_BUDGET, bytes([3]) + struct.pack('<Q', CU_PRICE_MICROLAMPORTS), []),
        Instruction(TOKEN_PROGRAM, bytes([12]) + struct.pack('<QB', int(req['amount']), decimals), [
            AccountMeta(ata(owner, mint), False, True),
            AccountMeta(mint, False, False),
            AccountMeta(ata(req['payTo'], mint), False, True),
            AccountMeta(owner, True, False),
        ]),
        Instruction(MEMO_PROGRAM, memo, []),
    ]
    msg = MessageV0.try_compile(fee_payer, ixs, [], Hash.from_string(bh))
    keys = list(msg.account_keys)[:msg.header.num_required_signatures]
    sigs = [payer.sign_message(to_bytes_versioned(msg)) if k == owner else Signature.default() for k in keys]
    tx = VersionedTransaction.populate(msg, sigs)
    import base64
    return base64.b64encode(bytes(tx)).decode()


def fetch_paid(url: str, payer: Keypair, ask, method: str = 'GET', timeout: float = 60) -> dict:
    """Returns {'status', 'paid': bool, 'blocked': reasons|None, 'requirement', 'settlement', 'body'}."""
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url)
        if r.status_code != 402:
            return {'status': r.status_code, 'paid': False, 'blocked': None, 'body': _json(r)}
        pr = b64d(r.headers[H_REQUIRED]) if H_REQUIRED in r.headers else r.json()
        req = pr['accepts'][0]
        ok, reasons = ask(req, pr.get('resource') or {})
        if not ok:
            return {'status': 402, 'paid': False, 'blocked': reasons, 'requirement': req, 'body': None}
        payload = {'x402Version': 2, 'resource': pr.get('resource'), 'accepted': req,
                   'payload': {'transaction': build_payment_tx(req, payer)}}
        r2 = c.request(method, url, headers={H_SIGNATURE: b64e(payload)})
        settle = b64d(r2.headers[H_RESPONSE]) if H_RESPONSE in r2.headers else None
        return {'status': r2.status_code, 'paid': r2.status_code == 200, 'blocked': None, 'requirement': req,
                'settlement': settle, 'body': _json(r2)}


def _json(r):
    try:
        return r.json()
    except Exception:
        return r.text[:500]
