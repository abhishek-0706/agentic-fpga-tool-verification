"""The verification agent: runs the lifecycle stages and keeps run state on disk so any stage can
be resumed or called on its own (CLI or MCP)."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import ROOT, RUNS, coverage, evaluate, generators, llm, obs, report, runner, spec, triage, yosys
from .models import Cluster, DefectDraft, JobResult, JobSpec, TestCase, TestPlan, TriageDecision
from .tracker import Tracker

PROFILES = {
    "full": None,
    "quick": {"testcases": {"tc_comb_mux", "tc_mul_reg", "tc_lutram", "tc_legacy_cmp4", "tc_legacy_parity"},
              "flows": {"generic", "xc7", "xc7_flatten", "xc7_flat_noio"}},
}
TRACKER_PATH = RUNS / "tracker.sqlite"
GOLD_PATH = ROOT / "eval" / "triage_gold.jsonl"


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)),
                    encoding="utf-8")


def latest_run() -> Path:
    p = RUNS / "latest.txt"
    if not p.exists():
        raise SystemExit("no run yet: run `python -m vagent demo` first")
    return Path(p.read_text(encoding="utf-8").strip())


class Agent:
    def __init__(self, run_dir: Path | None = None, profile: str = "full", mode: str = "rules",
                 workers: int = 8, timeout: int = 150):
        if run_dir is None:
            run_dir = RUNS / f"{datetime.now():%Y%m%d-%H%M%S}-{profile}"
            run_dir.mkdir(parents=True)
            (RUNS / "latest.txt").write_text(str(run_dir), encoding="utf-8")
            _dump(run_dir / "state.json", {"profile": profile, "mode": mode, "workers": workers,
                                           "timeout": timeout, "stages": {}})
        self.dir = run_dir
        st = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.state = st
        self.profile, self.workers, self.timeout = st["profile"], workers or st["workers"], st["timeout"]
        self.mode = mode or st["mode"]
        if self.mode == "llm" and llm.provider_name() is None:
            print("[agent] mode=llm requested but no provider is configured; using rules")
            self.mode = "rules"
        self.state["mode"] = self.mode
        obs.init(run_dir)
        self.run_id = run_dir.name

    # ------------------------------------------------------------------ helpers
    def _save_state(self, stage: str, **info) -> None:
        self.state["stages"][stage] = {"done": datetime.now().isoformat(timespec="seconds"), **info}
        self.state["timings"] = {**self.state.get("timings", {}), **obs.timings}
        _dump(self.dir / "state.json", self.state)

    def _load(self, name: str):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def plan_obj(self) -> TestPlan:
        return TestPlan.model_validate(self._load("plan.json"))

    def suite(self) -> list[TestCase]:
        return [TestCase.model_validate(t) for t in self._load("testcases.json")]

    def results(self, name: str = "results.json") -> list[JobResult]:
        return [JobResult.model_validate(r) for r in self._load(name)]

    def clusters(self) -> list[Cluster]:
        return [Cluster.model_validate(c) for c in self._load("clusters.json")]

    def _batch(self, pairs: list[tuple[TestCase, object]], tag: str, build: str = "buggy",
               sub: str = "jobs") -> list[JobResult]:
        jobs = [runner.make_job(self.dir / sub, tc, fl, build, tag=tag, golden=runner.load_golden(), infra=False)
                for tc, fl in pairs]
        return runner.run_jobs(jobs, self.dir / sub, max(1, min(self.workers, len(jobs))), self.timeout)

    # ------------------------------------------------------------------ stages
    def stage_plan(self) -> TestPlan:
        with obs.stage("1_spec_to_plan"):
            plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
            prof = PROFILES[self.profile]
            if prof:
                for f in plan.flows:
                    f.in_regression = f.in_regression and f.id in prof["flows"]
            _dump(self.dir / "plan.json", plan.model_dump())
            print(f"  {len(plan.features)} features, {len(plan.regression_flows)} regression flows "
                  f"({', '.join(f.id for f in plan.regression_flows)})")
        self._save_state("plan", features=len(plan.features))
        return plan

    def stage_generate(self) -> list[TestCase]:
        plan = self.plan_obj()
        with obs.stage("2_test_generation"):
            suite = generators.generated_suite(plan)
            gate_fail = []
            for tc in suite:
                ok, why = yosys.gate(tc.source, tc.top)
                obs.event("gate", testcase=tc.name, admitted=ok)
                if not ok:
                    gate_fail.append((tc.name, why))
            if gate_fail:
                raise RuntimeError(f"generated testcases failed the Yosys gate: {gate_fail}")
            suite += generators.legacy_suite()
            prof = PROFILES[self.profile]
            if prof:
                suite = [t for t in suite if t.name in prof["testcases"]]
            _dump(self.dir / "testcases.json", [t.model_dump() for t in suite])
            print(f"  {len(suite)} testcases ({sum(t.origin == 'generated' for t in suite)} generated and gated, "
                  f"{sum(t.origin == 'legacy' for t in suite)} legacy)")
        self._save_state("generate", testcases=len(suite))
        return suite

    def stage_regress(self) -> list[JobResult]:
        plan, suite, golden = self.plan_obj(), self.suite(), runner.load_golden()
        with obs.stage("3_4_execute_and_oracles"):
            jobs = [runner.make_job(self.dir / "jobs", tc, f, "buggy", golden=golden)
                    for tc in suite for f in plan.regression_flows]
            print(f"  {len(jobs)} jobs on {self.workers} workers, timeout {self.timeout}s, build=buggy, "
                  f"golden={'yes' if golden else 'MISSING'}")
            res = runner.run_jobs(jobs, self.dir / "jobs", self.workers, self.timeout)
            _dump(self.dir / "results.json", [r.model_dump() for r in res])
        n_fail = sum(r.status == "fail" for r in res)
        print(f"  pass {len(res) - n_fail}/{len(res)}")
        self._save_state("regress", jobs=len(res), failures=n_fail)
        return res

    def stage_triage(self) -> list[Cluster]:
        plan, res = self.plan_obj(), self.results()
        flows = {f.id: f for f in plan.flows}
        specs = {p.name: JobSpec.model_validate_json((p / "job.json").read_text(encoding="utf-8"))
                 for p in (self.dir / "jobs").iterdir() if (p / "job.json").exists()}
        with obs.stage("6_triage"):
            # 1. rerun infrastructure suspects once (flaky detection)
            suspects = [r for r in res if r.status == "fail" and r.kind in ("timeout", "infra")]
            rerun = {}
            if suspects:
                print(f"  rerunning {len(suspects)} infrastructure suspects")
                rr = self._batch([(specs[r.job_id].testcase, specs[r.job_id].flow) for r in suspects], "rerun",
                                 sub="rerun")
                rerun = {s.job_id: x for s, x in zip(suspects, rr)}
            effective = [rerun[r.job_id] if r.job_id in rerun and rerun[r.job_id].status == "pass" else r
                         for r in res]
            effective = [r.model_copy(update={"job_id": r.job_id.removesuffix("__rerun")}) for r in effective]
            _dump(self.dir / "results_effective.json", [r.model_dump() for r in effective])
            # 2. cluster + rule classification (+ known-defect matching)
            rerun_passed = {j for j, x in rerun.items() if x.status == "pass"}
            clusters = triage.cluster_failures(res, flows, triage.load_known(), rerun_passed)
            for cl in clusters:
                cl.rules_decision = cl.decision
            # 3. optional LLM reasoning, validated; rules remain the fallback
            if self.mode == "llm":
                for cl in clusters:
                    out = llm.structured(
                        "Classify this regression failure cluster as tool_defect, test_bug, infrastructure or "
                        "known_issue, name the tool component, and justify from the evidence only.",
                        cl.evidence.model_dump(), TriageDecision)
                    if out:
                        cl.decision, cl.decided_by = out, "llm"
            # 4. minimal reproducers for tool defects (delta reduction over options and parameters)
            todo = [cl for cl in clusters if cl.decision.category == "tool_defect"
                    and cl.evidence.kind not in ("timeout", "infra")]
            if todo:
                print(f"  delta-reducing {len(todo)} tool-defect clusters")
                with ThreadPoolExecutor(max_workers=len(todo)) as ex:
                    reps = list(ex.map(lambda cl: triage.reduce_cluster(
                        cl, {r.job_id: r for r in res}, specs,
                        lambda pairs, tag, cid=cl.id: self._batch(pairs, f"{cid}_{tag}", sub="reduce"),
                        self.dir), todo))
                for cl, rep in zip(todo, reps):
                    cl.reproducer = rep
            _dump(self.dir / "clusters.json", [c.model_dump() for c in clusters])
        for cl in clusters:
            e = cl.evidence
            print(f"  {cl.id} {e.kind:<11} {cl.decision.category:<14} n={len(cl.members):<2} "
                  f"fp={','.join(e.fingerprint) or '-':<22} {e.message[:60]}")
        self._save_state("triage", clusters=len(clusters))
        return clusters

    def draft_for(self, cl: Cluster) -> DefectDraft | None:
        ev, dec = cl.evidence, cl.decision
        if dec.category not in ("tool_defect", "test_bug"):
            return None
        sev = {"equiv_fail": "S1", "crash": "S2", "timeout": "S2"}.get(ev.kind, "S3")
        what = {"equiv_fail": "synthesised netlist is not equivalent to RTL",
                "qor_golden": "resource usage regressed versus golden",
                "qor_expect": "resource expectation not met", "crash": "tool internal error",
                "parse_error": "testcase does not parse", "elab_error": "testcase does not elaborate",
                "timeout": "job hangs"}.get(ev.kind, ev.kind)
        opts = [o for o in ev.fingerprint if o.startswith("-")]
        if dec.category == "tool_defect":
            title = f"[{dec.component}] {what}{' with ' + ' '.join(opts) if opts else ''} ({len(ev.testcases)} testcase(s))"
        else:
            title = f"[testsuite] {', '.join(ev.testcases)}: {what}"
        rep = cl.reproducer
        steps = [f"python -m vagent rerun-job {rep.job_dir}"] if rep else [
            f"python -m vagent rerun-job {self.dir / 'jobs' / cl.members[0]}"]
        if rep:
            steps.append(f"Yosys command: {rep.flow_command} -top <top> (options removed by reduction: "
                         f"{', '.join(rep.removed_options) or 'none'}; params {rep.params})")
        steps.append("Compare synth.log, equiv.log and result.json in that directory")
        expected = {"equiv_fail": "Netlist equivalent to RTL (SAT miter finds no counterexample)",
                    "qor_golden": "Primitive usage equal to the golden reference build",
                    "qor_expect": "Resource expectations met", "crash": "Synthesis completes without error",
                    "parse_error": "Testcase parses", "elab_error": "Testcase elaborates with the declared top",
                    }.get(ev.kind, "Job passes")
        d = DefectDraft(title=title[:160], component=dec.component, severity=sev, category=dec.category,
                        summary=f"{dec.rationale}\nFailing flows: {', '.join(ev.flows_failed)}. Passing flows: "
                                f"{', '.join(ev.flows_passed) or 'none'}. Signature {ev.signature}.",
                        repro_steps=steps, expected=expected, actual=f"{ev.message}\n{ev.excerpt[-600:]}",
                        affected=cl.members, signature=ev.signature)
        if self.mode == "llm":
            out = llm.structured("Improve this defect report's title and summary for a tool developer. Keep "
                                 "facts unchanged.", {"draft": d.model_dump(), "evidence": ev.model_dump()},
                                 DefectDraft)
            if out:  # facts are pinned to the deterministic values; the LLM only rewrites prose
                d = out.model_copy(update={"signature": d.signature, "category": d.category, "affected": d.affected,
                                           "repro_steps": d.repro_steps, "severity": d.severity,
                                           "actual": d.actual, "expected": d.expected, "component": d.component})
        return d

    def stage_defects(self, approve_as: str | None = None) -> list[dict]:
        clusters = self.clusters()
        tr = Tracker(TRACKER_PATH)
        with obs.stage("7_defect_management"):
            for cl in clusters:
                d = self.draft_for(cl)
                if d is None:
                    if cl.decision.category == "known_issue":
                        obs.event("known_issue_occurrence", cluster=cl.id, kdb=cl.evidence.known_match)
                    continue
                did, new = tr.upsert_draft(d, self.run_id)
                cl.defect_id = did
                how = ("reopened: failed again after closing" if tr.get(did)["occurrences"] > 1 else "new draft") \
                    if new else "duplicate of existing"
                print(f"  {cl.id} -> {did} ({how}) {d.title[:80]}")
            _dump(self.dir / "clusters.json", [c.model_dump() for c in clusters])
            if approve_as:
                for row in tr.all("draft"):
                    tr.approve(row["id"], approve_as)
                    print(f"  {row['id']} approved by {approve_as!r} and filed")
            else:
                drafts = tr.all("draft")
                if drafts:
                    print(f"  {len(drafts)} draft(s) await human approval: python -m vagent approve <ID> --by <name>")
        self._save_state("defects")
        return tr.all()

    def stage_verify_fix(self) -> dict:
        tr = Tracker(TRACKER_PATH)
        clusters = {c.defect_id: c for c in self.clusters() if c.defect_id}
        filed = [d for d in tr.all("filed") if d["category"] == "tool_defect" and d["id"] in clusters]
        out = {"defects": [], "delta": {"fixed": [], "still_failing": [], "new_failures": [], "still_passing": []}}
        if not filed:
            prev = self.dir / "fixverify.json"
            if prev.exists():
                print("  no filed tool defects left to verify; keeping the earlier fixverify.json")
                self._save_state("verify_fix", closed=0)
                return json.loads(prev.read_text(encoding="utf-8"))
            print("  no filed tool defects from this run; approve drafts first")
            _dump(prev, out)
            self._save_state("verify_fix", closed=0)
            return out
        specs = {p.name: JobSpec.model_validate_json((p / "job.json").read_text(encoding="utf-8"))
                 for p in (self.dir / "jobs").iterdir() if (p / "job.json").exists()}
        base = {r.job_id: r for r in self.results("results_effective.json")}
        plan = self.plan_obj()
        with obs.stage("8_fix_verification"):
            pairs, keys = [], []
            affected_tcs = set()
            for d in filed:
                cl = clusters[d["id"]]
                for j in cl.members:
                    pairs.append((specs[j].testcase, specs[j].flow))
                    keys.append(j)
                    affected_tcs.add(specs[j].testcase.name)
                if cl.reproducer:
                    rs = JobSpec.model_validate_json((Path(cl.reproducer.job_dir) / "job.json").read_text(encoding="utf-8"))
                    pairs.append((rs.testcase, rs.flow))
                    keys.append(f"repro:{d['id']}")
            # regression safety: every other regression flow of the affected testcases
            for j, s in specs.items():
                if s.testcase.name in affected_tcs and j not in keys and s.flow.id in {f.id for f in plan.regression_flows}:
                    pairs.append((s.testcase, s.flow))
                    keys.append(j)
            print(f"  re-running {len(pairs)} jobs on build=fixed")
            new = dict(zip(keys, self._batch(pairs, "fixed", build="fixed", sub="fixed")))
            for k, r in new.items():
                if k.startswith("repro:"):
                    continue
                before = base[k].status
                bucket = {("fail", "pass"): "fixed", ("fail", "fail"): "still_failing",
                          ("pass", "fail"): "new_failures", ("pass", "pass"): "still_passing"}[(before, r.status)]
                out["delta"][bucket].append(k)
            regressions = out["delta"]["new_failures"]
            for d in filed:
                cl = clusters[d["id"]]
                repro_ok = new.get(f"repro:{d['id']}") is None or new[f"repro:{d['id']}"].status == "pass"
                members_ok = all(new[j].status == "pass" for j in cl.members)
                if repro_ok and members_ok and not regressions:
                    tr.close(d["id"], f"run {self.run_id}: reproducer and {len(cl.members)} affected jobs pass "
                                      f"on build=fixed")
                out["defects"].append({"id": d["id"], "title": d["title"], "reproducer_passes": repro_ok,
                                       "affected_pass": members_ok,
                                       "closed": repro_ok and members_ok and not regressions})
            _dump(self.dir / "fixverify.json", out)
        dl = out["delta"]
        print(f"  delta: fixed {len(dl['fixed'])}, still failing {len(dl['still_failing'])}, "
              f"new failures {len(dl['new_failures'])}, still passing {len(dl['still_passing'])}; "
              f"closed {sum(x['closed'] for x in out['defects'])}/{len(filed)} defects")
        if dl["new_failures"]:
            print(f"  WARNING: {len(dl['new_failures'])} regression(s) on build=fixed; no defect closed: "
                  f"{', '.join(dl['new_failures'])}")
        self._save_state("verify_fix", closed=sum(x["closed"] for x in out["defects"]),
                         regressions=len(dl["new_failures"]))
        return out

    def stage_coverage(self) -> dict:
        plan, suite, res = self.plan_obj(), self.suite(), self.results("results_effective.json")
        with obs.stage("9_coverage"):
            cov = coverage.matrix(plan, suite, res)
            gaps = coverage.gaps(plan, cov)
            props = coverage.propose(plan, gaps, self.mode)
            _dump(self.dir / "coverage.json", {"matrix": cov, "gaps": gaps, "proposals": props})
            pdir = self.dir / "proposals"
            pdir.mkdir(exist_ok=True)
            for p in props:
                if p.get("verilog"):
                    (pdir / f"{p['top']}.v").write_text(p["verilog"], encoding="utf-8")
        print(f"  required cells verified: {cov['verified']}/{cov['required']} ({cov['pct_verified']}%), "
              f"{len(gaps)} gaps, {sum(1 for p in props if p.get('admitted'))} proposals admitted by gate")
        self._save_state("coverage")
        return cov

    def stage_eval(self) -> dict:
        clusters, res = self.clusters(), self.results()
        with obs.stage("11_evaluation"):
            out = {"live_rules": evaluate.live_eval(
                       [c.model_copy(update={"decision": c.rules_decision or c.decision}) for c in clusters], res),
                   "escapes": evaluate.escapes(res),
                   "gold_rules": evaluate.gold_eval(GOLD_PATH, triage.classify_rules) if GOLD_PATH.exists() else None,
                   "llm": "not run (no LLM provider configured)" if self.mode != "llm" else None}
            if self.mode == "llm":
                out["live_llm"] = evaluate.live_eval(clusters, res)
                if GOLD_PATH.exists():
                    out["gold_llm"] = evaluate.gold_eval(GOLD_PATH, lambda ev: llm.structured(
                        "Classify this regression failure cluster as tool_defect, test_bug, infrastructure or "
                        "known_issue.", ev.model_dump(), TriageDecision) or triage.classify_rules(ev))
            _dump(self.dir / "eval.json", out)
        lv, gd = out["live_rules"], out["gold_rules"]
        print(f"  live (job-level, rules): accuracy {lv['accuracy']} on {lv['n']} failing jobs; "
              f"gold set (rules): {gd['accuracy'] if gd else 'n/a'} on {gd['n'] if gd else 0} items; "
              f"escapes {len(out['escapes'])}")
        self._save_state("eval")
        return out

    def stage_report(self) -> Path:
        with obs.stage("12_report"):
            res = self.results()
            clusters = self.clusters()
            cats = {}
            for c in clusters:
                cats[c.decision.category] = cats.get(c.decision.category, 0) + len(c.members)
            tr = Tracker(TRACKER_PATH)
            metrics = {
                "run_id": self.run_id, "profile": self.profile, "mode": self.mode, "tool": yosys.version(),
                "jobs": len(res), "passed": sum(r.status == "pass" for r in res),
                "failed": sum(r.status == "fail" for r in res),
                "pass_rate": round(sum(r.status == "pass" for r in res) / max(1, len(res)), 3),
                "failure_kinds": {k: sum(r.kind == k for r in res) for k in sorted({r.kind for r in res if r.kind})},
                "equiv_verdicts": {k: sum(r.equiv == k for r in res) for k in sorted({r.equiv for r in res})},
                "clusters": len(clusters), "triage_jobs_by_category": cats,
                "defects": {s: len(tr.all(s)) for s in ("draft", "filed", "closed", "rejected")},
                "stage_seconds": {**self.state.get("timings", {}), **obs.timings},
                "llm": dict(obs.counters),
            }
            _dump(self.dir / "metrics.json", metrics)
            path = report.render(self, metrics, tr)
        print(f"  report: {path}")
        return path

    def run_all(self, approve_as: str | None = None) -> Path:
        self.stage_plan()
        self.stage_generate()
        self.stage_regress()
        self.stage_triage()
        self.stage_defects(approve_as)
        self.stage_verify_fix()
        self.stage_coverage()
        self.stage_eval()
        return self.stage_report()
