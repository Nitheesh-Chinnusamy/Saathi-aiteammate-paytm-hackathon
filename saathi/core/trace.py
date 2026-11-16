"""trace.py - structured step-trace events for the UI "Saathi brain" view.

Each sub-step records: step number, title, status, result summary, evidence IDs, duration,
AI badge (none / ASR / clustering / Gemini tier) and the range of log line ids it emitted,
so a step card can link straight to its log lines.

Production: OpenTelemetry spans per step, rendered in an internal case viewer.
"""
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

from saathi.core import logger

STEPS = {1: "Understand", 2: "Diagnose", 3: "Safety check", 4: "Act", 5: "Contract", 6: "Verify", 7: "Remediate"}
_traces = defaultdict(list)
_lock = threading.RLock()


def emit(case_id, step, title, status="ok", result="", evidence=None, ms=0.0, ai="none", log_from=None, log_to=None):
    ev = {"case_id": case_id, "step": step, "step_name": STEPS.get(step, str(step)), "title": title, "status": status,
          "result": result, "evidence": list(evidence or []), "ms": round(ms, 1), "ai": ai,
          "log_from": log_from, "log_to": log_to, "seq": None}
    with _lock:
        ev["seq"] = len(_traces[case_id]) + 1
        _traces[case_id].append(ev)
    return ev


@contextmanager
def substep(case_id, step, title, ai="none"):
    """with trace.substep(cid, 2, 'Bayes update') as t: t['result'] = ...; t['evidence'] = [...]"""
    t = {"status": "ok", "result": "", "evidence": [], "ai": ai}
    start_log = logger.last_id() + 1
    t0 = time.perf_counter()
    with logger.context(case_id=case_id, step=step):
        try:
            yield t
        except Exception as e:
            t["status"] = "error"
            t["result"] = t["result"] or f"error: {e}"
            raise
        finally:
            emit(case_id, step, title, t["status"], t["result"], t["evidence"], (time.perf_counter() - t0) * 1000,
                 t["ai"], start_log, logger.last_id())


def get(case_id):
    with _lock:
        return list(_traces.get(case_id, []))


def clear():
    with _lock:
        _traces.clear()
