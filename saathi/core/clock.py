"""clock.py - virtual clock (starts 2026-11-14 10:00 IST) + scheduled-event heap.

The demo needs hours of "system time" (rental batches, bank settlements, promise
deadlines) to pass in seconds, so every module reads time from here, never from
datetime.now(). Time ticks in real time between jumps; [+1h]/[+24h] advance it and
fire due scheduled events in timestamp order.

Production: Temporal durable timers / a workflow engine's timer service.
"""
import heapq
import itertools
import threading
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 11, 14, 10, 0, 0, tzinfo=IST)


def iso(dt):
    return dt.astimezone(IST).isoformat(timespec="milliseconds")


def parse(s):
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def hours_between(a, b):
    return (parse(b) - parse(a)).total_seconds() / 3600.0


class VirtualClock:
    def __init__(self):
        self._lock = threading.RLock()
        self._listeners = []          # fn(kind, info) - kind: 'advanced' | 'reset'
        self.reset()

    # ------------------------------------------------------------------ time
    def reset(self):
        with self._lock:
            self._base = START
            self._anchor = time.monotonic()
            self._ticking = True
            self._heap = []
            self._seq = itertools.count()

    def set_ticking(self, on):
        with self._lock:
            self._jump(self.now())
            self._ticking = bool(on)

    def now(self):
        with self._lock:
            if not self._ticking:
                return self._base
            return self._base + timedelta(seconds=time.monotonic() - self._anchor)

    def now_iso(self):
        return iso(self.now())

    def _jump(self, t):
        self._base = t
        self._anchor = time.monotonic()

    # -------------------------------------------------------------- schedule
    def schedule_at(self, when, name, fn, payload=None):
        when = parse(when)
        with self._lock:
            heapq.heappush(self._heap, (when, next(self._seq), name, fn, payload or {}))
        return iso(when)

    def schedule_in(self, seconds, name, fn, payload=None):
        return self.schedule_at(self.now() + timedelta(seconds=seconds), name, fn, payload)

    def pending(self):
        with self._lock:
            return [{"at": iso(w), "name": n, "payload": p} for (w, _, n, _, p) in sorted(self._heap, key=lambda e: (e[0], e[1]))]

    def _pop_due(self, until):
        with self._lock:
            if self._heap and self._heap[0][0] <= until:
                return heapq.heappop(self._heap)
            return None

    def fire_due(self):
        """Fire events whose time has already passed in real ticking time."""
        fired = 0
        while True:
            ev = self._pop_due(self.now())
            if not ev:
                return fired
            self._run(ev)
            fired += 1

    def advance(self, seconds):
        """Jump forward, firing every scheduled event in order at its own timestamp."""
        start = self.now()
        target = start + timedelta(seconds=seconds)
        fired = 0
        while True:
            ev = self._pop_due(target)
            if not ev:
                break
            with self._lock:
                if ev[0] > self.now():
                    self._jump(ev[0])
            self._run(ev)
            fired += 1
        with self._lock:
            self._jump(max(target, self.now()))
        for fn in list(self._listeners):
            fn("advanced", {"from": iso(start), "to": self.now_iso(), "fired": fired, "seconds": seconds})
        return {"from": iso(start), "to": self.now_iso(), "fired": fired}

    def _run(self, ev):
        when, _, name, fn, payload = ev
        try:
            fn(**payload)
        except Exception as e:  # never let one bad event stop the clock
            from saathi.core import logger
            logger.log("clock", f"event {name} failed: {e!r}", level="error")

    def on(self, fn):
        self._listeners.append(fn)


clock = VirtualClock()
