"""mcp_client.py - MCP client used by context_service, Step 2 tests, Step 6 predicates and the gateway.

Builds real JSON-RPC 2.0 messages. Default transport is in-process (serialise -> server
handler -> parse); MCP_TRANSPORT=http sends the same bytes to POST /mcp with a signed session
header. Tests can inject any transport callable(raw_json, headers) -> raw_json.

Production: official MCP client SDK over streamable HTTP with OAuth.
"""
import itertools
import json
import os

from saathi.core import logger
from saathi.mcp import mcp_server

_ids = itertools.count(1)


class MCPError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def inprocess_transport(merchant_id):
    def send(raw, headers):
        return mcp_server.handle_json(raw, {"merchant_id": merchant_id})
    return send


def http_transport(base_url=None):
    import requests

    def send(raw, headers):
        url = (base_url or os.environ.get("MCP_BASE_URL") or "http://127.0.0.1:5000").rstrip("/") + "/mcp"
        r = requests.post(url, data=raw.encode("utf-8"), headers={**headers, "Content-Type": "application/json"}, timeout=15)
        return r.text
    return send


def _fmt_args(args):
    parts = []
    for k, v in args.items():
        if k == "gateway_token":
            parts.append("token:✓")
        else:
            parts.append(f'{k}:{json.dumps(v, ensure_ascii=False)}')
    return "{" + ", ".join(parts) + "}"


class MCPClient:
    def __init__(self, merchant_id, transport=None, quiet=False):
        self.merchant_id = merchant_id
        self.quiet = quiet
        mode = os.environ.get("MCP_TRANSPORT", "inprocess")
        self.transport_name = "http" if (transport is None and mode == "http") else ("custom" if transport else "inprocess")
        if transport is not None:
            self._send = transport
        elif mode == "http":
            self._send = http_transport()
        else:
            self._send = inprocess_transport(merchant_id)
        self.calls = 0

    def _rpc(self, method, params=None):
        req = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
        if params is not None:
            req["params"] = params
        headers = {"X-Saathi-Session": mcp_server.session_token(self.merchant_id)}
        raw = self._send(json.dumps(req, ensure_ascii=False), headers)
        resp = json.loads(raw)
        if resp.get("id") != req["id"]:
            raise MCPError(-32603, "response id mismatch")
        if "error" in resp:
            raise MCPError(resp["error"]["code"], resp["error"]["message"])
        return resp["result"]

    def initialize(self):
        return self._rpc("initialize", {"protocolVersion": mcp_server.PROTOCOL_VERSION,
                                        "clientInfo": {"name": "saathi-agent", "version": "0.1.0"}, "capabilities": {}})

    def list_tools(self):
        return self._rpc("tools/list")["tools"]

    def call(self, name, args=None):
        idf = "customer_id" if name in mcp_server.CUSTOMER_TOOLS else "merchant_id"
        args = {idf: self.merchant_id, **(args or {})}
        if not self.quiet:
            logger.log("mcp-client", f"→ tools/call {name} {_fmt_args(args)}" + (f" [{self.transport_name}]" if self.transport_name == "http" else ""))
        self.calls += 1
        try:
            res = self._rpc("tools/call", {"name": name, "arguments": args})
        except MCPError as e:
            logger.log("mcp-client", f"← {name} error {e.code}: {e.message}", level="warn")
            raise
        return res["structuredContent"]
