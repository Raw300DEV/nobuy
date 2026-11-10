"""LEASH: a spend firewall for a trading agent. Every action the agent wants to take (buy a coin, pay for data)
goes through check() first and comes back ALLOWED or BLOCKED with plain-English reasons. Nothing spends without it.

State (spend today, open positions, loss streak, kill switch) is kept in data/state.json so limits survive restarts.
"""
from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('LEASH_DATA') or os.path.join(HERE, 'data')
os.makedirs(DATA, exist_ok=True)
STATE = os.path.join(DATA, 'state.json')
CONFIG = os.path.join(HERE, 'config.json')

DEFAULTS = {
    'mode': 'paper',                 # paper | live (live needs the user's explicit go; agent.py refuses otherwise)
    'daily_sol': 0.5,                # total SOL the agent may put into buys per day
    'max_trade_sol': 0.05,           # per buy
    'max_open': 3,                   # open positions at once
    'daily_data_usd': 0.25,          # x402 data budget per day
    'max_data_call_usd': 0.01,       # per paid data call
    'loss_streak_pause': 4,          # this many losing exits in a row ...
    'pause_minutes': 30,             # ... pauses buying this long
    'allowed_venues': ['pump.fun', 'pumpswap'],
    'block_dev_sold': True,          # never buy after the dev already sold
    'block_factory_dev': 100,        # dev wallets with this many prior launches are factories
    'block_mayhem': True,
    'trusted_payees': {},            # name -> address, for x402 data sellers; lookalikes get blocked
}

_lock = threading.Lock()
clock = time.time   # replay mode swaps this for a virtual clock


def config() -> dict:
    c = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        c.update(json.load(open(CONFIG, encoding='utf8')))
    return c


def _today():
    return time.strftime('%Y-%m-%d', time.localtime(clock()))


def state() -> dict:
    s = json.load(open(STATE, encoding='utf8')) if os.path.exists(STATE) else {}
    if s.get('day') != _today():
        s.update({'day': _today(), 'spent_sol': 0.0, 'spent_data_usd': 0.0, 'allowed': 0, 'blocked': 0})
    s.setdefault('open', {})
    s.setdefault('loss_streak', 0)
    s.setdefault('paused_until', 0)
    s.setdefault('killed', False)
    s.setdefault('closed', [])
    return s


def save(s: dict):
    tmp = STATE + '.tmp'
    json.dump(s, open(tmp, 'w', encoding='utf8'), indent=1)
    os.replace(tmp, STATE)


def lookalike(a: str, b: str) -> bool:
    """Same first 3 and last 3 characters but a different address: the address-poisoning shape."""
    return a != b and len(a) > 30 and len(b) > 30 and a[:3] == b[:3] and a[-3:] == b[-3:]


def check(action: dict, commit: bool = True) -> dict:
    """action: {'type': 'buy', 'mint', 'symbol', 'sol', 'venue', 'facts': {...}}
            or {'type': 'pay', 'to', 'payee', 'usd', 'purpose'}
    Returns {'allowed': bool, 'reasons': [str], 'action': action}. When allowed and commit, the spend is booked."""
    cfg = config()
    with _lock:
        s = state()
        why = []
        if s['killed']:
            why.append('Kill switch is on.')
        if action['type'] == 'buy':
            f = action.get('facts', {})
            sol = float(action.get('sol', 0))
            if action.get('venue') not in cfg['allowed_venues']:
                why.append(f"Venue {action.get('venue')} isn't on the allowed list.")
            if sol > cfg['max_trade_sol'] + 1e-12:
                why.append(f"{sol:g} SOL is over the {cfg['max_trade_sol']:g} SOL per-trade limit.")
            if s['spent_sol'] + sol > cfg['daily_sol'] + 1e-12:
                why.append(f"Daily limit hit ({s['spent_sol']:.2f} / {cfg['daily_sol']:.2f} SOL).")
            if len(s['open']) >= cfg['max_open']:
                why.append(f"Already holding {len(s['open'])} coins (max {cfg['max_open']}).")
            if action.get('mint') in s['open']:
                why.append('Already holding this coin.')
            if clock() < s['paused_until']:
                why.append(f"Paused after {cfg['loss_streak_pause']} losses in a row, until {time.strftime('%H:%M', time.localtime(s['paused_until']))}.")
            if cfg['block_dev_sold'] and f.get('dev_sold'):
                why.append('The dev already sold.')
            if cfg['block_factory_dev'] and (f.get('prior') or 0) >= cfg['block_factory_dev']:
                why.append(f"Factory dev: {f['prior']:,} coins launched before this one.")
            if cfg['block_mayhem'] and f.get('mayhem'):
                why.append('Mayhem-mode coin (different curve).')
        elif action['type'] == 'pay':
            usd = float(action.get('usd', 0))
            if usd > cfg['max_data_call_usd'] + 1e-12:
                why.append(f"${usd:g} is over the ${cfg['max_data_call_usd']:g} per-call limit.")
            if s['spent_data_usd'] + usd > cfg['daily_data_usd'] + 1e-12:
                why.append(f"Daily data budget used (${s['spent_data_usd']:.3f} / ${cfg['daily_data_usd']:.2f}).")
            trusted = cfg['trusted_payees']
            to = action.get('to', '')
            if action.get('payee') in trusted and trusted[action['payee']] != to:
                why.append(f"Pay-to address isn't the one on file for {action['payee']}.")
            for name, addr in trusted.items():
                if lookalike(addr, to):
                    why.append(f"Pay-to address is a lookalike of {name}'s address (same start and end). Possible poisoning.")
            if to and to not in trusted.values():
                why.append('Pay-to address is not on the trusted list.')
        else:
            why.append(f"Unknown action type {action['type']}.")
        ok = not why
        if commit:
            if ok:
                s['allowed'] += 1
                if action['type'] == 'buy':
                    s['spent_sol'] += float(action['sol'])
                else:
                    s['spent_data_usd'] += float(action['usd'])
            else:
                s['blocked'] += 1
            save(s)
        return {'allowed': ok, 'reasons': why, 'action': action}


def open_position(mint: str, pos: dict):
    with _lock:
        s = state(); s['open'][mint] = pos; save(s)


def close_position(mint: str, result: dict):
    cfg = config()
    with _lock:
        s = state()
        s['open'].pop(mint, None)
        s['closed'] = (s['closed'] + [result])[-500:]
        s['loss_streak'] = s['loss_streak'] + 1 if result['pnl_sol'] < 0 else 0
        if s['loss_streak'] >= cfg['loss_streak_pause']:
            s['paused_until'] = clock() + 60 * cfg['pause_minutes']
            s['loss_streak'] = 0
        save(s)


def kill(on: bool = True):
    with _lock:
        s = state(); s['killed'] = on; save(s)
