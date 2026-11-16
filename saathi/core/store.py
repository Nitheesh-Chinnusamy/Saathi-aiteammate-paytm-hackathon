"""store.py - SQLite schema, tiny query helpers, id counters and reset.

Two groups of tables live in one in-memory database for the prototype:
  * "Paytm systems of record" (merchants, devices, charges, billing, ledger, settlements,
    warehouse, payments, finance queue). Only paytm_sim (Paytm side) writes them and only
    mcp_server (Saathi side) reads them.
  * Saathi's own tables (cases, contracts, audit, desk, incidents, stats, playbooks ...).

Production: Postgres per service (Paytm SoR stays where it is; Saathi owns its own DB).
"""
import json
import os
import sqlite3
import threading

LOCK = threading.RLock()
_conn = None

MERCHANT_DATA_TABLES = {"merchants", "devices", "charges", "billing_events", "warehouse_scans", "tickets",
                        "settlements", "payouts", "ledger_entries", "finance_queue", "payments", "heartbeats",
                        "customers", "upi_txns", "customer_credits", "qr_codes", "sim_requests"}

SCHEMA = """
-- ---------- simulated Paytm systems of record ----------
CREATE TABLE merchants (id TEXT PRIMARY KEY, name TEXT, business TEXT, city TEXT, lang TEXT, phone TEXT,
  bank_account TEXT, ifsc TEXT, bank_name TEXT, linked_vpa TEXT, joined_at TEXT, fraud_flag INTEGER DEFAULT 0,
  background INTEGER DEFAULT 0);
CREATE TABLE devices (id TEXT PRIMARY KEY, merchant_id TEXT, model TEXT, serial TEXT, state TEXT, state_since TEXT,
  rental_status TEXT, monthly_rent INTEGER, last_heartbeat TEXT, online INTEGER DEFAULT 1);
CREATE TABLE charges (id TEXT PRIMARY KEY, merchant_id TEXT, device_id TEXT, type TEXT, amount INTEGER, ts TEXT, status TEXT);
CREATE TABLE billing_events (id TEXT PRIMARY KEY, merchant_id TEXT, device_id TEXT, event TEXT, ts TEXT);
CREATE TABLE warehouse_scans (id TEXT PRIMARY KEY, merchant_id TEXT, device_id TEXT, serial TEXT, qc TEXT, ts TEXT);
CREATE TABLE tickets (id TEXT PRIMARY KEY, merchant_id TEXT, topic TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
  credit_issued INTEGER, notes TEXT);
CREATE TABLE settlements (id TEXT PRIMARY KEY, merchant_id TEXT, amount INTEGER, status TEXT, reason TEXT,
  bank_name TEXT, ifsc TEXT, ts TEXT, credited_at TEXT);
CREATE TABLE payouts (id TEXT PRIMARY KEY, merchant_id TEXT, kind TEXT, amount INTEGER, status TEXT, ts TEXT);
CREATE TABLE ledger_entries (id TEXT PRIMARY KEY, merchant_id TEXT, type TEXT, amount INTEGER, ref TEXT, ts TEXT, status TEXT);
CREATE TABLE finance_queue (id TEXT PRIMARY KEY, merchant_id TEXT, amount INTEGER, ref TEXT, kind TEXT, idem_key TEXT UNIQUE,
  status TEXT, created_at TEXT, posted_at TEXT);
CREATE TABLE payments (id TEXT PRIMARY KEY, merchant_id TEXT, amount INTEGER, ts TEXT, status TEXT, channel TEXT, alert_sent INTEGER);
CREATE TABLE heartbeats (id TEXT PRIMARY KEY, device_id TEXT, merchant_id TEXT, ts TEXT);
CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, city TEXT, lang TEXT, phone TEXT, bank_account TEXT, bank_name TEXT,
  ifsc TEXT, vpa TEXT, fraud_flag INTEGER DEFAULT 0, joined_at TEXT);
CREATE TABLE upi_txns (id TEXT PRIMARY KEY, utr TEXT, customer_id TEXT, payer_vpa TEXT, payee_vpa TEXT, payee_name TEXT,
  merchant_id TEXT, amount INTEGER, ts TEXT, status TEXT, bank_name TEXT, ifsc TEXT, reason TEXT, reversal_status TEXT,
  reversal_ref TEXT, reversal_at TEXT);
CREATE TABLE customer_credits (id TEXT PRIMARY KEY, customer_id TEXT, amount INTEGER, ref TEXT, kind TEXT, idem_key TEXT UNIQUE, ts TEXT);
CREATE TABLE qr_codes (id TEXT PRIMARY KEY, merchant_id TEXT, vpa TEXT, status TEXT, created_at TEXT);
CREATE TABLE sim_requests (id TEXT PRIMARY KEY, kind TEXT, subject_id TEXT, ref TEXT, idem_key TEXT UNIQUE, ts TEXT);
-- ---------- Saathi's own tables ----------
CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER);
CREATE TABLE sim_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE cases (id TEXT PRIMARY KEY, merchant_id TEXT, status TEXT, issue_type TEXT, incident_id TEXT, key TEXT,
  created_at TEXT, updated_at TEXT, llm_used TEXT DEFAULT 'none', source TEXT DEFAULT 'chat', data TEXT);
CREATE TABLE contracts (id TEXT PRIMARY KEY, case_id TEXT, merchant_id TEXT, playbook_id TEXT, checks TEXT, promise TEXT,
  deadline TEXT, original_deadline TEXT, status TEXT, cycle INTEGER DEFAULT 1, created_at TEXT, closed_at TEXT, proof TEXT,
  ladder TEXT DEFAULT '[]', on_fail TEXT, lang TEXT, amount INTEGER);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY, ts TEXT, case_id TEXT, action TEXT, row_json TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE idempotency (key TEXT PRIMARY KEY, case_id TEXT, action TEXT, result TEXT, ts TEXT);
CREATE TABLE compensations (id TEXT PRIMARY KEY, case_id TEXT, action TEXT, idem_key TEXT, compensation TEXT, params TEXT,
  status TEXT, ts TEXT);
CREATE TABLE desk (id TEXT PRIMARY KEY, case_id TEXT, merchant_id TEXT, kind TEXT, title TEXT, proof TEXT, status TEXT,
  created_at TEXT, resolved_at TEXT, resolution TEXT);
CREATE TABLE owner_tickets (id TEXT PRIMARY KEY, case_id TEXT, owner TEXT, evidence TEXT, status TEXT, ts TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT, case_id TEXT, direction TEXT, kind TEXT,
  text TEXT, text_en TEXT, lang TEXT, ts TEXT, choices TEXT);
CREATE TABLE incidents (id TEXT PRIMARY KEY, signature TEXT, issue_type TEXT, title TEXT, description TEXT, predicate TEXT,
  precision_holdout REAL, members INTEGER, status TEXT, created_at TEXT, source TEXT);
CREATE TABLE incident_members (incident_id TEXT, case_id TEXT, merchant_id TEXT, ts TEXT);
CREATE TABLE historical_cases (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_type TEXT, cause TEXT, fix TEXT, verified INTEGER,
  latency_hours REAL, created_at TEXT, source TEXT DEFAULT 'seed');
CREATE TABLE cluster_cards (issue_type TEXT PRIMARY KEY, area TEXT, description TEXT, examples TEXT, causes TEXT,
  playbook_id TEXT, owner TEXT, verified_success REAL, resolved INTEGER DEFAULT 0, ticket_topic TEXT);
CREATE TABLE lifecycles (name TEXT PRIMARY KEY, stages TEXT, p90_hours TEXT);
CREATE TABLE playbooks (id TEXT PRIMARY KEY, name TEXT, issue_type TEXT, autonomy TEXT, status TEXT, actions TEXT,
  checks TEXT, approval_above INTEGER, wins INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, source TEXT, created_at TEXT);
CREATE TABLE policy_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE templates (lang TEXT, kind TEXT, text TEXT, PRIMARY KEY (lang, kind));
"""


def conn():
    global _conn
    with LOCK:
        if _conn is None:
            _conn = sqlite3.connect(os.environ.get("SAATHI_DB", ":memory:"), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
        return _conn


def q(sql, args=()):
    with LOCK:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with LOCK:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def insert(table, row):
    cols = ",".join(row.keys())
    marks = ",".join("?" for _ in row)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in row.values()]
    return ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", vals)


def update(_table, _key_col, _key_val, **fields):
    sets = ",".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
    ex(f"UPDATE {_table} SET {sets} WHERE {_key_col}=?", vals + [_key_val])


def next_id(prefix, start):
    """Sequential human-friendly ids: next_id('K-', 301) -> K-301, K-302 ..."""
    with LOCK:
        row = q1("SELECT val FROM counters WHERE name=?", (prefix,))
        val = start if row is None else row["val"] + 1
        ex("INSERT OR REPLACE INTO counters (name, val) VALUES (?, ?)", (prefix, val))
        return f"{prefix}{val}"


def jload(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def reset():
    """Drop everything and re-create the schema (seed.py fills it)."""
    with LOCK:
        c = conn()
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            c.execute(f"DROP TABLE {t}")
        c.executescript(SCHEMA)
        c.commit()


# ------------------------------------------------------------------ case persistence (Saathi table)
def save_case(case):
    from saathi.core.clock import clock
    now = clock.now_iso()
    case["updated_at"] = now
    row = {"merchant_id": case["merchant_id"], "status": case.get("status", "OPEN"), "issue_type": case.get("issue_type"),
           "incident_id": (case.get("incident") or {}).get("id"), "key": case.get("key"), "updated_at": now,
           "llm_used": case.get("llm_used", "none"), "source": case.get("source", "chat"),
           "data": json.dumps({k: v for k, v in case.items() if not k.startswith("_")}, ensure_ascii=False, default=str)}
    with LOCK:
        if q1("SELECT id FROM cases WHERE id=?", (case["id"],)):
            update("cases", "id", case["id"], **row)
        else:
            insert("cases", {"id": case["id"], "created_at": case.get("created_at", now), **row})


def load_case(case_id):
    r = q1("SELECT data FROM cases WHERE id=?", (case_id,))
    return json.loads(r["data"]) if r else None
