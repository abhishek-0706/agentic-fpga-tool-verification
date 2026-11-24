"""Stage 3: regression execution. Each job runs in its own process with a hard timeout."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import ROOT, obs, yosys
from .models import Flow, JobResult, JobSpec, TestCase

# Emulated infrastructure failures (first attempt only). Keyed by (testcase, flow).
INFRA_FAULTS = {("tc_counter", "xc7"): "hang", ("tc_shreg", "generic"): "enospc"}


def job_key(tc: str, flow: str) -> str:
    return f"{tc}__{flow}"


def golden_key(tc: str, command: str) -> str:
    return f"{tc}|{' '.join(command.split())}"


def load_golden() -> dict[str, dict[str, int]]:
    p = ROOT / "golden" / "qor_golden.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def make_job(jobs_dir: Path, tc: TestCase, flow: Flow, build: str, attempt: int = 1, tag: str = "",
             golden: dict | None = None, infra: bool = True) -> JobSpec:
    jid = job_key(tc.name, flow.id) + (f"__{tag}" if tag else "")
    d = jobs_dir / jid
    d.mkdir(parents=True, exist_ok=True)
    for stale in ("result.json", "injected.json", "netlist.json"):
        (d / stale).unlink(missing_ok=True)
    gold = (golden or {}).get(golden_key(tc.name, flow.command))
    spec = JobSpec(job_id=jid, testcase=tc, flow=flow, build=build, attempt=attempt, golden=gold,
                   infra_fault=INFRA_FAULTS.get((tc.name, flow.id)) if infra else None)
    (d / "design.v").write_text(tc.source, encoding="utf-8")
    (d / "job.json").write_text(spec.model_dump_json(indent=1), encoding="utf-8")
    return spec


def _run_one(d: Path, timeout: int) -> JobResult:
    spec = JobSpec.model_validate_json((d / "job.json").read_text(encoding="utf-8"))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    try:
        p = subprocess.run([sys.executable, "-m", "vagent.worker", str(d)], cwd=str(d), env=env,
                           capture_output=True, text=True, timeout=timeout)
        (d / "worker.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        if (d / "result.json").exists():
            res = JobResult.model_validate_json((d / "result.json").read_text(encoding="utf-8"))
        else:
            tail = [l for l in (p.stderr or "").splitlines() if l.strip()][-1:] or [f"exit code {p.returncode}"]
            res = JobResult(job_id=spec.job_id, testcase=spec.testcase.name, flow=spec.flow.id, build=spec.build,
                            attempt=spec.attempt, status="fail", kind="infra",
                            message=f"worker died (exit {p.returncode}): {tail[0]}", excerpt="\n".join(
                                (p.stderr or "").splitlines()[-5:]), job_dir=str(d))
    except subprocess.TimeoutExpired:
        res = JobResult(job_id=spec.job_id, testcase=spec.testcase.name, flow=spec.flow.id, build=spec.build,
                        attempt=spec.attempt, status="fail", kind="timeout",
                        message=f"job exceeded timeout of {timeout}s and was killed", duration_s=timeout,
                        job_dir=str(d))
    (d / "result.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    obs.event("job_done", job=res.job_id, build=res.build, status=res.status, kind=res.kind,
              equiv=res.equiv, seconds=res.duration_s)
    return res


def run_jobs(jobs: list[JobSpec], jobs_dir: Path, workers: int, timeout: int) -> list[JobResult]:
    yosys.warm()
    # longest-first scheduling: xilinx flows and sequential designs first
    order = sorted(jobs, key=lambda j: (j.flow.family != "xc7", not j.testcase.sequential))
    done = 0
    results: dict[str, JobResult] = {}

    def task(j: JobSpec) -> JobResult:
        return _run_one(jobs_dir / j.job_id, timeout)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(task, j) for j in order]):
            res = fut.result()
            done += 1
            results[res.job_id] = res
            mark = "PASS" if res.status == "pass" else f"FAIL[{res.kind}]"
            print(f"  [{done:3d}/{len(jobs)}] {res.job_id:<42} {mark:<18} {res.duration_s:6.1f}s", flush=True)
    return [results[j.job_id] for j in jobs]
