"""NOBUY MCP server: gives any MCP agent (Claude, Cursor, ...) a `nobuy_check` tool.

    pip install mcp
    python mcp_server.py            (stdio)

Claude Desktop / Claude Code config:
    {"mcpServers": {"nobuy": {"command": "python", "args": ["/path/to/mcp_server.py"]}}}

The agent's API address is read from https://nobuy.fun/config.js (it changes when the agent restarts).
Set NOBUY_API to pin one.
"""
import json, os, re, urllib.request

SITE = 'https://nobuy.fun'
B58 = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server
except ImportError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as Server

server = Server('nobuy')


def _get(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': 'nobuy-mcp/1'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def _api() -> str:
    if os.environ.get('NOBUY_API'):
        return os.environ['NOBUY_API'].rstrip('/')
    m = re.search(r"api:\s*'([^']*)'", _get(SITE + '/config.js'))
    if not m or not m.group(1):
        raise RuntimeError('The NOBUY agent is off air right now. Try again later or set NOBUY_API.')
    return m.group(1).rstrip('/')


@server.tool()
def nobuy_check(mint: str) -> dict:
    """Check a pump.fun coin before buying it. Returns verdict AVOID, CAUTION, CLEAR or UNKNOWN,
    with reasons: did the dev already sell, how many coins this dev launched before, and how often
    they dumped within 60 seconds. Pass the coin's mint (contract address)."""
    mint = mint.strip()
    if not B58.fullmatch(mint):
        return {'error': 'not a Solana address'}
    return json.loads(_get(f'{_api()}/api/check/{mint}'))


if __name__ == '__main__':
    server.run()
