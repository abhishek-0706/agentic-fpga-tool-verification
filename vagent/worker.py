"""One regression job, run as its own process: `python -m vagent.worker <job_dir>`.

tool (toolshim) -> QoR checks -> equivalence oracle -> result.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from . import oracles, toolshim, yosys
from .models import JobResult, JobSpec


def _first_error(log: str) -> str:
    for line in log.splitlines():
        if "ERROR" in line:
            return line.strip()
    return "tool exited with non-zero status and no ERROR line"


def _excerpt(log: str, needle: str, before: int = 6, after: int = 3) -> str:
    lines = log.splitlines()
    idx = next((i for i, l in enumerate(lines) if needle and needle in l), len(lines) - 1)
    return "\n".join(lines[max(0, idx - before): idx + after + 1])


def classify_tool_error(msg: str) -> str:
    if re.search(r"syntax error|Parser error|parse error|unexpected", msg, re.I):
        return "parse_error"
    if re.search(r"not found|hierarchy|Can't find|is not part of the design|Unknown module", msg, re.I):
        return "elab_error"
    return "crash"


def write_ground_truth(d: Path, label: str, detail: str) -> None:
    gt_p = d / "injected.json"
    gt = json.loads(gt_p.read_text(encoding="utf-8")) if gt_p.exists() else []
    gt.append({"label": label, "detail": detail})
    gt_p.write_text(json.dumps(gt), encoding="utf-8")


def run_job(d: Path) -> JobResult:
    spec = JobSpec.model_validate_json((d / "job.json").read_text(encoding="utf-8"))
    tc, flow = spec.testcase, spec.flow
    t0 = time.perf_counter()
    res = JobResult(job_id=spec.job_id, testcase=tc.name, flow=flow.id, build=spec.build, attempt=spec.attempt,
                    status="pass", job_dir=str(d))

    if spec.infra_fault and spec.attempt == 1:  # emulated infrastructure failures (first attempt only)
        write_ground_truth(d, "infrastructure", spec.infra_fault)
        if spec.infra_fault == "hang":
            time.sleep(3600)
        print("OSError: [Errno 28] No space left on device: 'netlist.json'", file=sys.stderr)
        sys.exit(3)

    rc, injected = toolshim.run_tool(tc.source, tc.top, flow, spec.build, d)
    for i in injected:
        write_ground_truth(d, "known_issue" if i.startswith("KDB") else "tool_defect", i)
    if tc.ground_truth:
        write_ground_truth(d, tc.ground_truth, "testcase")

    log = (d / "synth.log").read_text(encoding="utf-8", errors="replace")
    if rc != 0:
        msg = _first_error(log)
        res.status, res.kind, res.message = "fail", classify_tool_error(msg), msg
        res.excerpt = _excerpt(log, msg)
    else:
        cells = yosys.count_cells(yosys.load_netlist(d / "netlist.json"), tc.top)
        fams = yosys.families(cells)
        res.cells, res.families = cells, fams
        res.qor_failures = oracles.check_expectations(tc, flow, cells, fams)
        drift = oracles.golden_drift(fams, spec.golden)
        res.golden_match = None if spec.golden is None else not drift
        res.equiv, res.equiv_note = oracles.equivalence(tc, flow, d, fams, spec.sat_timeout)
        if res.equiv == "inconclusive" and res.equiv_note.startswith("oracle error"):
            res.status, res.kind, res.message = "fail", "infra", f"equivalence oracle could not run: {res.equiv_note}"
            res.excerpt = _excerpt((d / "equiv.log").read_text(encoding="utf-8", errors="replace"), "ERROR")
        elif res.equiv == "fail":
            res.status, res.kind, res.message = "fail", "equiv_fail", f"equivalence check failed: {res.equiv_note}"
            res.excerpt = _excerpt((d / "equiv.log").read_text(encoding="utf-8", errors="replace"),
                                   "model found: FAIL", before=2, after=1)
        elif drift:
            res.status, res.kind = "fail", "qor_golden"
            res.message = "QoR drift vs golden: " + "; ".join(drift)
            if res.qor_failures:
                res.message += " | expectation: " + "; ".join(res.qor_failures)
        elif res.qor_failures:
            res.status, res.kind = "fail", "qor_expect"
            res.message = "QoR expectation unmet: " + "; ".join(res.qor_failures)
        if res.kind in ("qor_golden", "qor_expect"):
            res.excerpt = "cells: " + json.dumps(fams)
    res.duration_s = round(time.perf_counter() - t0, 2)
    return res


def main() -> None:
    d = Path(sys.argv[1]).resolve()
    res = run_job(d)
    (d / "result.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
