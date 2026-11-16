"""app.py - Flask routes + serves the UI (index + architecture) + the MCP endpoint (POST /mcp).

Run: pip install -r requirements.txt && python app.py   (http://127.0.0.1:5000)
Production: an API gateway + the agent service behind Paytm's merchant app; UI is an ops console.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request, session  # noqa: E402

from saathi.services import human_desk  # noqa: E402
from saathi.integrations import llm  # noqa: E402
from saathi.core import logger  # noqa: E402
from saathi.mcp import mcp_server  # noqa: E402
from saathi.services import notify  # noqa: E402
from saathi.services import pipeline  # noqa: E402
from saathi.integrations import sarvam  # noqa: E402
from saathi.steps import step4_act  # noqa: E402
from saathi.steps import step5_contract  # noqa: E402
from saathi.core import store  # noqa: E402
from saathi.core import trace  # noqa: E402
from saathi.core.clock import clock  # noqa: E402
from saathi.sim import seed  # noqa: E402
from saathi.sim.seed import LANGUAGES, SCENARIOS  # noqa: E402
from saathi.steps.step1_understand import DIRECTORY, CaseValidationError  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "saathi-dev-secret"
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.json.ensure_ascii = False
app.json.sort_keys = False   # keep the language list in its natural order

pipeline.reset_world()


def _mid():
    return session.get("merchant_id")


@app.before_request
def _tick():
    if request.path.startswith("/api/") and request.path != "/api/logs":
        pipeline.tick()


@app.errorhandler(Exception)
def _err(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    if code == 500:
        logger.log("human", f"server error {e.__class__.__name__}: {str(e)[:120]}", level="error")
    return jsonify({"error": str(e)}), code


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/architecture")
def architecture():
    return render_template("architecture.html")


# ------------------------------------------------------------------ status / session
@app.get("/api/status")
def status():
    return jsonify({
        "clock": clock.now_iso(), "start": "2026-11-14T10:00:00.000+05:30",
        "sarvam": "live" if sarvam.is_live() else "mock", "gemini": "live" if llm.is_live() else "mock",
        "gemini_model": llm.model_name(), "mcp_transport": os.environ.get("MCP_TRANSPORT", "inprocess"),
        "merchants": list(DIRECTORY.values()), "scenarios": SCENARIOS, "session_merchant": _mid(),
        "session_lang": session.get("lang"), "languages": LANGUAGES, "voices": sarvam.VOICES,
        "users": [DIRECTORY[seed.DEMO_USERS["merchant"]], DIRECTORY[seed.DEMO_USERS["customer"]]],
        "bank_late": bool(seed.sim_config("bank_late")),
        "pending_events": len(clock.pending()),
    })


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    mid = body.get("merchant_id")
    if mid not in DIRECTORY:
        return jsonify({"error": "unknown merchant"}), 400
    p = DIRECTORY[mid]
    lang = body.get("lang") if body.get("lang") in LANGUAGES else p["lang"]
    log_from = logger.last_id()
    session["merchant_id"], session["lang"] = mid, lang
    # every login starts with an empty chat: only messages from now on are shown
    session["chat_from"] = (store.q1("SELECT MAX(id) m FROM messages") or {}).get("m") or 0
    logger.log("auth", f"login (simulated Paytm session) → {mid} · {p['name']} · chose {LANGUAGES[lang]['name']}")
    return jsonify({"merchant": p, "lang": lang, "session_token": mcp_server.session_token(mid), "log_from": log_from})


@app.post("/api/logout")
def logout():
    session.pop("merchant_id", None)
    session.pop("lang", None)
    return jsonify({"ok": True})


@app.post("/api/tts")
def tts():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()[:1500]
    if not text:
        return jsonify({"error": "empty text"}), 400
    lang = body.get("lang") if body.get("lang") in LANGUAGES else "en"
    audio = sarvam.tts(text, lang, body.get("speaker"))
    return jsonify({"audio": audio, "mime": "audio/wav", "engine": "sarvam" if audio else "browser",
                    "lang_code": LANGUAGES[lang]["code"]})


def _require_login():
    mid = _mid()
    if not mid:
        return None, (jsonify({"error": "login first (identity comes from the session, never from text)"}), 401)
    return mid, None


# ------------------------------------------------------------------ conversation
@app.post("/api/message")
def message():
    mid, err = _require_login()
    if err:
        return err
    if request.files.get("audio"):
        f = request.files["audio"]
        audio = f.read()
        res = pipeline.handle_message(mid, audio=audio, audio_name=f.filename or "clip.webm", audio_mime=f.mimetype,
                                      scenario_id=request.form.get("scenario_id"), lang=session.get("lang"),
                                      focus=request.form.get("focus") == "1")
    else:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "empty message"}), 400
        res = pipeline.handle_message(mid, text=text, scenario_id=body.get("scenario_id"), lang=session.get("lang"),
                                      focus=bool(body.get("focus")))
    return jsonify(res)


@app.post("/api/choice")
def choice():
    mid, err = _require_login()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(pipeline.choose(mid, body.get("case_id"), body.get("anomaly_id")))
    except (PermissionError, CaseValidationError) as e:
        return jsonify({"error": str(e)}), 403


@app.post("/api/utr")
def utr():
    mid, err = _require_login()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(pipeline.provide_utr(mid, body.get("case_id"), str(body.get("utr") or "")))
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403


@app.post("/api/sim/bank_late")
def bank_late():
    return jsonify(pipeline.set_bank_late(bool((request.get_json(silent=True) or {}).get("on"))))


@app.get("/cases")
def cases_page():
    return render_template("cases.html")


@app.get("/api/chat")
def chat():
    mid = request.args.get("merchant_id") or _mid()
    rows = notify.thread(mid) if mid else []
    if mid and mid == session.get("merchant_id"):
        start = session.get("chat_from", 0)
        top = (store.q1("SELECT MAX(id) m FROM messages") or {}).get("m") or 0
        if start > top:                      # world was reset since login
            start = session["chat_from"] = 0
        rows = [r for r in rows if r["id"] > start]
    return jsonify({"messages": rows})


# ------------------------------------------------------------------ world controls
@app.post("/api/clock/advance")
def advance():
    hours = float((request.get_json(silent=True) or {}).get("hours", 1))
    if not 0 < hours <= 24 * 14:
        return jsonify({"error": "hours out of range"}), 400
    return jsonify(pipeline.advance(hours))


@app.post("/api/reset")
def reset():
    pipeline.reset_world()
    session.pop("merchant_id", None)
    session.pop("chat_from", None)
    return jsonify({"ok": True, "clock": clock.now_iso()})


@app.post("/api/spike")
def spike():
    return jsonify(pipeline.simulate_outage(int((request.get_json(silent=True) or {}).get("n", 40))))


# ------------------------------------------------------------------ observability
@app.get("/api/logs")
def logs():
    since = int(request.args.get("since", 0) or 0)
    return jsonify({"lines": logger.since(since), "last_id": logger.last_id(), "clock": clock.now_iso()})


@app.get("/api/cases")
def cases():
    rows = store.q("SELECT id, merchant_id, status, issue_type, incident_id, llm_used, source, created_at FROM cases "
                   "WHERE source='chat' ORDER BY created_at DESC, id DESC")
    return jsonify({"cases": rows})


@app.get("/api/cases/<cid>")
def case_detail(cid):
    c = store.load_case(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    return jsonify({"case": c, "trace": trace.get(cid),
                    "contract": step5_contract.load(c["contract_id"]) if c.get("contract_id") else None,
                    "desk": [d for d in human_desk.items() if d["case_id"] == cid]})


@app.get("/api/contracts")
def contracts():
    return jsonify({"contracts": step5_contract.all_contracts(), "clock": clock.now_iso()})


@app.get("/api/incidents")
def incidents():
    rows = store.q("SELECT * FROM incidents ORDER BY created_at DESC, id DESC")
    for r in rows:
        r["session_members"] = store.q1("SELECT COUNT(*) n FROM incident_members WHERE incident_id=?", (r["id"],))["n"]
    return jsonify({"incidents": rows})


@app.get("/api/desk")
def desk():
    return jsonify({"items": human_desk.items()})


@app.post("/api/desk/<did>/<action>")
def desk_action(did, action):
    try:
        return jsonify(pipeline.desk_action(did, action, (request.get_json(silent=True) or {}).get("operator", "ops.anita")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/audit")
def audit():
    return jsonify({"rows": store.q("SELECT seq, ts, case_id, action, prev_hash, hash, row_json FROM audit_log ORDER BY seq DESC")})


@app.post("/api/audit/verify")
def audit_verify():
    return jsonify(step4_act.verify_chain())


@app.get("/api/metrics")
def metrics():
    return jsonify(pipeline.metrics())


# ------------------------------------------------------------------ MCP endpoint (JSON-RPC 2.0)
@app.post("/mcp")
def mcp():
    tok = request.headers.get("X-Saathi-Session")
    mid = mcp_server.verify_session_token(tok) if tok else _mid()
    raw = request.get_data(as_text=True)
    out = mcp_server.handle_json(raw, {"merchant_id": mid} if mid else {})
    return app.response_class(out, mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Saathi on http://127.0.0.1:{port}  ·  sarvam={'live' if sarvam.is_live() else 'mock'} "
          f"gemini={'live' if llm.is_live() else 'mock'}  ·  MCP_TRANSPORT={os.environ.get('MCP_TRANSPORT', 'inprocess')}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
