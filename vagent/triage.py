"""Stage 6: failure triage. Deterministic normalisation, signatures, clustering, rule-based
classification, known-defect matching and delta reduction to a minimal reproducer."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import ROOT
from .generators import GENERATORS, make_testcase
from .models import Cluster, Evidence, Flow, JobResult, JobSpec, Reproducer, TriageDecision

_NORM = [
    (re.compile(r"[A-Za-z]:[\\/][^\s'`]+|(?<!\w)/[\w./-]+"), "<path>"),
    (re.compile(r"design\.v:\d+"), "design.v:<line>"),
    (re.compile(r"\$(auto|abc|techmap|paramod|flatten)\$[^\s'`]*"), "$<id>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\(\d+->\d+\)"), ""),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]

SPECIALISED = ("DSP", "LUTRAM", "BRAM", "SRL", "CARRY", "MUXF")


def normalize(msg: str) -> str:
    out = msg
    for pat, rep in _NORM:
        out = pat.sub(rep, out)
    return out.strip()


def key_message(r: JobResult) -> str:
    """The part of a failure message that identifies the failure mode (design-specific names removed)."""
    if r.kind == "equiv_fail":
        return "equivalence counterexample (netlist not equivalent to RTL)"
    if r.kind == "qor_golden":
        # primary symptom: lost primitive inference; secondary LUT/MUXF growth is a consequence
        drift = re.findall(r"(\w+) (decreased|increased)", r.message.split("|")[0])
        primary = [d for d in drift if d[1] == "decreased"] or drift
        return "QoR drift vs golden: " + ", ".join(sorted(f"{a} {b}" for a, b in primary))
    if r.kind == "qor_expect":
        return "QoR expectation unmet: " + ", ".join(sorted(set(re.findall(r"expected (\w+>=\d+)", r.message))))
    return normalize(r.message)


def option_tokens(flow: Flow) -> set[str]:
    return {flow.base, *flow.option_groups}


def fingerprint(failed: list[Flow], passed: list[Flow]) -> tuple[list[str], bool]:
    """Options common to every failing flow and absent from every passing flow."""
    if not failed:
        return [], False
    inter = set.intersection(*(option_tokens(f) for f in failed))
    union = set().union(*(option_tokens(f) for f in passed)) if passed else set()
    baseline_failed = any(not f.option_groups for f in failed)
    return sorted(inter - union), baseline_failed


def signature(kind: str, key: str, fp: list[str]) -> str:
    return hashlib.sha1(f"{kind}|{key}|{','.join(fp)}".encode()).hexdigest()[:10]


def load_known() -> list[dict]:
    return json.loads((ROOT / "data" / "known_defects.json").read_text(encoding="utf-8"))


def match_known(message: str, known: list[dict]) -> str | None:
    for k in known:
        if k["status"] == "open" and re.search(k["match"], message):
            return k["id"]
    return None


def cluster_failures(results: list[JobResult], flows: dict[str, Flow], known: list[dict],
                     rerun_passed: set[str] | None = None) -> list[Cluster]:
    """rerun_passed: first-attempt job ids that passed on an identical rerun (None = no rerun done)."""
    ok_jobs = rerun_passed or set()
    by_tc: dict[str, list[JobResult]] = {}
    for r in results:
        by_tc.setdefault(r.testcase, []).append(r)
    groups: dict[str, list[JobResult]] = {}
    meta: dict[str, tuple] = {}
    for tc, rs in by_tc.items():
        tool_fail = [r for r in rs if r.status == "fail" and r.kind not in ("timeout", "infra")]
        passed = [flows[r.flow] for r in rs if r.status == "pass" or r.job_id in ok_jobs]
        for r in rs:
            if r.status != "fail":
                continue
            if r.kind in ("timeout", "infra"):
                fp, base = [], False
            else:
                same = [flows[x.flow] for x in tool_fail if x.kind == r.kind and key_message(x) == key_message(r)]
                fp, base = fingerprint(same, passed)
            infra = r.kind in ("timeout", "infra") and rerun_passed is not None
            sig = signature(r.kind, key_message(r) + (f"|rerun={_rr(r, ok_jobs)}" if infra else ""), fp)
            groups.setdefault(sig, []).append(r)
            prev = meta.get(sig, (fp, False))
            meta[sig] = (fp, prev[1] or base)
    clusters = []
    for i, (sig, members) in enumerate(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])), 1):
        fp, base = meta[sig]
        rep = members[0]
        tcs = sorted({m.testcase for m in members})
        passed = sorted({r.flow for tc in tcs for r in by_tc[tc] if r.status == "pass" or r.job_id in ok_jobs})
        fams = sorted({f for m in members for f in m.families if f in SPECIALISED})
        gm = [m.golden_match for m in members if m.golden_match is not None]
        ev = Evidence(signature=sig, kind=rep.kind, message=key_message(rep), excerpt=rep.excerpt[-1500:],
                      testcases=tcs, flows_failed=sorted({m.flow for m in members}), flows_passed=passed,
                      fingerprint=fp, baseline_failed=base, golden_match=(all(gm) if gm else None), families=fams,
                      known_match=match_known(rep.message, known),
                      error_in_testcase=bool(re.search(r"design\.v:\d+", rep.message)) or rep.kind == "elab_error")
        if rep.kind in ("timeout", "infra") and rerun_passed is not None:
            ev.rerun = _rr(rep, ok_jobs)
        clusters.append(Cluster(id=f"C{i:02d}", evidence=ev, members=[m.job_id for m in members],
                                decision=classify_rules(ev)))
    return clusters


def _rr(r: JobResult, ok_jobs: set[str]) -> str:
    return "passed" if r.job_id in ok_jobs else "failed"


def component_for(ev: Evidence) -> str:
    if ev.kind == "parse_error":
        return "testsuite: Verilog source"
    if ev.kind == "elab_error":
        return "testsuite: testcase metadata"
    if ev.kind in ("timeout", "infra"):
        return "regression infrastructure"
    m = re.search(r"in pass (\w+)", ev.message)
    if ev.kind == "crash":
        return f"synthesis core: {m.group(1) if m else 'unknown pass'}"
    if ev.kind == "qor_golden":
        fam = re.search(r"(\w+) (decreased|increased)", ev.message)
        return f"synth_xilinx: {fam.group(1) if fam else 'resource'} mapping"
    names = {"DSP": "DSP48 mapping", "LUTRAM": "distributed RAM mapping", "BRAM": "block RAM mapping",
             "SRL": "SRL mapping", "CARRY": "carry-chain mapping", "MUXF": "wide mux mapping"}
    for fam in SPECIALISED:
        if fam in ev.families:
            return f"synth_xilinx: {names[fam]}"
    if "-dff" in ev.fingerprint:
        return "synth_xilinx: abc9 -dff (LUT/FF mapping)"
    return "synthesis: logic optimisation/mapping"


def classify_rules(ev: Evidence) -> TriageDecision:
    comp = component_for(ev)
    opt = [o for o in ev.fingerprint if o.startswith("-")]

    def d(cat, conf, why, review=False, component=None):
        return TriageDecision(category=cat, confidence=conf, component=component or comp, rationale=why,
                              needs_human_review=review)

    if ev.known_match:
        return d("known_issue", 0.95, f"Message matches open known defect {ev.known_match}.")
    if ev.kind in ("timeout", "infra"):
        if ev.rerun == "passed":
            return d("infrastructure", 0.9, "Failed on first attempt, passed on identical rerun: flaky "
                                             "infrastructure (hang/disk), not a tool or testcase problem.")
        if ev.kind == "infra":
            return d("infrastructure", 0.7, "Worker-level error (no tool verdict produced).", True)
        return d("tool_defect", 0.5, "Timeout reproduces on rerun: possible tool hang or performance bug.", True)
    if ev.kind in ("parse_error", "elab_error") and ev.error_in_testcase:
        return d("test_bug", 0.9, "Front-end rejects the testcase itself (syntax or wrong top/metadata) on every "
                                  "flow; the failure is independent of synthesis options.")
    if ev.kind == "qor_expect" and ev.golden_match:
        return d("test_bug", 0.85, "Resource expectation fails but results match the golden reference build "
                                   "exactly: the expectation in the testcase is stale or wrong.",
                 component="testsuite: resource expectation")
    if opt and not ev.baseline_failed:
        return d("tool_defect", 0.9, f"Fails only when {', '.join(opt)} is used; baseline flow passes on the "
                                     f"same design. Option-triggered tool behaviour ({ev.kind}).")
    if ev.kind == "crash":
        return d("tool_defect", 0.75, "Tool internal error/assertion on a design the front-end accepted.")
    if ev.kind == "qor_golden":
        return d("tool_defect", 0.7, "Resource usage changed versus the golden reference build.")
    if ev.kind == "qor_expect" and ev.golden_match is False:
        return d("tool_defect", 0.6, "Expectation unmet and results differ from golden.", True)
    if ev.kind == "equiv_fail":
        return d("tool_defect", 0.6, "Netlist not equivalent on the baseline flow; could also be an X/initial "
                                     "state issue in the testcase.", True)
    return d("tool_defect", 0.3, "No rule matched confidently.", True)


# ---------------------------------------------------------------- delta reduction
MIN_PARAMS = {"param_hier": {"N": 2}, "shreg": {"DEPTH": 4}}


def reduce_cluster(cl: Cluster, results: dict[str, JobResult], specs: dict[str, JobSpec],
                   run_batch, workdir: Path) -> Reproducer | None:
    """Greedy delta reduction: drop option groups, then shrink generator parameters, while the
    failure (same kind) still reproduces. run_batch(list[(tc, flow)], tag) -> list[JobResult]."""
    members = [specs[j] for j in cl.members if j in specs]
    if not members:
        return None
    rep = sorted(members, key=lambda s: (-len(s.flow.option_groups), len(s.testcase.source)))[0]
    tc, flow, kind = rep.testcase, rep.flow, cl.evidence.kind
    trials = 0
    def reproduces(r):
        return r.status == "fail" and r.kind == kind and key_message(r) == cl.evidence.message

    def ckey(t, f, g):
        return (t.name, f.base, f.family, tuple(sorted(g)))

    known = {ckey(specs[j].testcase, specs[j].flow, specs[j].flow.option_groups): reproduces(r)
             for j, r in results.items() if j in specs and specs[j].testcase.params == tc.params}

    def still_fails(t, groups_list):
        nonlocal trials
        todo = [(t, flow.with_options(g, f"r{trials}_{i}")) for i, g in enumerate(groups_list)
                if ckey(t, flow, g) not in known]
        if todo:
            trials += len(todo)
            for (tt, ff), r in zip(todo, run_batch(todo, f"red{trials}")):
                known[ckey(tt, ff, ff.option_groups)] = reproduces(r)
        return [known[ckey(t, flow, g)] for g in groups_list]

    groups = list(flow.option_groups)
    while groups:
        cands = [[g for g in groups if g != drop] for drop in groups]
        ok = still_fails(tc, cands)
        nxt = next((c for c, f in zip(cands, ok) if f), None)
        if nxt is None:
            break
        groups = nxt
    removed = [g for g in flow.option_groups if g not in groups]
    final_r = None

    if tc.generator in GENERATORS and tc.params:
        mins = MIN_PARAMS.get(tc.generator, {})
        half = {k: max(mins.get(k, 2), v // 2) for k, v in tc.params.items()}
        low = {k: mins.get(k, 2) for k in tc.params}
        cands = [make_testcase(tc.generator, tc.features, tc.expect, p, name=f"{tc.name}_min{i}")
                 for i, p in enumerate([low, half]) if p != tc.params]
        if cands:
            fflow = flow.with_options(groups, "min")
            trials += len(cands)
            res = run_batch([(c, fflow) for c in cands], f"redp{trials}")
            for c, r in zip(cands, res):
                if reproduces(r):
                    tc, final_r = c, r
                    break
    final_flow = flow.with_options(groups, "min")
    if final_r is None:  # confirm the reproducer in its own directory
        final_r = run_batch([(tc, final_flow)], "repro")[0]
        trials += 1
    r = final_r
    if not reproduces(r):
        return None
    return Reproducer(testcase=tc.name, params=tc.params, flow_command=final_flow.command, removed_options=removed,
                      design_lines=len(tc.source.splitlines()), job_dir=r.job_dir, trials=trials)
