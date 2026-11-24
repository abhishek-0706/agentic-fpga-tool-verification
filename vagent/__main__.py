"""CLI: python -m vagent <command>"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import ROOT, RUNS


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vagent", description="Agentic verification of an FPGA synthesis tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the whole lifecycle end to end")
    d.add_argument("--profile", choices=["full", "quick"], default="full")
    d.add_argument("--mode", choices=["rules", "llm"], default="rules")
    d.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    d.add_argument("--timeout", type=int, default=150)
    d.add_argument("--approve-as", help="name of the human approving all drafted defects (omit to stop at drafts)")
    d.add_argument("--reset-tracker", action="store_true", help="start with an empty defect tracker")
    b = sub.add_parser("bless", help="regenerate golden QoR results from build=fixed")
    b.add_argument("--workers", type=int, default=8)
    a = sub.add_parser("approve", help="human approval: mark a drafted defect as filed")
    a.add_argument("ids", nargs="*")
    a.add_argument("--all", action="store_true")
    a.add_argument("--by", required=True, help="your name (recorded in the audit trail)")
    r = sub.add_parser("reject", help="reject a draft")
    r.add_argument("id")
    r.add_argument("--by", required=True)
    r.add_argument("--reason", default="")
    sub.add_parser("defects", help="list the tracker")
    for name in ("verify-fix", "coverage", "eval", "report", "build-gold"):
        p = sub.add_parser(name)
        p.add_argument("--run", default=None, help="run directory (default: latest)")
        p.add_argument("--workers", type=int, default=8)
    rj = sub.add_parser("rerun-job", help="re-run one job directory (e.g. a minimal reproducer)")
    rj.add_argument("job_dir")
    rj.add_argument("--build", choices=["buggy", "fixed"], default=None)
    sub.add_parser("mcp", help="start the MCP server (stdio)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    from .agent import GOLD_PATH, TRACKER_PATH, Agent, latest_run
    from .tracker import Tracker

    if args.cmd == "demo":
        RUNS.mkdir(exist_ok=True)
        if args.reset_tracker and TRACKER_PATH.exists():
            TRACKER_PATH.unlink()
        Agent(profile=args.profile, mode=args.mode, workers=args.workers, timeout=args.timeout).run_all(args.approve_as)
    elif args.cmd == "bless":
        bless(args.workers)
    elif args.cmd == "approve":
        tr = Tracker(TRACKER_PATH)
        ids = [x["id"] for x in tr.all("draft")] if args.all else args.ids
        for i in ids:
            tr.approve(i, args.by)
            print(f"{i} filed (approved by {args.by})")
    elif args.cmd == "reject":
        Tracker(TRACKER_PATH).reject(args.id, args.by, args.reason)
        print(f"{args.id} rejected (by {args.by})")
    elif args.cmd == "defects":
        for x in Tracker(TRACKER_PATH).all():
            print(f"{x['id']}  {x['status']:<8} {x['severity']} {x['category']:<11} seen={x['occurrences']}  {x['title']}")
    elif args.cmd == "rerun-job":
        from .models import JobSpec
        from .runner import _run_one

        jd = Path(args.job_dir).resolve()
        spec = JobSpec.model_validate_json((jd / "job.json").read_text(encoding="utf-8"))
        upd = {"attempt": spec.attempt + 1, **({"build": args.build} if args.build else {})}
        (jd / "job.json").write_text(spec.model_copy(update=upd).model_dump_json(indent=1), encoding="utf-8")
        for stale in ("result.json", "injected.json", "netlist.json"):
            (jd / stale).unlink(missing_ok=True)
        res = _run_one(jd, 600)
        print(json.dumps(res.model_dump(exclude={"excerpt", "cells"}), indent=1))
    elif args.cmd == "mcp":
        from .mcp_server import serve

        serve()
    else:
        run_dir = Path(args.run) if args.run else latest_run()
        ag = Agent(run_dir=run_dir, workers=args.workers, mode=None)
        if args.cmd == "verify-fix":
            ag.stage_verify_fix()
            ag.stage_report()
        elif args.cmd == "coverage":
            ag.stage_coverage()
        elif args.cmd == "eval":
            ag.stage_eval()
        elif args.cmd == "report":
            ag.stage_report()
        elif args.cmd == "build-gold":
            build_gold(ag, GOLD_PATH)


def bless(workers: int) -> None:
    from . import generators, runner, spec

    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    suite = generators.generated_suite(plan) + generators.legacy_suite()
    tmp = RUNS / "_bless"
    shutil.rmtree(tmp, ignore_errors=True)
    jobs = [runner.make_job(tmp, tc, f, "fixed", infra=False) for tc in suite for f in plan.flows]
    res = runner.run_jobs(jobs, tmp, workers, 300)
    # golden = what the reference build produced, whether or not a testcase's own expectation holds
    golden = {runner.golden_key(j.testcase.name, j.flow.command): r.families
              for j, r in zip(jobs, res) if r.cells and r.kind != "equiv_fail"}
    out = ROOT / "golden" / "qor_golden.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(golden, indent=1, sort_keys=True), encoding="utf-8")
    print(f"golden written for {len(golden)}/{len(res)} jobs -> {out}")
    print("failing on build=fixed:", [f"{r.job_id}:{r.kind}" for r in res if r.status == "fail"])


def build_gold(ag, path: Path) -> None:
    from . import evaluate

    items = evaluate.harvest_gold(ag.clusters(), ag.results(), ag.run_id)
    hand = ROOT / "eval" / "handwritten_cases.jsonl"
    lines = [json.dumps(i) for i in items]
    if hand.exists():
        lines += [l for l in hand.read_text(encoding="utf-8").splitlines() if l.strip()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"gold set: {len(items)} harvested + {len(lines) - len(items)} handwritten -> {path}")


if __name__ == "__main__":
    sys.exit(main())
