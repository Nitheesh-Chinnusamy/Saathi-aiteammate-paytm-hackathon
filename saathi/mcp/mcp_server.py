"""mcp_server.py - minimal MCP-style tool server: JSON-RPC 2.0 with initialize, tools/list,
tools/call. It is the ONLY Saathi-side module that queries merchant data tables.

* Read tools are scoped: merchant_id comes from the session and the server rejects any request
  for another merchant's rows (including row ids that belong to someone else).
* Write tools (stop_rental, post_credit, requeue_settlement, remote_restart) additionally need a
  valid HMAC gateway token whose scope covers the merchant, action and amount.
* PII (phone, bank account) is masked before anything leaves the server.

This is a minimal MCP-style server (initialize, tools/list, tools/call); production would use a
full MCP server via the official SDK in front of Paytm's systems.
Production: official MCP SDK server + OAuth-scoped service accounts in front of Paytm APIs.
"""
import hashlib
import hmac
import json
import os
import time

from saathi.sim import anomaly_rules
from saathi.core import logger
from saathi.sim import paytm_sim
from saathi.core import store
from saathi.core.clock import clock, iso
from datetime import timedelta

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "saathi-paytm-tools", "version": "0.1.0"}


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def secret():
    return (os.environ.get("SECRET_KEY") or "saathi-dev-secret").encode()


def session_token(merchant_id):
    sig = hmac.new(secret(), f"session:{merchant_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{merchant_id}.{sig}"


def verify_session_token(tok):
    try:
        mid, sig = tok.rsplit(".", 1)
    except Exception:
        return None
    return mid if hmac.compare_digest(session_token(mid), tok) else None


# ------------------------------------------------------------------ masking
def mask_phone(p):
    return (p[:2] + "x" * (len(p) - 4) + p[-2:]) if p else p


def mask_account(a):
    return ("X" * (len(a) - 4) + a[-4:]) if a else a


def _profile(mid):
    m = store.q1("SELECT id, name, business, city, lang, phone, bank_account, ifsc, bank_name, joined_at, fraud_flag "
                 "FROM merchants WHERE id=?", (mid,))
    if not m:
        raise ToolError(-32004, f"unknown merchant {mid}")
    return m


def _masked_profile(m):
    p = dict(m)
    p["phone"] = mask_phone(p["phone"])
    p["bank_account"] = mask_account(p["bank_account"])
    return p


# ------------------------------------------------------------------ scope checks
def _owns(table, row_id, mid):
    r = store.q1(f"SELECT merchant_id FROM {table} WHERE id=?", (row_id,))
    if r is None:
        raise ToolError(-32004, f"{row_id} not found")
    if r["merchant_id"] != mid:
        raise ToolError(-32001, f"scope violation: {row_id} does not belong to session merchant")


def _since(hours):
    return iso(clock.now() - timedelta(hours=hours))


# ------------------------------------------------------------------ read tools
def t_get_state_card(mid, a):
    t0 = time.perf_counter()
    prof = _profile(mid)
    devices = store.q("SELECT * FROM devices WHERE merchant_id=?", (mid,))
    charges = store.q("SELECT * FROM charges WHERE merchant_id=? AND ts>=? ORDER BY ts", (mid, _since(24 * 30)))
    settlements = store.q("SELECT * FROM settlements WHERE merchant_id=? AND ts>=? ORDER BY ts", (mid, _since(24 * 7)))
    payouts = store.q("SELECT * FROM payouts WHERE merchant_id=?", (mid,))
    tickets = store.q("SELECT * FROM tickets WHERE merchant_id=? AND opened_at>=? ORDER BY opened_at", (mid, _since(24 * 90)))
    pays = store.q("SELECT id, ts, alert_sent FROM payments WHERE merchant_id=? AND ts>=? ORDER BY ts", (mid, _since(24)))
    ms = (time.perf_counter() - t0) * 1000
    logger.log("mcp-server", f"db: devices {len(devices)} row · charges_30d {len(charges)} · settlements_7d {len(settlements)} · "
                             f"tickets_90d {len(tickets)} ({ms:.0f} ms) · scope ✓")
    now = clock.now_iso()
    raw = {"devices": devices, "charges_30d": charges, "settlements_7d": settlements, "payouts": payouts,
           "payments_24h": {"count": len(pays), "last_ts": pays[-1]["ts"] if pays else None,
                            "alerts_missing": sum(1 for p in pays if not p["alert_sent"])}}
    anomalies = anomaly_rules.flag(raw, now)
    card = {"merchant": _masked_profile(prof), "as_of": now, **raw,
            "tickets_90d": [{k: t[k] for k in ("id", "topic", "status", "opened_at", "closed_at", "credit_issued")} for t in tickets],
            "open_anomalies": anomalies}
    tokens = len(json.dumps(card, ensure_ascii=False)) // 4
    logger.log("mcp-server", f"masked 2 PII fields · state card {tokens} tokens · anomalies={[x['rule'] for x in anomalies]}")
    return card


def _rows(table, sql, args, label=None):
    t0 = time.perf_counter()
    rows = store.q(sql, args)
    logger.log("mcp-server", f"db: {label or table} {len(rows)} row{'s' if len(rows) != 1 else ''} "
                             f"({(time.perf_counter() - t0) * 1000:.0f} ms) · scope ✓")
    return {"source": table, "rows": rows, "as_of": clock.now_iso()}


def t_get_devices(mid, a):
    return _rows("devices", "SELECT * FROM devices WHERE merchant_id=?", (mid,))


def t_get_charges(mid, a):
    return _rows("charges", "SELECT * FROM charges WHERE merchant_id=? AND ts>=? ORDER BY ts",
                 (mid, _since(24 * a.get("days", 30))))


def t_get_tickets(mid, a):
    return _rows("tickets", "SELECT id, topic, status, opened_at, closed_at, credit_issued FROM tickets WHERE merchant_id=?", (mid,))


def t_get_billing_events(mid, a):
    if a.get("device_id"):
        _owns("devices", a["device_id"], mid)
        return _rows("billing_events", "SELECT * FROM billing_events WHERE merchant_id=? AND device_id=? ORDER BY ts",
                     (mid, a["device_id"]), f"billing_events[{a['device_id']}]")
    return _rows("billing_events", "SELECT * FROM billing_events WHERE merchant_id=? ORDER BY ts", (mid,))


def t_get_warehouse_scan(mid, a):
    _owns("devices", a["device_id"], mid)
    return _rows("warehouse_scans", "SELECT * FROM warehouse_scans WHERE device_id=? AND merchant_id=?", (a["device_id"], mid),
                 f"warehouse_scans[{a['device_id']}]")


def t_get_settlements(mid, a):
    if a.get("settlement_id"):
        _owns("settlements", a["settlement_id"], mid)
        return _rows("settlements", "SELECT * FROM settlements WHERE id=?", (a["settlement_id"],), f"settlements[{a['settlement_id']}]")
    return _rows("settlements", "SELECT * FROM settlements WHERE merchant_id=? ORDER BY ts", (mid,))


def t_get_ledger_entries(mid, a):
    if a.get("minutes"):
        return _rows("ledger_entries", "SELECT * FROM ledger_entries WHERE merchant_id=? AND ts>=? ORDER BY ts",
                     (mid, iso(clock.now() - timedelta(minutes=a["minutes"]))), f"ledger_entries_{a['minutes']}min")
    if a.get("ref"):
        return _rows("ledger_entries", "SELECT * FROM ledger_entries WHERE merchant_id=? AND ref=?", (mid, a["ref"]),
                     f"ledger_entries[ref={a['ref']}]")
    return _rows("ledger_entries", "SELECT * FROM ledger_entries WHERE merchant_id=? AND ts>=? ORDER BY ts",
                 (mid, _since(24 * a.get("days", 30))))


def t_get_device_heartbeat(mid, a):
    _owns("devices", a["device_id"], mid)
    res = _rows("heartbeats", "SELECT * FROM heartbeats WHERE device_id=? ORDER BY ts DESC LIMIT 5", (a["device_id"],),
                f"heartbeats[{a['device_id']}]")
    dev = store.q1("SELECT last_heartbeat, online FROM devices WHERE id=?", (a["device_id"],))
    res.update(dev)
    return res


def t_get_payments(mid, a):
    if a.get("minutes"):
        return _rows("payments", "SELECT * FROM payments WHERE merchant_id=? AND ts>=? ORDER BY ts",
                     (mid, iso(clock.now() - timedelta(minutes=a["minutes"]))), f"payments_{a['minutes']}min")
    return _rows("payments", "SELECT * FROM payments WHERE merchant_id=? AND ts>=? ORDER BY ts",
                 (mid, _since(a.get("hours", 24))), f"payments_{a.get('hours', 24)}h")


def t_get_credit_queue(mid, a):
    return _rows("finance_queue", "SELECT id, amount, ref, kind, status, created_at, posted_at FROM finance_queue WHERE merchant_id=?",
                 (mid,))


def t_get_payouts(mid, a):
    return _rows("payouts", "SELECT * FROM payouts WHERE merchant_id=?", (mid,))


def t_is_linked_account(mid, a):
    m = store.q1("SELECT linked_vpa, bank_account FROM merchants WHERE id=?", (mid,))
    dest = (a.get("destination") or "").strip().lower()
    linked = bool(dest) and dest in {(m["linked_vpa"] or "").lower(), (m["bank_account"] or "").lower()}
    logger.log("mcp-server", f"linked-account check → {'linked' if linked else 'NOT linked'} · scope ✓")
    return {"source": "merchants", "linked": linked, "as_of": clock.now_iso()}


def mask_vpa(v):
    if not v or "@" not in v:
        return v
    name, host = v.split("@", 1)
    return f"{name[:len(name) // 2]}***@{host}"


def t_get_txn_by_utr(mid, a):
    t0 = time.perf_counter()
    t = store.q1("SELECT * FROM upi_txns WHERE utr=?", (a["utr"].replace(" ", ""),))
    me = store.q1("SELECT linked_vpa FROM merchants WHERE id=?", (mid,))
    ms = (time.perf_counter() - t0) * 1000
    if not t:
        logger.log("mcp-server", f"db: upi_txns[utr] 0 rows ({ms:.0f} ms) · scope ✓")
        return {"source": "upi_switch", "found": False, "rows": [], "as_of": clock.now_iso()}
    mine = bool(me) and t["payee_vpa"] == me["linked_vpa"]
    logger.log("mcp-server", f"db: upi_txns[utr] 1 row ({ms:.0f} ms) · payee {'= session merchant' if mine else '≠ session merchant'} · payer data withheld")
    row = {"id": t["id"], "amount": t["amount"], "status": t["status"], "ts": t["ts"], "payee_vpa": mask_vpa(t["payee_vpa"]),
           "payee_name": t["payee_name"], "payee_is_you": mine}
    return {"source": "upi_switch", "found": True, "rows": [row], "as_of": clock.now_iso()}


def t_get_qr_codes(mid, a):
    return _rows("qr_codes", "SELECT * FROM qr_codes WHERE merchant_id=? ORDER BY created_at", (mid,))


# ---- customer tools (session subject is a C-xxxx customer id)
def _customer(cid):
    c = store.q1("SELECT * FROM customers WHERE id=?", (cid,))
    if not c:
        raise ToolError(-32004, f"unknown customer {cid}")
    return c


def t_get_customer_card(cid, a):
    t0 = time.perf_counter()
    c = _customer(cid)
    txns = store.q("SELECT id, utr, payee_name, payee_vpa, amount, ts, status, bank_name, ifsc, reason, reversal_status, reversal_ref "
                   "FROM upi_txns WHERE customer_id=? AND ts>=? ORDER BY ts", (cid, _since(24 * 7)))
    ms = (time.perf_counter() - t0) * 1000
    logger.log("mcp-server", f"db: customer 1 row · upi_txns_7d {len(txns)} ({ms:.0f} ms) · scope ✓")
    for t in txns:
        t["payee_vpa"] = mask_vpa(t["payee_vpa"])
    raw = {"upi_txns": txns}
    anomalies = anomaly_rules.flag_customer(raw)
    prof = {"id": c["id"], "name": c["name"], "business": "Paytm customer", "city": c["city"], "lang": c["lang"],
            "phone": mask_phone(c["phone"]), "bank_account": mask_account(c["bank_account"]), "bank_name": c["bank_name"],
            "ifsc": c["ifsc"], "fraud_flag": c["fraud_flag"]}
    card = {"merchant": prof, "user_type": "customer", "as_of": clock.now_iso(), "bank": c["bank_name"], **raw,
            "devices": [], "charges_30d": [], "settlements_7d": [], "payouts": [], "tickets_90d": [], "open_anomalies": anomalies}
    logger.log("mcp-server", f"masked 2 PII fields · customer card {len(json.dumps(card, ensure_ascii=False)) // 4} tokens · "
                             f"anomalies={[x['rule'] for x in anomalies]}")
    return card


def t_get_upi_txns(cid, a):
    if a.get("utr"):
        return _rows("upi_txns", "SELECT * FROM upi_txns WHERE customer_id=? AND utr=?", (cid, a["utr"]), "upi_txns[utr]")
    return _rows("upi_txns", "SELECT * FROM upi_txns WHERE customer_id=? ORDER BY ts", (cid,))


def t_get_customer_credits(cid, a):
    return _rows("customer_credits", "SELECT id, amount, ref, kind, ts FROM customer_credits WHERE customer_id=?", (cid,))


# ------------------------------------------------------------------ write tools
def _write(name, mid, a, amount, fn):
    from saathi.steps import step3_safety  # lazy: token crypto lives with the issuer
    ok, reason = step3_safety.verify_token(a.get("gateway_token"), mid, name, amount)
    if not ok:
        logger.log("mcp-server", f"write {name} REJECTED: {reason}", level="warn")
        raise ToolError(-32003, f"unauthorized write: {reason}")
    logger.log("mcp-server", f"write {name} token ✓ scope ✓ idem={a['idempotency_key']}")
    res = fn()
    if not res.get("ok"):
        raise ToolError(-32005, res.get("error", "write failed"))
    return {"source": "paytm_sim", **res}


def t_stop_rental(mid, a):
    _owns("devices", a["device_id"], mid)
    return _write("stop_rental", mid, a, 0, lambda: paytm_sim.stop_rental(mid, a["device_id"], a["idempotency_key"]))


def t_post_credit(mid, a):
    return _write("post_credit", mid, a, a["amount"],
                  lambda: paytm_sim.post_credit(mid, a["amount"], a["ref"], a.get("kind", "refund"), a["idempotency_key"]))


def t_requeue_settlement(mid, a):
    _owns("settlements", a["settlement_id"], mid)
    return _write("requeue_settlement", mid, a, a.get("amount", 0),
                  lambda: paytm_sim.requeue_settlement(mid, a["settlement_id"], a["idempotency_key"]))


def t_remote_restart(mid, a):
    _owns("devices", a["device_id"], mid)
    return _write("remote_restart", mid, a, 0, lambda: paytm_sim.remote_restart(mid, a["device_id"], a["idempotency_key"]))


def t_qr_replacement(mid, a):
    return _write("qr_replacement", mid, a, 0, lambda: paytm_sim.qr_replacement(mid, a.get("reason", "tampered_qr"), a["idempotency_key"]))


def t_raise_reversal_request(cid, a):
    if not store.q1("SELECT id FROM upi_txns WHERE utr=? AND customer_id=?", (a["utr"], cid)):
        raise ToolError(-32001, "scope violation: UTR does not belong to session customer")
    return _write("raise_reversal_request", cid, a, 0, lambda: paytm_sim.raise_reversal_request(cid, a["utr"], a["idempotency_key"]))


def t_post_customer_credit(cid, a):
    return _write("post_customer_credit", cid, a, a["amount"],
                  lambda: paytm_sim.post_customer_credit(cid, a["amount"], a["ref"], a.get("kind", "compensation"), a["idempotency_key"]))


# ------------------------------------------------------------------ registry
def _schema(props, required, idf="merchant_id"):
    base = {idf: {"type": "string", "description": "Session subject; enforced by the server"}}
    base.update(props)
    return {"type": "object", "properties": base, "required": [idf] + required, "additionalProperties": False}


def _cschema(props, required):
    return _schema(props, required, "customer_id")


S, I = {"type": "string"}, {"type": "integer"}
WRITE_PROPS = {"gateway_token": S, "idempotency_key": S}
TOOLS = {
    "get_state_card": ("read", "Masked per-merchant state card: devices, charges_30d, settlements_7d, tickets_90d, open_anomalies.", _schema({}, []), t_get_state_card),
    "get_devices": ("read", "Devices rented by the merchant with lifecycle state and heartbeat.", _schema({}, []), t_get_devices),
    "get_charges": ("read", "Charges (rental, fees) in the last N days.", _schema({"days": I}, []), t_get_charges),
    "get_tickets": ("read", "Support tickets of the merchant.", _schema({}, []), t_get_tickets),
    "get_billing_events": ("read", "Billing system events, optionally for one device.", _schema({"device_id": S}, []), t_get_billing_events),
    "get_warehouse_scan": ("read", "Warehouse return scans (serial, QC) for a device.", _schema({"device_id": S}, ["device_id"]), t_get_warehouse_scan),
    "get_settlements": ("read", "Settlements to the merchant bank, optionally one id.", _schema({"settlement_id": S}, []), t_get_settlements),
    "get_ledger_entries": ("read", "Ledger credits/settlements, optionally by reference or last N minutes.", _schema({"ref": S, "days": I, "minutes": I}, []), t_get_ledger_entries),
    "get_device_heartbeat": ("read", "Recent heartbeats + online flag for a device.", _schema({"device_id": S}, ["device_id"]), t_get_device_heartbeat),
    "get_payments": ("read", "Customer payments in the last N hours (or minutes), with alert delivery flag.", _schema({"hours": I, "minutes": I}, []), t_get_payments),
    "get_txn_by_utr": ("read", "Look up a UPI payment by UTR: amount, status and whether the payee is this merchant (payer data withheld).", _schema({"utr": S}, ["utr"]), t_get_txn_by_utr),
    "get_qr_codes": ("read", "The merchant's QR codes and their status.", _schema({}, []), t_get_qr_codes),
    "get_customer_card": ("read", "Masked customer state card: recent UPI payments, statuses, bank, reversal status, open anomalies.", _cschema({}, []), t_get_customer_card),
    "get_upi_txns": ("read", "Customer's UPI payments, optionally one UTR.", _cschema({"utr": S}, []), t_get_upi_txns),
    "get_customer_credits": ("read", "Credits/compensation posted to the customer.", _cschema({}, []), t_get_customer_credits),
    "get_payouts": ("read", "Incentive / cashback payouts.", _schema({}, []), t_get_payouts),
    "get_credit_queue": ("read", "Finance credit queue items (PENDING/POSTED).", _schema({}, []), t_get_credit_queue),
    "is_linked_account": ("read", "Whether a destination is the merchant's own linked account (no PII returned).", _schema({"destination": S}, ["destination"]), t_is_linked_account),
    "stop_rental": ("write", "Stop rental billing for a returned device. Needs gateway token.", _schema({"device_id": S, **WRITE_PROPS}, ["device_id", "gateway_token", "idempotency_key"]), t_stop_rental),
    "post_credit": ("write", "Queue a credit to the merchant ledger. Needs gateway token.", _schema({"amount": I, "ref": S, "kind": S, **WRITE_PROPS}, ["amount", "ref", "gateway_token", "idempotency_key"]), t_post_credit),
    "requeue_settlement": ("write", "Re-queue a held settlement to the bank. Needs gateway token.", _schema({"settlement_id": S, "amount": I, **WRITE_PROPS}, ["settlement_id", "gateway_token", "idempotency_key"]), t_requeue_settlement),
    "remote_restart": ("write", "Remote-restart a Soundbox. Needs gateway token.", _schema({"device_id": S, **WRITE_PROPS}, ["device_id", "gateway_token", "idempotency_key"]), t_remote_restart),
    "qr_replacement": ("write", "Block the merchant's current QR and issue a new one. Needs gateway token.", _schema({"reason": S, **WRITE_PROPS}, ["gateway_token", "idempotency_key"]), t_qr_replacement),
    "raise_reversal_request": ("write", "Raise a reversal request for a failed UPI payment. Needs gateway token.", _cschema({"utr": S, **WRITE_PROPS}, ["utr", "gateway_token", "idempotency_key"]), t_raise_reversal_request),
    "post_customer_credit": ("write", "Credit compensation to the customer (e.g. RBI TAT ₹100/day). Needs gateway token.", _cschema({"amount": I, "ref": S, "kind": S, **WRITE_PROPS}, ["amount", "ref", "gateway_token", "idempotency_key"]), t_post_customer_credit),
}
CUSTOMER_TOOLS = {n for n, v in TOOLS.items() if "customer_id" in v[2]["properties"]}
READ_ONLY_TOOLS = [k for k, v in TOOLS.items() if v[0] == "read"]


def _validate(schema, args):
    if not isinstance(args, dict):
        raise ToolError(-32602, "arguments must be an object")
    for r in schema["required"]:
        if r not in args:
            raise ToolError(-32602, f"missing required argument '{r}'")
    for k, v in args.items():
        spec = schema["properties"].get(k)
        if spec is None:
            raise ToolError(-32602, f"unknown argument '{k}'")
        if spec["type"] == "string" and not isinstance(v, str):
            raise ToolError(-32602, f"'{k}' must be a string")
        if spec["type"] == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            raise ToolError(-32602, f"'{k}' must be an integer")


def tools_list():
    return [{"name": n, "description": d, "inputSchema": s, "annotations": {"readOnlyHint": kind == "read"}}
            for n, (kind, d, s, _) in TOOLS.items()]


def handle(req, session):
    """req: parsed JSON-RPC request dict. session: {'merchant_id': ...} from login. Returns response dict."""
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    try:
        if req.get("jsonrpc") != "2.0":
            raise ToolError(-32600, "invalid request: jsonrpc must be '2.0'")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO, "capabilities": {"tools": {"listChanged": False}}}
        elif method == "tools/list":
            result = {"tools": tools_list()}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in TOOLS:
                raise ToolError(-32602, f"unknown tool {name}")
            kind, _, schema, fn = TOOLS[name]
            _validate(schema, args)
            mid = (session or {}).get("merchant_id")
            if not mid:
                raise ToolError(-32001, "no authenticated session")
            idf = "customer_id" if name in CUSTOMER_TOOLS else "merchant_id"
            if args[idf] != mid:
                logger.log("mcp-server", f"{name}: scope violation · requested {args[idf]} ≠ session {mid} → rejected", level="warn")
                raise ToolError(-32001, f"scope violation: {idf} does not match session")
            try:
                data = fn(mid, args)
            except ToolError as e:
                if e.code == -32001:
                    logger.log("mcp-server", f"{name}: {e.message} → rejected", level="warn")
                raise
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
                      "structuredContent": data, "isError": False}
        else:
            raise ToolError(-32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except ToolError as e:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}


def handle_json(raw, session):
    try:
        req = json.loads(raw)
    except Exception:
        return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
    return json.dumps(handle(req, session), ensure_ascii=False, default=str)
