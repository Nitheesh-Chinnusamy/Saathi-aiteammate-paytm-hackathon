"""llm.py - Gemini client via the OpenAI-compatible endpoint + deterministic mock.

Used ONLY where AI reasoning is needed:
  score_options  (Tier 1) "score, don't write": pick A..E|N among hypotheses with evidence ids
  tool_loop      (Tier 2) novel cases: read-only MCP tools, max 6 steps, every claim cited
  translate      typed free text not in the seed, when live
Mock (no GEMINI_API_KEY): deterministic outputs computed from the evidence / tool results by
a tiny rule planner, every line labelled "(mock)". Latency and tokens are logged for real calls.

Production: self-hosted open model (fine-tuned on resolved cases); prototype uses Gemini.
"""
import json
import os
import re
import time

from saathi.core import logger

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
STATS = {"calls": 0, "mock_calls": 0, "tokens_in": 0, "tokens_out": 0}


def is_live():
    return bool(os.environ.get("GEMINI_API_KEY"))


def model_name():
    return os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"


def reset_stats():
    for k in STATS:
        STATS[k] = 0


def _chat(messages, purpose, tools=None, case=None):
    import requests
    body = {"model": model_name(), "messages": messages, "reasoning_effort": "low"}
    if tools:
        body["tools"] = tools
    t0 = time.perf_counter()
    r = requests.post(URL, headers={"Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}",
                                    "Content-Type": "application/json"}, json=body, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    j = r.json()
    u = j.get("usage", {})
    STATS["calls"] += 1
    STATS["tokens_in"] += u.get("prompt_tokens", 0)
    STATS["tokens_out"] += u.get("completion_tokens", 0)
    if case is not None:
        case["gemini_calls"] = case.get("gemini_calls", 0) + 1
    logger.log("llm", f"{model_name()} {purpose} {ms:.0f} ms · in {u.get('prompt_tokens', '?')} tok · out {u.get('completion_tokens', '?')} tok")
    return j["choices"][0]["message"]


def _mock_count(case):
    STATS["mock_calls"] += 1
    if case is not None:
        case["gemini_calls"] = case.get("gemini_calls", 0) + 1


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


# ------------------------------------------------------------------ Tier 1
def validate_choice(obj, letters, evidence_ids):
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    if obj.get("choice") not in list(letters) + ["N"]:
        raise ValueError(f"bad choice {obj.get('choice')!r}")
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ValueError("confidence must be 0..1")
    ids = obj.get("evidence_ids")
    if not isinstance(ids, list) or any(i not in evidence_ids for i in ids):
        raise ValueError("evidence_ids must reference given evidence")
    return {"choice": obj["choice"], "confidence": float(conf), "evidence_ids": ids}


def score_options(evidence, options, case=None):
    """evidence: [{id, source, value}], options: {"A": {"hypothesis":..., "posterior":...}, ...}"""
    letters = list(options)
    ev_ids = [e["id"] for e in evidence]
    if is_live():
        prompt = ("Score, don't write. Pick the option best supported by the evidence. Reply with ONLY JSON "
                  '{"choice":"A".."E" or "N" (none fits),"confidence":0..1,"evidence_ids":[ids you relied on]}.\n'
                  f"Options: {json.dumps({k: v['hypothesis'] for k, v in options.items()})}\n"
                  f"Evidence: {json.dumps(evidence, ensure_ascii=False, default=str)}")
        try:
            msg = _chat([{"role": "user", "content": prompt}], "tier1 score_options", case=case)
            out = validate_choice(_extract_json(msg.get("content")), letters, ev_ids)
            logger.log("llm", f"tier1 → choice {out['choice']} conf {out['confidence']:.2f} · cites {out['evidence_ids']} · JSON ✓")
            return out
        except Exception as e:
            logger.log("llm", f"tier1 output rejected ({str(e)[:70]}) → N", level="warn")
            return {"choice": "N", "confidence": 0.0, "evidence_ids": [], "rejected": True}
    t0 = time.perf_counter()
    _mock_count(case)
    best = max(letters, key=lambda k: options[k]["posterior"])
    out = validate_choice({"choice": best, "confidence": round(min(0.95, options[best]["posterior"] + 0.1), 2),
                           "evidence_ids": ev_ids[:3]}, letters, ev_ids)
    logger.log("llm", f"{model_name()} tier1 score_options ({(time.perf_counter() - t0) * 1000:.1f} ms) → choice {out['choice']} "
                      f"conf {out['confidence']:.2f} · cites {out['evidence_ids']} (mock)")
    return out


# ------------------------------------------------------------------ Tier 2
def _openai_tools(mcp_tools):
    out = []
    for t in mcp_tools:
        if not t["annotations"]["readOnlyHint"] or t["name"] == "get_state_card":
            continue
        params = json.loads(json.dumps(t["inputSchema"]))
        params["properties"].pop("merchant_id", None)
        params["required"] = [r for r in params["required"] if r != "merchant_id"]
        params.pop("additionalProperties", None)
        out.append({"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": params}})
    return out


def _ids_in(result):
    ids = []
    for r in result.get("rows", []) if isinstance(result, dict) else []:
        if isinstance(r, dict) and r.get("id"):
            ids.append(r["id"])
    return ids


ALLOWED_FIXES = {"remote_restart": ["device_id"], "stop_rental": ["device_id"], "requeue_settlement": ["settlement_id"]}


def tool_loop(state_card, complaint_en, area, client, case=None, max_steps=6):
    """Returns {cause, summary, owner, fix, claims:[{text, evidence_ids}], confidence, evidence:{id: row}, steps}."""
    tools = client.list_tools()
    evidence = {d["id"]: {"source": "devices", **d} for d in state_card.get("devices", [])}
    if is_live():
        try:
            return _tool_loop_live(state_card, complaint_en, tools, client, case, evidence, max_steps)
        except Exception as e:
            logger.log("llm", f"tier2 live loop failed ({str(e)[:80]}) → mock planner", level="warn")
    return _tool_loop_mock(state_card, area, client, case, evidence, max_steps)


def _tool_loop_live(card, complaint, tools, client, case, evidence, max_steps):
    sys_prompt = ("You are Saathi's diagnosis engine for a Paytm merchant. Use the read-only tools to find the real cause. "
                  "When done reply with ONLY JSON: {\"cause\": snake_case, \"summary\": short, \"owner\": team, "
                  "\"fix\": {\"action\": one of remote_restart|stop_rental|requeue_settlement, \"params\": {...}} or null, "
                  "\"confidence\": 0..1, \"claims\": [{\"text\": ..., \"evidence_ids\": [row ids from tool results]}]}. "
                  "Every claim MUST cite ids that appeared in tool results.")
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Complaint: {complaint}\nState card: {json.dumps(card, ensure_ascii=False, default=str)[:6000]}"}]
    oa_tools = _openai_tools(tools)
    for step in range(1, max_steps + 1):
        msg = _chat(msgs, f"tier2 step {step}", tools=oa_tools, case=case)
        calls = msg.get("tool_calls") or []
        if not calls:
            out = _extract_json(msg.get("content"))
            out.update({"evidence": evidence, "steps": step})
            return out
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            args = json.loads(c["function"].get("arguments") or "{}")
            args.pop("merchant_id", None)
            logger.log("llm", f"tier2 step {step} → {name} {json.dumps(args)}")
            try:
                res = client.call(name, args)
                for r in res.get("rows", []):
                    if r.get("id"):
                        evidence[r["id"]] = {"source": res.get("source"), **r}
                content = json.dumps(res, ensure_ascii=False, default=str)[:4000]
            except Exception as e:
                content = json.dumps({"error": str(e)})
            msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": content})
    raise RuntimeError("tool loop exceeded max steps")


def _tool_loop_mock(card, area, client, case, evidence, max_steps):
    """Deterministic planner standing in for the model: picks tools by area, concludes from the data."""
    t0 = time.perf_counter()
    _mock_count(case)
    step = 0

    def use(name, args):
        nonlocal step
        step += 1
        logger.log("llm", f"tier2 step {step} → {name} {json.dumps(args)} (mock)")
        res = client.call(name, args)
        for r in res.get("rows", []):
            if r.get("id"):
                evidence[r["id"]] = {"source": res.get("source"), **r}
        return res

    devices = use("get_devices", {})["rows"]
    offline = []
    for d in devices[:2]:
        hb = use("get_device_heartbeat", {"device_id": d["id"]})
        if not hb.get("online"):
            offline.append((d, hb))
    pays = use("get_payments", {"hours": 24})["rows"]
    unalerted = [p for p in pays if p["status"] == "CREDITED" and not p["alert_sent"]]
    if offline and unalerted:
        d, hb = offline[0]
        hb_ids = [r["id"] for r in hb.get("rows", [])][:1]
        out = {"cause": "soundbox_offline_alerts_not_delivered",
               "summary": f"Payments are crediting fine; {d['id']} stopped sending heartbeats so alerts are not announced",
               "owner": "device-ops", "confidence": 0.86,
               "fix": {"action": "remote_restart", "params": {"device_id": d["id"]}},
               "claims": [{"text": f"{d['id']} offline, last heartbeat {d['last_heartbeat'][11:16]}", "evidence_ids": [d["id"]] + hb_ids},
                          {"text": f"{len(unalerted)} QR payments credited without an alert", "evidence_ids": [p["id"] for p in unalerted[:3]]}]}
    else:
        out = {"cause": "unclear", "summary": "No data explains the complaint", "owner": "support-desk", "confidence": 0.3,
               "fix": None, "claims": []}
    logger.log("llm", f"{model_name()} tier2 tool_loop {step} steps ({(time.perf_counter() - t0) * 1000:.0f} ms) → "
                      f"{out['cause']} conf {out['confidence']:.2f} (mock)")
    out.update({"evidence": evidence, "steps": step})
    return out


# ------------------------------------------------------------------ chat (greetings, status, small talk)
def converse(message, language, name, facts, case=None):
    """Live only. Classifies the message and writes a short reply. Returns None on any failure (rules take over)."""
    prompt = (f"You are Saathi, a friendly Paytm helper for a small shop owner named {name}. "
              f"Classify their message and reply in {language} only (1-2 short, simple sentences, warm tone). "
              "Never promise refunds or actions yourself. If the message describes any problem with money, rent, "
              "payments, Soundbox or account, the intent is \"complaint\". "
              f"Facts about their current request (use only if they ask about status): {facts}\n"
              f"Message: {message}\n"
              'Reply with ONLY JSON: {"intent": "greeting|thanks|status|complaint|other", "reply": "...", '
              '"reply_en": "English copy of reply", "english": "English translation of the message"}')
    try:
        msg = _chat([{"role": "user", "content": prompt}], "converse", case=case)
        out = _extract_json(msg.get("content"))
        if out.get("intent") not in ("greeting", "thanks", "status", "complaint", "other"):
            raise ValueError(f"bad intent {out.get('intent')!r}")
        if out["intent"] != "complaint" and not str(out.get("reply") or "").strip():
            raise ValueError("empty reply")
        logger.log("llm", f"converse → intent={out['intent']}" + ("" if out["intent"] == "complaint" else " · reply written by Gemini"))
        return out
    except Exception as e:
        logger.log("llm", f"converse failed ({str(e)[:70]}) → keyword rules", level="warn")
        return None


# ------------------------------------------------------------------ translate
def translate_to(text, language):
    """English → another language (live only). Used for replies that have no ready template."""
    if not is_live():
        return None
    try:
        msg = _chat([{"role": "user", "content": f"Translate into simple, friendly {language} for a shop owner. Copy numbers, ₹ amounts "
                                                 f"and record IDs exactly as written (do not add brackets around anything) and keep the line "
                                                 f"breaks. Reply with only the translation.\n\n{text}"}],
                    f"translate→{language}")
        return (msg.get("content") or "").strip() or None
    except Exception as e:
        logger.log("llm", f"translate→{language} failed ({str(e)[:60]}) → English", level="warn")
        return None


def translate(text, case=None):
    if not is_live():
        return None
    try:
        msg = _chat([{"role": "user", "content": "Translate to plain English. Reply with only the translation.\n\n" + text}],
                    "translate", case=case)
        return (msg.get("content") or "").strip()
    except Exception as e:
        logger.log("llm", f"translate failed ({str(e)[:60]}) → pass-through", level="warn")
        return None
