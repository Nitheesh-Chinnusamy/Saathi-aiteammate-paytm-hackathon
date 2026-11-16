"""M4: Step 6 Verify, Step 7 Remediate, learning loop, spike detection."""
from saathi.core import event_bus
from saathi.services import human_desk
from saathi.services import learning
from saathi.services import pipeline
from saathi.steps import step5_contract
from saathi.steps import step6_verify
from saathi.core import store
from saathi.sim.seed import SCENARIOS


def send(sid):
    sc = SCENARIOS[sid]
    return pipeline.handle_message(sc["merchant_id"], text=sc["text"], scenario_id=sid)


def test_predicates_read_sim_not_gateway():
    r = send("S1")
    k = step5_contract.load(r["case"]["contract_id"])
    # the gateway reported success for post_credit...
    assert any(e["action"] == "post_credit" and e["result"]["ok"] for e in r["case"]["executed"])
    # ...but the system of record has no ledger credit, so the check must FAIL
    k = step6_verify.evaluate(k["id"])
    st = {c["name"]: c["status"] for c in k["checks"]}
    assert st["credit_posted"] == "FAIL"


def test_s1_ladder_order_and_human_fix():
    r = send("S1")
    kid = r["case"]["contract_id"]
    pipeline.advance(24)
    k = step5_contract.load(kid)
    assert {c["name"]: c["status"] for c in k["checks"]}["rental_stopped"] == "PASS"
    pipeline.advance(24)
    k = step5_contract.load(kid)
    assert [s["step"] for s in k["ladder"]] == ["retry", "recheck", "chase_owner", "compensate", "notify_delay", "escalate", "rearm"]
    assert "PENDING" in k["ladder"][1]["result"]
    assert k["cycle"] == 2 and k["status"] == "OPEN"
    # retry never double-credits
    assert store.q1("SELECT COUNT(*) n FROM finance_queue WHERE ref='C-5521'")["n"] == 1
    esc = [d for d in human_desk.items("OPEN") if d["kind"] == "escalation"]
    assert len(esc) == 1 and esc[0]["proof"]["queue_items"]
    pipeline.desk_action(esc[0]["id"], "resolve")
    k = step5_contract.load(kid)
    assert k["status"] == "CLOSED" and k["proof"]["kept_original_deadline"] is False
    assert any(e.startswith("LE-") for c in k["proof"]["checks"] for e in c["evidence"])
    assert store.load_case(r["case"]["id"])["status"] == "CLOSED"


def test_s2_happy_path_closes_before_deadline():
    r = send("S2")
    pipeline.advance(48)
    k = step5_contract.load(r["case"]["contract_id"])
    assert k["status"] == "CLOSED" and k["proof"]["kept_original_deadline"] is True
    assert k["ladder"] == []
    msgs = store.q("SELECT kind, lang FROM messages WHERE case_id=? AND direction='out'", (r["case"]["id"],))
    assert ("done_credit", "hi") in [(m["kind"], m["lang"]) for m in msgs]


def test_reconcile_catches_lost_events():
    r = send("S2")
    from saathi.core.clock import clock
    event_bus.clear()            # simulate a broken event pipe: no subscriber hears sim events
    clock._listeners.clear()     # ...and no reconcile-on-advance either
    clock.advance(31 * 3600)
    k = step5_contract.load(r["case"]["contract_id"])
    assert k["status"] == "OPEN"     # credit posted in the SoR, but nobody noticed
    step6_verify.reconcile()
    assert step5_contract.load(r["case"]["contract_id"])["status"] == "CLOSED"


def test_s4_novel_draft_playbook():
    r = send("S4")
    assert r["case"]["status"] == "AWAITING_APPROVAL"
    did = r["case"]["desk_id"]
    pipeline.desk_action(did, "approve")
    pipeline.advance(0.25)
    case = store.load_case(r["case"]["id"])
    assert case["status"] == "CLOSED"
    pb = store.q1("SELECT * FROM playbooks WHERE id='remote_restart'")
    assert pb["status"] == "DRAFT"
    review = [d for d in human_desk.items("OPEN") if d["kind"] == "playbook_review"]
    assert review
    pipeline.desk_action(review[0]["id"], "approve")
    assert store.q1("SELECT status, autonomy FROM playbooks WHERE id='remote_restart'")["autonomy"] == "ACT_WITH_APPROVAL"


def test_spike_detector_40_to_1():
    res = pipeline.simulate_outage(40)
    assert res["cases"] == 40
    assert len(res["incidents"]) == 1 and res["incidents"][0]["n"] == 40
    inc = store.q1("SELECT * FROM incidents WHERE id=?", (res["incidents"][0]["incident_id"],))
    assert inc["source"] == "spike" and inc["members"] == 40


def test_spike_needs_enough_merchants():
    # 9 distinct merchants is below the >= 10 threshold -> no incident
    from saathi.sim import paytm_sim
    held = paytm_sim.bank_outage(9)
    assert len({h["merchant_id"] for h in held}) == 9
    from saathi.core.clock import clock
    for h in held:
        store.save_case({"id": store.next_id("C-", 9917), "merchant_id": h["merchant_id"], "created_at": clock.now_iso(),
                         "status": "UNDERSTOOD", "issue_type": "settlement_held", "key": "settlement_held|DCCB|BANK_TIMEOUT",
                         "source": "outage"})
    assert learning.detect_spikes() == []


def test_learning_updates_stats_on_close():
    n0 = store.q1("SELECT COUNT(*) n FROM historical_cases WHERE issue_type='rent_after_return'")["n"]
    send("S2")
    pipeline.advance(48)
    assert store.q1("SELECT COUNT(*) n FROM historical_cases WHERE issue_type='rent_after_return'")["n"] == n0 + 1
    pb = store.q1("SELECT wins, attempts FROM playbooks WHERE id='stop_rental_and_credit'")
    assert (pb["wins"], pb["attempts"]) == (141, 147)
