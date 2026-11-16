"""logger.py - structured one-line backend logs, ring buffer of 2,000 lines.

Every line is emitted by the real code path at the moment it happens (HONEST LOGS RULE):
there are no hard-coded narrative lines anywhere. Lines carry virtual time + real epoch ms,
the component, and the case/step context so UI step cards can link to their lines.
A last-line-of-defence scrubber masks any long digit run (phone / account numbers).

Production: OpenTelemetry structured logs shipped to Loki/Elastic with trace ids.
"""
import re
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager

from saathi.core.clock import clock

COMPONENTS = ["auth", "mcp-client", "mcp-server", "db", "sarvam", "parse", "match", "cluster",
              "promise", "incident", "lifecycle", "bayes", "llm", "policy", "token", "gateway",
              "audit", "contract", "verify", "clock", "sim", "remediate", "notify", "learning", "human"]

_buf = deque(maxlen=2000)
_lock = threading.RLock()
_next_id = [1]
_ctx = threading.local()
_DIGITS = re.compile(r"(?<![\w-])(\d{2})\d{4,}(\d{2})(?!\d)")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

QUIET_STDOUT = False


def scrub(text):
    """Mask any 8+ digit run: 9876543210 -> 98xxxxxx10."""
    return _DIGITS.sub(lambda m: m.group(1) + "x" * (len(m.group(0)) - 4) + m.group(2), text)


def _clean(v):
    if isinstance(v, str):
        return scrub(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


@contextmanager
def context(**kw):
    old = dict(getattr(_ctx, "v", {}))
    _ctx.v = {**old, **kw}
    try:
        yield
    finally:
        _ctx.v = old


@contextmanager
def muted():
    """Drop info lines emitted by this thread (bulk operations log one honest summary instead)."""
    old = getattr(_ctx, "muted", False)
    _ctx.muted = True
    try:
        yield
    finally:
        _ctx.muted = old


def log(component, msg, level="info", **fields):
    if getattr(_ctx, "muted", False) and level not in ("warn", "error"):
        return None
    ctx = getattr(_ctx, "v", {})
    with _lock:
        lid = _next_id[0]
        _next_id[0] += 1
        vt = clock.now_iso()
        line = {"id": lid, "virtual_ts": vt, "real_ms": int(time.time() * 1000), "component": component,
                "level": level, "msg": scrub(str(msg)), "fields": _clean(fields),
                "case_id": ctx.get("case_id"), "step": ctx.get("step")}
        _buf.append(line)
    if not QUIET_STDOUT:
        try:
            print(f"{vt[11:23]} [{component:<10}] {line['msg']}", flush=True)
        except Exception:
            pass
    return lid


def last_id():
    with _lock:
        return _next_id[0] - 1


def since(after_id=0, limit=2000):
    with _lock:
        return [l for l in _buf if l["id"] > after_id][:limit]


def clear():
    with _lock:
        _buf.clear()
