# NOBUY

An AI agent checks every new pump.fun coin at second ten and blocks the ones built to dump on you: the dev already sold, factory devs, mayhem traps. Every spend, coin buys and paid data calls alike, goes through a firewall first and is logged with its reason.

Live: https://nobuy.fun · API docs: https://nobuy.fun/docs.html

## Check a coin

| Endpoint | Price | Use |
|---|---|---|
| `GET /api/check/{mint}` | free, 8/min per IP | people, light agents |
| `GET /x402/devcheck/{mint}` | $0.005 USDC over [x402](https://www.x402.org/) | agents, no key or account |
| `GET /x402/price` | free | network, pay-to, prices |

Verdicts: `AVOID` (dev sold / factory dev / dumps inside 60s on most coins), `CAUTION` (repeat dev, mayhem coin), `CLEAR` (no red flags, not a promise), `UNKNOWN` (not in our launch data).

## MCP

```
pip install mcp
python mcp_server.py
```
```json
{ "mcpServers": { "nobuy": { "command": "python", "args": ["/path/to/mcp_server.py"] } } }
```
Gives any MCP client a `nobuy_check(mint)` tool.

## Files

| File | What it does |
|---|---|
| `leash.py` | The firewall. `check(action)` returns ALLOWED or BLOCKED with plain reasons. Limits live in `config.json`. |
| `agent.py` | Paper agent. Decides on every new coin at +10s, pays for data over x402 only through the firewall. |
| `intel.py` | The dev check: dev sells, prior launches, how often the dev dumped inside 60s. |
| `x402/` | x402 seller (paywall) and buyer, Solana USDC, PayAI facilitator. |
| `app.py` | Public read-only API (FastAPI): state, feed, payments, checks. |
| `mcp_server.py` | MCP tool wrapper. |
| `backtest.py` | Buy rules tested on every recorded launch. |
| `site/` | nobuy.fun. |

Data comes from our own pump.fun launch collector (not in this repo). `intel.py` reads it through `launchreport/report.py`.

## What we found

`backtest.py` ran every buy rule over 18,063 recorded launches. Every rule lost 2-7% per trade after fees. So the agent runs in paper mode, and the product is the part that held up: knowing which coins not to buy.

NOBUY is a memecoin, no promises. Not financial advice.
