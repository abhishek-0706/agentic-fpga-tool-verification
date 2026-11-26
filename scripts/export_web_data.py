"""Export a finished run into docs/data/run.json for the static web version (GitHub Pages).

Usage: python scripts/export_web_data.py [RUN_DIR] [--llm-run RUN_DIR]
Defaults: the run named in runs/latest.txt, and no LLM-mode evaluation.
Local paths are removed; the script refuses to write output that still contains one.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
OUT = ROOT / "docs" / "data" / "run.json"
DROP_KEYS = {"job_dir", "spec_path"}
LOCAL_PATH = re.compile(r"[A-Za-z]:\\\\|\\\\Users\\\\|/home/|/Users/")


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in DROP_KEYS}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, str) and str(ROOT) in x:  # absolute repo paths become repo-relative POSIX paths
        return re.sub(re.escape(str(ROOT)) + r"[\\/]([^\s\"']+)", lambda m: m.group(1).replace("\\", "/"), x)
    return x


def defects(run_id: str) -> tuple[list[dict], list[dict]]:
    p = RUNS / "tracker.sqlite"
    if not p.exists():
        return [], []
    db = sqlite3.connect(str(p))
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM defects WHERE last_run = ? ORDER BY id", (run_id,))]
    for r in rows:
        r["body"] = json.loads(r["body"]) if r.get("body") else {}
    ids = [r["id"] for r in rows]
    audit = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY ts")] if ids else []
    return rows, [a for a in audit if a["defect_id"] in ids]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?")
    ap.add_argument("--llm-run", help="run directory whose eval.json holds live_llm and gold_llm")
    a = ap.parse_args()
    run = Path(a.run) if a.run else RUNS / (RUNS / "latest.txt").read_text(encoding="utf-8").strip()
    metrics, plan = load(run / "metrics.json"), load(run / "plan.json")
    results, effective = load(run / "results.json"), load(run / "results_effective.json")
    first = {r["job_id"]: r for r in results}
    for r in effective:
        f = first.get(r["job_id"])
        if f and (f["status"], f["kind"]) != (r["status"], r["kind"]):
            r["first_attempt"] = {k: f[k] for k in ("status", "kind", "message")}
        gt = run / "jobs" / r["job_id"] / "injected.json"
        r["ground_truth"] = load(gt) if gt.exists() else []
    defect_rows, audit = defects(metrics["run_id"])
    ev = load(run / "eval.json")
    evaluation = {"rules": {k: ev[k] for k in ("live_rules", "gold_rules", "escapes") if k in ev}, "llm": None}
    if a.llm_run:
        lr = Path(a.llm_run)
        lev, lm = load(lr / "eval.json"), load(lr / "metrics.json")
        evaluation["llm"] = {"run_id": lm["run_id"], "calls": lm.get("llm"),
                             **{k: lev[k] for k in ("live_llm", "gold_llm") if lev.get(k)}}
    data = scrub({
        "run": {"id": metrics["run_id"], "profile": metrics["profile"], "mode": metrics["mode"], "tool": metrics["tool"]},
        "metrics": metrics,
        "flows": plan["flows"],
        "features": plan["features"],
        "testcases": load(run / "testcases.json"),
        "golden": load(ROOT / "golden" / "qor_golden.json"),
        "results": effective,
        "clusters": load(run / "clusters.json"),
        "defects": defect_rows,
        "audit": audit,
        "fixverify": load(run / "fixverify.json"),
        "coverage": load(run / "coverage.json"),
        "evaluation": evaluation,
    })
    text = json.dumps(data, indent=1, ensure_ascii=False)
    leaks = sorted(set(m.group(0) for m in LOCAL_PATH.finditer(text)))
    if leaks:
        sys.exit(f"refusing to write {OUT}: local paths remain ({leaks})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text) // 1024} KB) from run {metrics['run_id']}")


if __name__ == "__main__":
    main()
