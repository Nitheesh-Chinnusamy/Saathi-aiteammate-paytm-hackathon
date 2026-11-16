"""M7: the four everyday problems - S6 payment check (tampered QR), S7 failed UPI (both variants), S8 explain deductions."""
import pytest

from saathi.core import store
from saathi.sim.seed import SCENARIOS


@pytest.fixture()
def client():
    import app as appmod
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c


def contract(c, kid):
    return next(k for k in c.get("/api/contracts").get_json()["contracts"] if k["id"] == kid)


def kinds(c, who):
    return [m["kind"] for m in c.get(f"/api/chat?merchant_id={who}").get_json()["messages"] if m["direction"] == "out"]


def adv(c, h):
    assert c.post("/api/clock/advance", json={"hours": h}).status_code == 200


# ------------------------------------------------------------------ S6
def test_s6_payment_check_tampered_qr_one_tap_utr(client):
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "hinglish"})
    r = client.post("/api/message", json={"text": SCENARIOS["S6"]["text"], "scenario_id": "S6"}).get_json()
    case = r["case"]
    assert case["intent"] == "payment_check" and case["status"] == "AWAITING_UTR"
    ask = r["messages"][-1]
    assert ask["kind"] == "ask_utr" and "3,624" in ask["text"] and ask["choices"][0]["utr"] == SCENARIOS["S6"]["utr"]
    assert client.get("/api/audit").get_json()["rows"] == []            # nothing done yet
    r = client.post("/api/utr", json={"case_id": case["id"], "utr": ask["choices"][0]["utr"]}).get_json()
    case = r["case"]
    assert case["diagnosis"]["cause"] == "tampered_qr" and case["llm_used"] == "none"
    assert case["safety"]["decision"] == "ALLOW"
    assert [e["action"] for e in case["executed"]] == ["qr_replacement"]
    k = contract(client, case["contract_id"])
    assert k["promise"]["hours"] == 24 and [c["name"] for c in k["checks"]] == ["qr_registered", "test_payment_received"]
    assert kinds(client, "M-2291")[-2:] == ["qr_tampered", "promise_qr"]
    adv(client, 6)
    k = contract(client, case["contract_id"])
    assert k["status"] == "CLOSED" and all(c["status"] == "PASS" for c in k["checks"])
    assert kinds(client, "M-2291")[-1] == "done_qr"
    assert store.q1("SELECT status FROM qr_codes WHERE id='QR-2291'")["status"] == "BLOCKED"


def test_s6_typed_utr_and_unknown_utr(client):
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "en"})
    case = client.post("/api/message", json={"text": SCENARIOS["S6"]["texts"]["en"], "scenario_id": "S6"}).get_json()["case"]
    r = client.post("/api/message", json={"text": "UTR is 9999 0000 1111"}).get_json()
    assert r["case"]["id"] == case["id"] and r["case"]["status"] == "AWAITING_UTR"
    assert r["messages"][-1]["kind"] == "utr_not_found"
    r = client.post("/api/message", json={"text": "4231 8890 1123"}).get_json()
    assert r["case"]["diagnosis"]["cause"] == "tampered_qr" and r["case"]["status"] == "VERIFYING"


# ------------------------------------------------------------------ S7
def start_s7(client):
    client.post("/api/login", json={"merchant_id": "C-1001", "lang": "hi"})
    r = client.post("/api/message", json={"text": SCENARIOS["S7"]["text"], "scenario_id": "S7"}).get_json()
    case = r["case"]
    assert case["issue_type"] == "debited_not_credited"
    assert case["incident"]["id"] == "INC-0420" and case["incident"]["method"] == "bank"
    assert case["safety"]["decision"] == "ALLOW" and case["llm_used"] == "none"
    out = [m["kind"] for m in r["messages"] if m["kind"] != "merchant"]
    assert out == ["guidance_failed", "promise_reversal"]                # guidance first
    k = contract(client, case["contract_id"])
    assert k["deadline"].startswith("2026-11-15T23:59:59")              # end of T+1
    return case, k


def test_s7_reversal_on_time(client):
    case, k = start_s7(client)
    adv(client, 33)                                                     # past T+1 18:00
    k = contract(client, k["id"])
    assert k["status"] == "CLOSED" and k["ladder"] == [] and k["proof"]["kept_original_deadline"]
    assert kinds(client, "C-1001")[-1] == "done_reversal"
    assert store.q1("SELECT COUNT(*) n FROM customer_credits")["n"] == 0


def test_s7_bank_late_rbi_compensation(client):
    assert client.post("/api/sim/bank_late", json={"on": True}).get_json()["bank_late"] is True
    case, k = start_s7(client)
    assert k["promise"]["basis"].startswith("target: end of T+1 (RBI limit for merchant payments: T+5")
    adv(client, 40)                                                     # T+1 target missed (16 Nov 02:00)
    k = contract(client, k["id"])
    assert [s["step"] for s in k["ladder"]] == ["retry", "recheck", "chase_owner", "compensate", "notify_delay", "escalate", "rearm"]
    assert "no compensation due yet" in k["ladder"][3]["result"]          # still within the RBI T+5 limit
    assert store.q1("SELECT COUNT(*) n FROM customer_credits")["n"] == 0
    assert k["deadline"].startswith("2026-11-19T23:59")                  # next checkpoint = RBI limit
    assert "delay_reversal" in kinds(client, "C-1001")
    adv(client, 94)                                                     # past end of T+5 (20 Nov 00:00)
    k = contract(client, k["id"])
    assert k["status"] == "ESCALATED"
    assert "₹100" in k["ladder"][10]["result"]
    assert store.q1("SELECT SUM(amount) s FROM customer_credits WHERE customer_id='C-1001'")["s"] == 100
    assert "delay_reversal_comp" in kinds(client, "C-1001")
    adv(client, 12)                                                     # bank finally reverses at T+6 12:00
    k = contract(client, k["id"])
    assert k["status"] == "CLOSED" and k["proof"]["kept_original_deadline"] is False
    assert kinds(client, "C-1001")[-1] == "done_reversal"
    lines = client.get("/api/logs?since=0").get_json()["lines"]
    assert any("RBI TAT" in l["msg"] and l["component"] == "remediate" for l in lines)


# ------------------------------------------------------------------ S8
def test_s8_explain_then_fix(client):
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "en"})
    r = client.post("/api/message", json={"text": SCENARIOS["S8"]["texts"]["en"], "scenario_id": "S8"}).get_json()
    case = r["case"]
    msg = r["messages"][-1]
    assert case["status"] == "AWAITING_CHOICE" and msg["kind"] == "explain"
    for rid in ("S-8102", "C-5590", "C-5521"):
        assert rid in msg["text"]
    assert msg["choices"][0]["anomaly_id"] == "A-RAR-SB-77"
    r = client.post("/api/choice", json={"case_id": case["id"], "anomaly_id": "A-RAR-SB-77"}).get_json()
    c = r["case"]
    assert c["issue_type"] == "rent_after_return" and c["safety"]["decision"] == "ALLOW" and c["contract_id"]


def test_two_demo_users_in_status(client):
    s = client.get("/api/status").get_json()
    assert [u["type"] for u in s["users"]] == ["merchant", "customer"]


def test_cases_page(client):
    html = client.get("/cases").get_data(as_text=True)
    assert html.count("[Source title]") == 4 and "Why it happens" in html


def test_actual_transcript_kept_and_strict_snap(client):
    client.post("/api/login", json={"merchant_id": "C-1001", "lang": "en"})
    said = "My money is debited from home. I have not been told my credit."
    c = client.post("/api/message", json={"text": said, "focus": True}).get_json()["case"]
    assert c["text_original"] == said and c["scenario_id"] is None        # open-world, real words shown
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "en"})
    assert client.get("/api/chat?merchant_id=M-2291").get_json()["messages"] == []   # switching person clears chat
    c = client.post("/api/message", json={"text": "Customer paid 3624 rupees but no alert came", "focus": True}).get_json()["case"]
    assert c["intent"] == "payment_check"
    assert c["parsed"]["amounts"][0]["said"] == 3624 and c["parsed"]["amounts"][0]["snapped"] is None   # no ±15% snap
