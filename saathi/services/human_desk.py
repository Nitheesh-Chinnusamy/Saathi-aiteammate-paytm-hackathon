"""human_desk.py - the human-in-the-loop queue: approvals, escalations with proof packs,
blocked/fraud flags and draft-playbook reviews.

Production: a ticketing/ops console (e.g. Zendesk/Freshdesk/internal tool) with SLAs and RBAC.
"""
import json

from saathi.core import logger
from saathi.core import store
from saathi.core.clock import clock


def open_item(case_id, merchant_id, kind, title, proof):
    did = store.next_id("H-", 501)
    store.insert("desk", {"id": did, "case_id": case_id, "merchant_id": merchant_id, "kind": kind, "title": title,
                          "proof": json.dumps(proof, ensure_ascii=False, default=str), "status": "OPEN",
                          "created_at": clock.now_iso()})
    logger.log("human", f"desk {did} opened · {kind} · {title}")
    return did


def close_item(did, resolution, operator="ops-agent"):
    store.update("desk", "id", did, status="DONE", resolved_at=clock.now_iso(), resolution=resolution)
    logger.log("human", f"desk {did} {resolution} by {operator}")


def get(did):
    r = store.q1("SELECT * FROM desk WHERE id=?", (did,))
    if r:
        r["proof"] = store.jload(r["proof"], {})
    return r


def items(status=None):
    rows = store.q("SELECT * FROM desk" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC, id DESC",
                   (status,) if status else ())
    for r in rows:
        r["proof"] = store.jload(r["proof"], {})
    return rows
