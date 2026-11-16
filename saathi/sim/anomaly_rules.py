"""anomaly_rules.py - deterministic rules that pre-flag open anomalies into the state card,
plus the named incident predicates reused by Step 1 (incident attach) and Step 2.

Rules operate on plain dicts (never the DB). Each anomaly carries the record IDs that prove it.

Production: a rules engine / streaming anomaly detectors over the merchant event stream.
"""
from saathi.core.clock import hours_between, parse

DEVICE_OFFLINE_STALE_H = 24


def flag(raw, now_iso):
    """raw = unmasked assembled merchant data from mcp_server. Returns list of anomalies."""
    out = []
    for d in raw.get("devices", []):
        if d["state"] == "RETURN_RECEIVED" and d["rental_status"] == "ACTIVE":
            after = [c for c in raw.get("charges_30d", []) if c.get("device_id") == d["id"] and c["type"] == "RENTAL"
                     and parse(c["ts"]) > parse(d["state_since"])]
            if after:
                c = after[-1]
                out.append({"id": f"A-RAR-{d['id']}", "rule": "rent_after_return", "area": "device",
                            "device_id": d["id"], "device_model": d["model"], "state": d["state"],
                            "state_since": d["state_since"], "charge_id": c["id"], "amount": c["amount"],
                            "summary": f"₹{c['amount']} rent {c['id']} charged after {d['id']} was returned",
                            "evidence_ids": [d["id"], c["id"]]})
        stale = hours_between(d["last_heartbeat"], now_iso) if d.get("last_heartbeat") else 0
        if d["state"] == "ACTIVE" and stale > DEVICE_OFFLINE_STALE_H and raw.get("payments_24h", {}).get("count", 0) == 0:
            out.append({"id": f"A-OFF-{d['id']}", "rule": "device_offline", "area": "device", "device_id": d["id"],
                        "device_model": d["model"], "state": "OFFLINE", "summary": f"{d['id']} offline {stale:.0f}h, no payments",
                        "evidence_ids": [d["id"]]})
    for s in raw.get("settlements_7d", []):
        if s["status"] == "HELD":
            out.append({"id": f"A-SH-{s['id']}", "rule": "settlement_held", "area": "settlement",
                        "settlement_id": s["id"], "amount": s["amount"], "reason_code": s["reason"],
                        "bank": s["bank_name"], "ifsc_prefix": (s["ifsc"] or "")[:4], "since": s["ts"],
                        "summary": f"₹{s['amount']:,} settlement {s['id']} held ({s['reason']})",
                        "evidence_ids": [s["id"]]})
    for p in raw.get("payouts", []):
        if p["status"] == "PENDING":
            out.append({"id": f"A-INC-{p['id']}", "rule": "incentive_pending", "area": "payments",
                        "payout_id": p["id"], "amount": p["amount"], "since": p["ts"],
                        "summary": f"₹{p['amount']} {p['kind'].lower()} payout {p['id']} pending",
                        "evidence_ids": [p["id"]]})
    return out


def flag_customer(raw):
    """Customer-side rules: a UPI payment debited from the customer but never credited to the payee."""
    out = []
    for t in raw.get("upi_txns", []):
        if t["status"] in ("DEBITED_NOT_CREDITED", "PENDING") and t.get("reversal_status") != "REVERSED":
            out.append({"id": f"A-DNC-{t['id']}", "rule": "debited_not_credited", "area": "payments", "txn_id": t["id"],
                        "utr": t["utr"], "amount": t["amount"], "txn_status": t["status"], "bank": t["bank_name"],
                        "bank_prefix": (t["ifsc"] or "")[:4], "reason_code": t["reason"], "since": t["ts"],
                        "payee": t["payee_name"],
                        "summary": f"₹{t['amount']:,} to {t['payee_name']} {t['status'].replace('_', ' ').lower()} ({t['id']})",
                        "evidence_ids": [t["id"]]})
    return out


def incident_key(anomaly):
    """Key built from the ANOMALY'S DATA, never from the merchant's words."""
    r = anomaly["rule"]
    if r == "debited_not_credited":
        return f"{r}|{anomaly.get('bank_prefix')}|{anomaly.get('reason_code')}"
    if r in ("rent_after_return", "device_offline"):
        return f"{r}|{anomaly.get('device_model')}|{anomaly.get('state')}"
    if r == "settlement_held":
        return f"{r}|{anomaly.get('ifsc_prefix')}|{anomaly.get('reason_code')}"
    return f"{r}|-|-"


# ------------------------------------------------------------------ incident predicates
def rent_after_return_unbilled(card, data):
    """device in RETURN_RECEIVED and no billing return event and a rental charge after the return date."""
    for d in card.get("devices", []):
        if d["state"] != "RETURN_RECEIVED":
            continue
        events = [e for e in data.get("billing_events", []) if e["device_id"] == d["id"]
                  and e["event"] in ("RETURN_RECORDED", "RENTAL_STOPPED")]
        charged = [c for c in card.get("charges_30d", []) if c.get("device_id") == d["id"] and c["type"] == "RENTAL"
                   and parse(c["ts"]) > parse(d["state_since"])]
        if not events and charged:
            return True, [d["id"], charged[-1]["id"]]
    return False, []


def settlement_held_bank(card, data, incident=None):
    sig = (incident or {}).get("signature", "settlement_held|DCCB|BANK_NO_RESPONSE").split("|")
    for s in card.get("settlements_7d", []):
        if s["status"] == "HELD" and (s.get("ifsc") or "")[:4] == sig[1] and s.get("reason") == sig[2]:
            return True, [s["id"]]
    return False, []


PREDICATES = {"rent_after_return_unbilled": rent_after_return_unbilled, "settlement_held_bank": settlement_held_bank}


def run_predicate(name, card, data, incident=None):
    fn = PREDICATES.get(name)
    if not fn:
        return False, []
    if name == "settlement_held_bank":
        return fn(card, data, incident)
    return fn(card, data)
