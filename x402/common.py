"""x402 v2 on Solana (exact scheme): shared constants and helpers.

Spec: github.com/coinbase/x402 specs/x402-specification-v2.md, specs/transports-v2/http.md,
      specs/schemes/exact/scheme_exact_svm.md
Facilitator: PayAI (facilitator.payai.network) signs as feePayer and pays the network fee, so neither the agent
nor our server needs SOL for a payment. 1,000 free credits, ~1.18 credits per mainnet settle (11-08).
"""
from __future__ import annotations

import base64
import json
import os

from solders.pubkey import Pubkey

FACILITATOR = os.environ.get('X402_FACILITATOR', 'https://facilitator.payai.network')

NETWORKS = {
    'mainnet': {'caip2': 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp', 'rpc': 'https://api.mainnet-beta.solana.com',
                'usdc': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'explorer': 'https://solscan.io/tx/{sig}'},
    'devnet': {'caip2': 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1', 'rpc': 'https://api.devnet.solana.com',
               'usdc': '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU', 'explorer': 'https://solscan.io/tx/{sig}?cluster=devnet'},
}
USDC_DECIMALS = 6

TOKEN_PROGRAM = Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
ATA_PROGRAM = Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL')
COMPUTE_BUDGET = Pubkey.from_string('ComputeBudget111111111111111111111111111111')
MEMO_PROGRAM = Pubkey.from_string('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr')

H_REQUIRED = 'PAYMENT-REQUIRED'      # server -> client, 402
H_SIGNATURE = 'PAYMENT-SIGNATURE'    # client -> server, the signed payment
H_RESPONSE = 'PAYMENT-RESPONSE'      # server -> client, settlement result


def net(name: str | None = None) -> dict:
    if not name:
        name = os.environ.get('LEASH_X402_NETWORK')
    if not name:   # fall back to the leash config's x402_mode (paper -> devnet for the seller side)
        try:
            cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json'), encoding='utf8'))
            name = cfg.get('x402_mode') if cfg.get('x402_mode') in NETWORKS else 'devnet'
        except Exception:
            name = 'devnet'
    return {'name': name, **NETWORKS[name]}


def b64e(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()


def b64d(s: str):
    return json.loads(base64.b64decode(s))


def ata(owner: Pubkey | str, mint: Pubkey | str) -> Pubkey:
    owner = Pubkey.from_string(owner) if isinstance(owner, str) else owner
    mint = Pubkey.from_string(mint) if isinstance(mint, str) else mint
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM)[0]


def usd_to_units(usd: float) -> int:
    return int(round(usd * 10 ** USDC_DECIMALS))


def units_to_usd(units: int | str) -> float:
    return int(units) / 10 ** USDC_DECIMALS
