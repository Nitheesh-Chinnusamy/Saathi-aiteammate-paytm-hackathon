"""learning.py - the learning loop around all steps.

* on_close: verified results update historical stats (priors + P90 latencies), cluster-card
  counts and playbook wins/attempts; autonomy promotion/demotion rule is evaluated.
* novel solved case (Tier 2) -> DRAFT playbook awaiting human approval on the desk.
* spike detection: >= 10 distinct merchants AND >= 5x baseline for one data-built key within
  2h -> open an incident and attach every member ("40 tickets -> 1 incident").

Production: offline training jobs + a feature store; incident detection in a streaming engine.
"""
from datetime import timedelta

from saathi.services import human_desk
from saathi.core import logger
from saathi.core import store
from saathi.core.clock import clock, hours_between, iso

AUTONOMY_LEVELS = ["DRAFT", "ACT_WITH_APPROVAL", "ACT_ALONE"]
PROMOTION_RULE = ("DRAFT → ACT_WITH_APPROVAL when a human approves the draft; ACT_WITH_APPROVAL → ACT_ALONE after "
                  "≥ 20 verified wins at ≥ 95% verified rate; any verification failure after the ladder demotes one level.")
SPIKE_MIN_MERCHANTS, SPIKE_MULT, SPIKE_WINDOW_H = 10, 5.0, 2


def autonomy_check(pb_id):
    pb = store.q1("SELECT * FROM playbooks WHERE id=?", (pb_id,))
    if not pb or pb["status"] != "ACTIVE":
        return None
    rate = pb["wins"] / pb["attempts"] if pb["attempts"] else 0
    if pb["autonomy"] == "ACT_WITH_APPROVAL" and pb["wins"] >= 20 and rate >= 0.95:
        store.update("playbooks", "id", pb_id, autonomy="ACT_ALONE")
        logger.log("learning", f"{pb_id} promoted → ACT_ALONE ({pb['wins']}/{pb['attempts']} = {rate:.1%})")
        return "ACT_ALONE"
    logger.log("learning", f"{pb_id} {pb['wins']}/{pb['attempts']} verified ({rate:.1%}) → stays {pb['autonomy']}")
    return pb["autonomy"]


def on_close(case_id, kid):
    from saathi.steps import step5_contract
    case = store.load_case(case_id) or {}
    k = step5_contract.load(kid)
    dx = case.get("diagnosis") or {}
    issue = case.get("issue_type")
    lat = hours_between(k["created_at"], k["closed_at"])
    human_fixed = bool(store.q1("SELECT id FROM desk WHERE case_id=? AND kind='escalation'", (case_id,)))
    if issue in ("rent_after_return", "settlement_held") or dx.get("fix", {}).get("novel"):
        hist_issue = "device_offline" if dx.get("fix", {}).get("novel") else issue
        store.insert("historical_cases", {"issue_type": hist_issue, "cause": dx.get("cause"), "fix": k["playbook_id"], "verified": 1,
                                          "latency_hours": round(lat, 2), "created_at": clock.now_iso(), "source": "learned"})
        n = store.q1("SELECT COUNT(*) n FROM historical_cases WHERE issue_type=?", (hist_issue,))["n"]
        logger.log("learning", f"{hist_issue} stats += 1 verified case ({lat:.1f}h, cause {dx.get('cause')}) → {n} rows feed priors/P90")
    store.ex("UPDATE cluster_cards SET resolved = resolved + 1 WHERE issue_type=?", (issue,))
    pb = store.q1("SELECT * FROM playbooks WHERE id=?", (k["playbook_id"],))
    if pb and pb["status"] == "ACTIVE":
        win = 0 if human_fixed else 1
        store.ex("UPDATE playbooks SET attempts = attempts + 1, wins = wins + ? WHERE id=?", (win, pb["id"]))
        if human_fixed:
            logger.log("learning", f"{pb['id']} fix needed a human after the ladder → counted as attempt, not a win")
        autonomy_check(pb["id"])
    if dx.get("fix", {}).get("novel"):
        draft_playbook(case, dx, k)


def draft_playbook(case, dx, k):
    actions = [a["action"] for a in dx["fix"]["actions"]]
    pb_id = actions[0]
    if store.q1("SELECT id FROM playbooks WHERE id=?", (pb_id,)):
        return None
    store.insert("playbooks", {"id": pb_id, "name": f"Learned: {dx.get('cause_label') or dx.get('cause')}", "issue_type": dx.get("cause"),
                               "autonomy": "DRAFT", "status": "DRAFT", "actions": actions, "checks": [c["name"] for c in k["checks"]],
                               "approval_above": None, "wins": 1, "attempts": 1, "source": f"learned from {case['id']}",
                               "created_at": clock.now_iso()})
    did = human_desk.open_item(case["id"], case["merchant_id"], "playbook_review", f"Draft playbook '{pb_id}' learned from {case['id']}",
                               {"playbook": pb_id, "cause": dx.get("cause"), "claims": dx.get("claims"), "actions": actions,
                                "checks": [c["name"] for c in k["checks"]], "verified_by": k["id"], "proof": k.get("proof")})
    logger.log("learning", f"novel fix verified → DRAFT playbook {pb_id} ({'+'.join(actions)}) · awaiting human approval {did}")
    return pb_id


def approve_playbook(pb_id, operator):
    store.update("playbooks", "id", pb_id, status="ACTIVE", autonomy="ACT_WITH_APPROVAL")
    logger.log("learning", f"playbook {pb_id} approved by {operator} → ACTIVE · autonomy ACT_WITH_APPROVAL")


# ------------------------------------------------------------------ spike detection
def baseline_per_window(issue_type):
    n = store.q1("SELECT COUNT(*) n FROM historical_cases WHERE issue_type=? AND source='seed'", (issue_type,))["n"]
    windows = 90 * 24 / SPIKE_WINDOW_H          # history spans 90 days
    return max(1.0, n / windows)


def detect_spikes():
    since = iso(clock.now() - timedelta(hours=SPIKE_WINDOW_H))
    rows = store.q("SELECT id, merchant_id, key, issue_type FROM cases WHERE created_at>=? AND key IS NOT NULL AND incident_id IS NULL",
                   (since,))
    groups = {}
    for r in rows:
        groups.setdefault(r["key"], []).append(r)
    opened = []
    active = {i["signature"] for i in store.q("SELECT signature FROM incidents WHERE status='ACTIVE'")}
    for key, members in groups.items():
        if key in active:
            continue
        merchants = {m["merchant_id"] for m in members}
        base = baseline_per_window(members[0]["issue_type"])
        if len(merchants) >= SPIKE_MIN_MERCHANTS and len(members) >= SPIKE_MULT * base:
            iid = store.next_id("INC-0", 500)
            parts = key.split("|")
            store.insert("incidents", {"id": iid, "signature": key, "issue_type": members[0]["issue_type"],
                                       "title": f"Spike: {parts[0]} at {parts[1]} ({parts[2]})",
                                       "description": f"Many {parts[1]} merchants report {parts[0].replace('_', ' ')} with reason {parts[2]}",
                                       "predicate": "settlement_held_bank" if parts[0] == "settlement_held" else None,
                                       "precision_holdout": None, "members": 0, "status": "ACTIVE", "created_at": clock.now_iso(),
                                       "source": "spike"})
            logger.log("learning", f"spike {key}: {len(members)} cases / {len(merchants)} merchants in {SPIKE_WINDOW_H}h vs baseline "
                                   f"{base:.1f} (≥{SPIKE_MIN_MERCHANTS} merchants, ≥{SPIKE_MULT:g}x) → opened {iid}")
            for m in members:
                attach(iid, m["id"], m["merchant_id"])
            opened.append(iid)
            from saathi.services import clustering
            clustering.invalidate()
    return opened


def attach(iid, case_id, merchant_id):
    store.insert("incident_members", {"incident_id": iid, "case_id": case_id, "merchant_id": merchant_id, "ts": clock.now_iso()})
    store.ex("UPDATE incidents SET members = members + 1 WHERE id=?", (iid,))
    store.ex("UPDATE cases SET incident_id=? WHERE id=?", (iid, case_id))
