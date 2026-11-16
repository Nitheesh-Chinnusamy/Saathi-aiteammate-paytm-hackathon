"""M6: end-to-end through Flask (test client + clock advance), MCP over HTTP, log guarantees."""
import io
import json

import pytest

from saathi.sim import seed
from saathi.mcp.mcp_client import MCPClient, MCPError
from saathi.sim.seed import SCENARIOS


@pytest.fixture()
def client():
    import app as appmod
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c


def login(c, mid):
    r = c.post("/api/login", json={"merchant_id": mid})
    assert r.status_code == 200


def say(c, sid):
    sc = SCENARIOS[sid]
    login(c, sc["merchant_id"])
    r = c.post("/api/message", json={"text": sc["text"], "scenario_id": sid})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def adv(c, h):
    assert c.post("/api/clock/advance", json={"hours": h}).status_code == 200


def contract(c, kid):
    return next(k for k in c.get("/api/contracts").get_json()["contracts"] if k["id"] == kid)


def desk(c, kind, status="OPEN"):
    return [d for d in c.get("/api/desk").get_json()["items"] if d["kind"] == kind and d["status"] == status]


def test_message_requires_login(client):
    r = client.post("/api/message", json={"text": "hello"})
    assert r.status_code == 401


def test_s1_end_to_end(client):
    r = say(client, "S1")
    case = r["case"]
    assert case["llm_used"] == "none" and case["gemini_calls"] == 0
    assert case["incident"]["id"] == "INC-0412" and case["priority"] == "HIGH"
    assert case["safety"]["decision"] == "ALLOW"
    assert len(case["executed"]) == 2
    k = contract(client, case["contract_id"])
    assert k["promise"]["hours"] == 48
    adv(client, 24)
    k = contract(client, case["contract_id"])
    assert {c["name"]: c["status"] for c in k["checks"]} == {"rental_stopped": "PASS", "credit_posted": "FAIL"}
    adv(client, 24)
    k = contract(client, case["contract_id"])
    assert [s["step"] for s in k["ladder"]] == ["retry", "recheck", "chase_owner", "compensate", "notify_delay", "escalate", "rearm"]
    esc = desk(client, "escalation")
    assert len(esc) == 1
    r = client.post(f"/api/desk/{esc[0]['id']}/resolve", json={})
    assert r.status_code == 200
    k = contract(client, case["contract_id"])
    assert k["status"] == "CLOSED" and k["proof"]["checks"][1]["evidence"]
    detail = client.get(f"/api/cases/{case['id']}").get_json()
    assert detail["case"]["status"] == "CLOSED"
    steps = {t["step"] for t in detail["trace"]}
    assert steps == {1, 2, 3, 4, 5, 6, 7}
    msgs = [m["kind"] for m in client.get("/api/chat?merchant_id=M-2291").get_json()["messages"]]
    assert msgs == ["merchant", "promise_credit", "delay", "done_credit"]
    assert client.post("/api/audit/verify").get_json()["ok"]


def test_s2_same_incident_closed_in_hindi(client):
    r = say(client, "S2")
    assert r["case"]["incident"]["id"] == "INC-0412"
    adv(client, 48)
    k = contract(client, r["case"]["contract_id"])
    assert k["status"] == "CLOSED"
    done = [m for m in client.get("/api/chat?merchant_id=M-3107").get_json()["messages"] if m["kind"] == "done_credit"]
    assert done and done[0]["lang"] == "hi" and done[0]["text"] == seed.TEMPLATES["hi"]["done_credit"].format(amount="600")


def test_s3_choice_approval_close(client):
    r = say(client, "S3")
    assert r["case"]["status"] == "AWAITING_CHOICE"
    choice_msg = r["messages"][-1]
    assert choice_msg["kind"] == "choice" and len(choice_msg["choices"]) == 2
    settle = next(x for x in choice_msg["choices"] if x["rule"] == "settlement_held")
    r = client.post("/api/choice", json={"case_id": r["case"]["id"], "anomaly_id": settle["anomaly_id"]}).get_json()
    case = r["case"]
    assert case["incident"]["id"] == "INC-0420"
    assert case["safety"]["decision"] == "NEEDS_APPROVAL"
    ap = desk(client, "approval")
    assert ap and ap[0]["case_id"] == case["id"]
    client.post(f"/api/desk/{ap[0]['id']}/approve", json={})
    detail = client.get(f"/api/cases/{case['id']}").get_json()
    kid = detail["case"]["contract_id"]
    assert contract(client, kid)["promise"]["hours"] == 24
    adv(client, 6)
    assert contract(client, kid)["status"] == "CLOSED"


def test_s4_novel_tool_loop_draft_playbook(client):
    r = say(client, "S4")
    case = r["case"]
    assert case["match"]["status"] == "unmatched" and case["area"] in ("payments", "device")
    assert case["llm_used"] == "tier2"
    assert case["safety"]["decision"] == "NEEDS_APPROVAL"
    ap = desk(client, "approval")[0]
    client.post(f"/api/desk/{ap['id']}/approve", json={})
    adv(client, 0.25)
    assert client.get(f"/api/cases/{case['id']}").get_json()["case"]["status"] == "CLOSED"
    pbs = {p["id"]: p for p in client.get("/api/metrics").get_json()["playbooks"]}
    assert pbs["remote_restart"]["status"] == "DRAFT"
    assert desk(client, "playbook_review")


def test_s5_denied_flagged_refusal_hi(client):
    r = say(client, "S5")
    case = r["case"]
    assert case["status"] == "DENIED" and case["safety"]["decision"] == "DENY"
    failed = {x["name"] for x in case["safety"]["rules"] if not x["passed"]}
    assert {"no_payout_to_unlinked_account", "credit_not_above_disputed_charge"} <= failed
    assert desk(client, "blocked")
    refusal = r["messages"][-1]
    assert refusal["kind"] == "refusal" and refusal["lang"] == "hi" and refusal["text"] == seed.TEMPLATES["hi"]["refusal"]
    assert case["identity_claims_ignored"] is True
    assert client.get("/api/audit").get_json()["rows"] == []      # nothing executed


def test_spike_endpoint(client):
    r = client.post("/api/spike", json={"n": 40}).get_json()
    assert r["cases"] == 40 and len(r["incidents"]) == 1
    m = client.get("/api/metrics").get_json()
    assert m["tickets_per_incident"]["num"] >= 40


def test_voice_mock_path(client):
    login(client, "M-2291")
    r = client.post("/api/message", data={"scenario_id": "S1", "audio": (io.BytesIO(b"\x1aE\xdf\xa3fakewebm"), "clip.webm")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    case = r.get_json()["case"]
    assert case["input_mode"] == "voice" and case["asr_live"] is False
    lines = client.get("/api/logs?since=0").get_json()["lines"]
    sar = [l for l in lines if l["component"] == "sarvam"]
    assert sar and all("(mock)" in l["msg"] for l in sar)


# ------------------------------------------------------------------ MCP over HTTP
def http_transport(client):
    def send(raw, headers):
        return client.post("/mcp", data=raw, headers={**headers, "Content-Type": "application/json"}).get_data(as_text=True)
    return send


def test_mcp_over_http(client):
    c = MCPClient("M-2291", transport=http_transport(client))
    assert c.initialize()["serverInfo"]["name"]
    assert len(c.list_tools()) >= 14
    rows = c.call("get_devices", {})["rows"]
    assert rows[0]["id"] == "SB-77"
    with pytest.raises(MCPError) as e:
        c.call("get_devices", {"merchant_id": "M-3107"})
    assert e.value.code == -32001
    # no / forged session header -> rejected for tools/call
    raw = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_devices", "arguments": {"merchant_id": "M-2291"}}})
    out = client.post("/mcp", data=raw, headers={"X-Saathi-Session": "M-2291.forged", "Content-Type": "application/json"}).get_json()
    assert out["error"]["code"] == -32001


# ------------------------------------------------------------------ logs
def test_logs_since_and_components_and_masking(client):
    first = client.get("/api/logs?since=0").get_json()["last_id"]
    for sid in ["S1", "S2", "S4", "S5"]:
        say(client, sid)
    adv(client, 48)
    r = client.get(f"/api/logs?since={first}").get_json()
    lines = r["lines"]
    assert lines and all(l["id"] > first for l in lines)
    assert client.get(f"/api/logs?since={r['last_id']}").get_json()["lines"] == []
    comps = {l["component"] for l in lines}
    for comp in ["auth", "mcp-client", "mcp-server", "parse", "match", "cluster", "promise", "incident", "lifecycle", "bayes",
                 "llm", "policy", "token", "gateway", "audit", "contract", "verify", "clock", "sim", "remediate", "notify",
                 "learning", "human"]:
        assert comp in comps, comp
    # each step emits at least one line attributed to it
    steps = {l["step"] for l in lines if l["step"]}
    assert {1, 2, 3, 4, 5, 6, 7} <= steps
    assert all(l["msg"].startswith("[sim]") for l in lines if l["component"] == "sim")
    assert any("(mock)" in l["msg"] for l in lines if l["component"] == "llm")
    blob = json.dumps(lines, ensure_ascii=False)
    for h in seed.HEROES:
        assert h[5] not in blob and h[6] not in blob      # phone + bank account never appear unmasked


# ------------------------------------------------------------------ language chosen at login + voice replies
def test_language_chosen_at_login(client):
    r = client.post("/api/login", json={"merchant_id": "M-2291", "lang": "en"}).get_json()
    assert r["lang"] == "en"
    sc = SCENARIOS["S1"]
    res = client.post("/api/message", json={"text": sc["texts"]["en"], "scenario_id": "S1"}).get_json()
    assert res["case"]["lang"] == "en" and res["case"]["incident"]["id"] == "INC-0412"
    promise = res["messages"][-1]
    assert promise["lang"] == "en" and promise["text"].startswith("Your Soundbox rent has been stopped")


def test_every_scenario_text_recognised_in_every_language(client):
    from saathi.steps import step1_understand as s1
    for sid, sc in SCENARIOS.items():
        for lang, text in sc["texts"].items():
            c = s1.understand(sc["merchant_id"], text=text, lang=lang)
            assert c["text_english"] == sc["text_en"], (sid, lang)
            assert c["lang"] == lang


def test_tts_mock_falls_back_to_browser(client):
    r = client.post("/api/tts", json={"text": "నమస్తే", "lang": "te"}).get_json()
    assert r["engine"] == "browser" and r["audio"] is None and r["lang_code"] == "te-IN"
    lines = client.get("/api/logs?since=0").get_json()["lines"]
    assert any(l["component"] == "sarvam" and "tts" in l["msg"] and "(mock)" in l["msg"] for l in lines)


def test_all_languages_complete(client):
    from saathi.steps import step1_understand as s1
    kinds = set(seed.TEMPLATES["en"]) - set(seed.CONV_KINDS) - set(seed.NEW_KINDS)   # these are translated live
    assert {"ta", "kn", "ml", "mr", "bn", "gu", "pa", "od"} <= set(seed.LANGUAGES)
    for lang in seed.LANGUAGES:
        assert kinds <= set(seed.TEMPLATES[lang]), lang
        for sid in ("S1", "S2", "S3", "S4", "S5"):
            assert lang in SCENARIOS[sid]["texts"], (sid, lang)
        c = s1.understand("M-6001", text=SCENARIOS["S5"]["texts"][lang], lang=lang)
        assert c["identity_claims_ignored"], lang
        assert c["intent"] == "payout_request", lang


def test_tamil_end_to_end(client):
    client.post("/api/login", json={"merchant_id": "M-3107", "lang": "ta"})
    r = client.post("/api/message", json={"text": SCENARIOS["S2"]["texts"]["ta"], "scenario_id": "S2"}).get_json()
    assert r["case"]["incident"]["id"] == "INC-0412"
    assert r["messages"][-1]["lang"] == "ta" and "₹600" in r["messages"][-1]["text"]


# ------------------------------------------------------------------ chat first: only a real problem opens a request
def test_hello_does_not_open_a_request(client):
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "ta"})
    r = client.post("/api/message", json={"text": "Hello"}).get_json()
    assert r["case"] is None and r["intent"] == "greeting"
    assert r["messages"][0]["kind"] == "greet" and "Ramesh" in r["messages"][0]["text"]
    assert client.get("/api/audit").get_json()["rows"] == []          # nothing was done
    assert client.get("/api/cases").get_json()["cases"] == []


def test_status_question_before_and_after_a_request(client):
    client.post("/api/login", json={"merchant_id": "M-3107", "lang": "en"})
    r = client.post("/api/message", json={"text": "what is my status?"}).get_json()
    assert r["case"] is None and r["messages"][0]["kind"] == "status_none"
    client.post("/api/message", json={"text": SCENARIOS["S2"]["texts"]["en"], "scenario_id": "S2"})
    r = client.post("/api/message", json={"text": "any update on my request?"}).get_json()
    assert r["messages"][0]["kind"] == "status_progress"
    client.post("/api/clock/advance", json={"hours": 48})
    r = client.post("/api/message", json={"text": "status please"}).get_json()
    assert r["messages"][0]["kind"] == "status_done"


def test_free_text_problem_opens_a_request(client):
    client.post("/api/login", json={"merchant_id": "M-2291", "lang": "en"})
    r = client.post("/api/message", json={"text": "why is rent still being cut from my account?"}).get_json()
    assert r["case"] and r["case"]["issue_type"] == "rent_after_return"
