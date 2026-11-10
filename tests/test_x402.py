"""x402 + leash tests (offline; no funds needed).   python -X utf8 tests/test_x402.py

1. The payment tx we build decodes to the exact layout the facilitator's fast path wants
   (compute limit, compute price, USDC TransferChecked to the payTo ATA, memo), facilitator as fee payer,
   only the agent's signature filled in, fee payer never touches an instruction.
2. The leash gate blocks: over-price, lookalike payTo (address poisoning), untrusted payTo, wrong asset, wrong network.
3. The leash gate allows a correct call to the trusted seller.
"""
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ['LEASH_DATA'] = tempfile.mkdtemp()
os.environ['LEASH_X402_NETWORK'] = 'devnet'

from solders.keypair import Keypair  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402
import base64  # noqa: E402

import leash  # noqa: E402
import x402_pay  # noqa: E402
from x402.buyer import build_payment_tx  # noqa: E402
from x402.common import COMPUTE_BUDGET, MEMO_PROGRAM, TOKEN_PROGRAM, ata, net  # noqa: E402

N = net('devnet')
TRUSTED = leash.config()['trusted_payees']['launch intel']
FEE_PAYER = '2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4'
BH = 'DHW8Biy2fpAxdU8gxhkXBum8jud7w49mYGkce6Lr2t1L'
ok = fail = 0


def t(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(('PASS ' if cond else 'FAIL ') + name)


def req(**kw):
    r = {'scheme': 'exact', 'network': N['caip2'], 'amount': '5000', 'asset': N['usdc'], 'payTo': TRUSTED,
         'maxTimeoutSeconds': 60, 'extra': {'feePayer': FEE_PAYER, 'recentBlockhash': BH}}
    r.update(kw)
    return r


# 1. tx layout
payer = Keypair()
tx = VersionedTransaction.from_bytes(base64.b64decode(build_payment_tx(req(), payer)))
m = tx.message
keys = list(m.account_keys)
progs = [keys[ix.program_id_index] for ix in m.instructions]
t('4 instructions in fast-path order', progs == [COMPUTE_BUDGET, COMPUTE_BUDGET, TOKEN_PROGRAM, MEMO_PROGRAM])
t('fee payer is the facilitator (account 0)', str(keys[0]) == FEE_PAYER)
t('2 signers: facilitator + agent', m.header.num_required_signatures == 2 and keys[1] == payer.pubkey())
t('facilitator signature left empty, agent signed', tx.signatures[0] == Signature.default() and tx.signatures[1] != Signature.default())
tr = m.instructions[2]
amount, dec = struct.unpack('<QB', bytes(tr.data)[1:10])
t('TransferChecked 5000 units, 6 decimals', bytes(tr.data)[0] == 12 and amount == 5000 and dec == 6)
acc = [keys[i] for i in tr.accounts]
t('from agent ATA to the payTo ATA, agent is authority',
  acc[0] == ata(payer.pubkey(), N['usdc']) and acc[2] == ata(TRUSTED, N['usdc']) and acc[3] == payer.pubkey())
t('fee payer appears in no instruction', all(0 not in list(ix.accounts) for ix in m.instructions))
cu_price = struct.unpack('<Q', bytes(m.instructions[1].data)[1:9])[0]
t('compute price under the 5 lamports/CU cap', cu_price <= 5_000_000)
t('memo is a 32-char hex nonce', len(bytes(m.instructions[3].data)) == 32)

# 2-3. leash gate
gate = x402_pay.gate('test')
res = {'url': 'http://x/test'}
lookalike = TRUSTED[:3] + 'X' * (len(TRUSTED) - 6) + TRUSTED[-3:]
allowed, why = gate(req(), res)
t('correct call to trusted seller is ALLOWED', allowed)
allowed, why = gate(req(amount='50000'), res)
t('$0.05 call BLOCKED (over $0.01 cap)', not allowed and any('per-call limit' in w for w in why))
allowed, why = gate(req(payTo=lookalike), res)
t('lookalike payTo BLOCKED as poisoning', not allowed and any('lookalike' in w for w in why))
allowed, why = gate(req(payTo=str(Keypair().pubkey())), res)
t('random payTo BLOCKED (not trusted)', not allowed and any('trusted' in w for w in why))
allowed, why = gate(req(asset='So11111111111111111111111111111111111111112'), res)
t('wrong asset BLOCKED', not allowed)
allowed, why = gate(req(network=net('mainnet')['caip2']), res)
t('wrong network BLOCKED', not allowed)
s = leash.state()
t('daily data spend booked only for the allowed call', abs(s['spent_data_usd'] - 0.005) < 1e-9)
leash.kill(True)
allowed, why = gate(req(), res)
t('kill switch BLOCKS every payment', not allowed and any('Kill switch' in w for w in why))

print(f'\n{ok}/{ok + fail} passed')
sys.exit(1 if fail else 0)
