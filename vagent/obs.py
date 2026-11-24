"""Structured JSON-lines event log, stage timings and run metrics."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_path: Path | None = None
timings: dict[str, float] = {}
counters: dict[str, int] = {}
_lock = threading.Lock()


def init(run_dir: Path) -> None:
    global _path
    _path = run_dir / "events.jsonl"
    timings.clear()
    counters.clear()


def event(name: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": name, **fields}
    line = json.dumps(rec, default=str)
    with _lock:
        if _path:
            with _path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if os.environ.get("VAGENT_LOG_JSON"):
            print(line, file=sys.stderr)


def inc(key: str, n: int = 1) -> None:
    with _lock:
        counters[key] = counters.get(key, 0) + n


@contextlib.contextmanager
def stage(name: str):
    t0 = time.perf_counter()
    event("stage_start", stage=name)
    print(f"[stage] {name} ...", flush=True)
    try:
        yield
    finally:
        dt = round(time.perf_counter() - t0, 2)
        timings[name] = round(timings.get(name, 0.0) + dt, 2)
        event("stage_end", stage=name, seconds=dt)
        print(f"[stage] {name} done in {dt}s", flush=True)
