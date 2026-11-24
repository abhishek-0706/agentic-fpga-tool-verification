"""Labelled triage examples for LoRA fine-tuning, generated with the project's own fault injection.

  run    run the job bank (testcase x flow, build=buggy, plus emulated infrastructure faults) into finetune/_gen
  build  compose many small regressions from the bank, cluster them with vagent.triage, label each cluster from
         injected.json ground truth, drop gold-set duplicates, balance, split and write finetune/data/
  probe  run a few named jobs (template check)
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vagent import evaluate, generators, llm, runner, spec, toolshim, triage, yosys  # noqa: E402
from vagent.models import CATEGORIES, Evidence, Flow, JobResult, JobSpec, TestCase, TriageDecision  # noqa: E402

GEN = HERE / "_gen"
DATA = HERE / "data"
GOLD = ROOT / "eval" / "triage_gold.jsonl"
SEED = 20261124
MAX_WORKERS = 4
JOB_TIMEOUT = 600
HANG_TIMEOUT = 150  # the PoC's default per-job timeout (demo --timeout); the message records it
N_ENOSPC, N_HANG = 36, 18
N_COMPOSITIONS = 6000
KEY_CAP, CLASS_CAP, CLASS_MIN = 14, 130, 60
AUG_FRACTION = 0.35
VAL_FRACTION = 0.15

# the two task strings vagent/agent.py passes to llm.structured with the same Evidence payload
TASK_TRIAGE = ("Classify this regression failure cluster as tool_defect, test_bug, infrastructure or known_issue, "
               "name the tool component, and justify from the evidence only.")
TASK_EVAL = "Classify this regression failure cluster as tool_defect, test_bug, infrastructure or known_issue."
REFERENCE_RUNS = {"openai/gpt-oss-120b (Groq) run 20261124-012322-full": "20261124-012322-full",
                  "openai/gpt-oss-120b (Groq) run 20261124-004038-full": "20261124-004038-full"}


# ------------------------------------------------------------------ extra designs (new testcases, same fault model)
def seqdet1011() -> str:
    return """module seqdet1011 (
  input      clk, rst, din,
  output reg hit
);
  localparam IDLE = 2'd0, GOT1 = 2'd1, GOT10 = 2'd2, GOT101 = 2'd3;
  reg [1:0] st;
  always @(posedge clk) begin
    if (rst) begin
      st <= IDLE; hit <= 1'b0;
    end else begin
      hit <= 1'b0;
      case (st)
        IDLE:   st <= din ? GOT1 : IDLE;
        GOT1:   st <= din ? GOT1 : GOT10;
        GOT10:  st <= din ? GOT101 : IDLE;
        GOT101: begin st <= din ? GOT1 : GOT10; hit <= din; end
      endcase
    end
  end
endmodule
"""


def hs_ctrl() -> str:
    return """module hs_ctrl (
  input      clk, rst, req, done,
  output reg busy, ack
);
  localparam IDLE = 2'd0, RUN = 2'd1, ACK = 2'd2;
  reg [1:0] st;
  always @(posedge clk) begin
    if (rst) begin
      st <= IDLE; busy <= 1'b0; ack <= 1'b0;
    end else begin
      ack <= 1'b0;
      case (st)
        IDLE: if (req) begin st <= RUN; busy <= 1'b1; end
        RUN:  if (done) begin st <= ACK; busy <= 1'b0; end
        ACK:  begin st <= IDLE; ack <= 1'b1; end
        default: st <= IDLE;
      endcase
    end
  end
endmodule
"""


def rf_2r(AW=3, DW=4) -> str:
    return f"""module rf_2r #(parameter AW = {AW}, parameter DW = {DW}) (
  input           clk, we,
  input  [AW-1:0] wa, ra0, ra1,
  input  [DW-1:0] d,
  output [DW-1:0] q0, q1
);
  reg [DW-1:0] mem [0:(1<<AW)-1];
  always @(posedge clk) if (we) mem[wa] <= d;
  assign q0 = mem[ra0];
  assign q1 = mem[ra1];
endmodule
"""


def muladd(W=5) -> str:
    return f"""module muladd #(parameter W = {W}) (
  input                clk,
  input  [W-1:0]       a, b,
  input  [2*W-1:0]     c,
  output reg [2*W:0]   y
);
  always @(posedge clk) y <= a * b + c;
endmodule
"""


WRAPPERS = {  # testcase name: (inner generator, top module, wrapper source with a #(.P()) override)
    "tc_cnt_top": ("counter", "cnt_top", """module cnt_top (
  input        clk, rst, en, up,
  output [5:0] q,
  output       tc
);
  counter #(.W(6)) u_cnt (.clk(clk), .rst(rst), .en(en), .up(up), .q(q), .tc(tc));
endmodule
"""),
    "tc_mux_top": ("comb_mux", "mux_top", """module mux_top (
  input  [3:0] a, b, c, d,
  input  [1:0] sel,
  input  [3:0] req,
  output [3:0] y,
  output [1:0] grant,
  output       any,
  output       parity
);
  comb_mux #(.W(4)) u_mux (.a(a), .b(b), .c(c), .d(d), .sel(sel), .req(req), .y(y), .grant(grant),
                           .any(any), .parity(parity));
endmodule
"""),
    "tc_srl_top": ("shreg", "srl_top", """module srl_top (
  input        clk, en,
  input  [0:0] d,
  output [0:0] q
);
  shreg #(.W(1), .DEPTH(8)) u_srl (.clk(clk), .en(en), .d(d), .q(q));
endmodule
"""),
    "tc_ram_top": ("lutram", "ram_top", """module ram_top (
  input        clk, we,
  input  [2:0] wa, ra,
  input  [3:0] d,
  output [3:0] q
);
  lutram #(.AW(3), .DW(4)) u_ram (.clk(clk), .we(we), .wa(wa), .ra(ra), .d(d), .q(q));
endmodule
"""),
}
WRAP_DEPTH = {"tc_cnt_top": ("rst", 6), "tc_mux_top": (None, 1), "tc_srl_top": (None, 11), "tc_ram_top": (None, 4)}


def _mutate(src: str, old: str, new: str) -> str:
    assert old in src, f"mutation anchor not found: {old!r}"
    return src.replace(old, new, 1)


def build_bank(plan) -> list[dict]:
    """Bank entries: testcase, base (testcase identity used by the leakage check), golden mode, role."""
    std = {t.generator: t for t in generators.generated_suite(plan)}
    out: list[dict] = []

    def add(tc: TestCase, base: str, golden: str, role: str) -> None:
        out.append({"tc": tc, "base": base, "golden": golden, "role": role})

    for g, t in std.items():
        add(t, t.name, "project", "default")
    for t in generators.legacy_suite():
        add(t, t.name, "project", "legacy")

    def var(gen, params, name, golden):
        t = generators.make_testcase(gen, std[gen].features, std[gen].expect, params, name=name)
        add(t, f"tc_{gen}", golden, "variant")

    var("comb_mux", {"W": 4}, "tc_mux_w4", "computed")
    var("addsub", {"W": 8}, "tc_addsub_w8", "none")
    var("addsub", {"W": 24}, "tc_addsub_w24", "computed")
    var("mul_reg", {"W": 5}, "tc_mul_w5", "computed")
    var("macc", {"W": 5, "AW": 12}, "tc_macc_w5", "none")
    var("counter", {"W": 12}, "tc_counter_w12", "computed")
    var("shreg", {"W": 1, "DEPTH": 8}, "tc_shreg_w1d8", "computed")
    var("lutram", {"AW": 3, "DW": 4}, "tc_lutram_8x4", "none")
    var("param_hier", {"N": 2, "W": 4}, "tc_param_hier_n2", "computed")
    var("bram", {"AW": 5, "DW": 8}, "tc_bram_32x8", "computed")

    def new(name, gen, src, top, feats, seq, rst, depth, expect, golden):
        add(TestCase(name=name, generator=gen, params={}, top=top, features=feats, sequential=seq, reset=rst,
                     depth=depth, expect=expect, source=src), name, golden, "new_design")

    new("tc_seqdet_1011", "fsm_custom", seqdet1011(), "seqdet1011", ["FEAT-FSM", "FEAT-SEQ"], True, "rst", 8, {},
        "computed")
    new("tc_hs_ctrl", "fsm_custom", hs_ctrl(), "hs_ctrl", ["FEAT-FSM", "FEAT-SEQ"], True, "rst", 8, {}, "none")
    new("tc_rf_2r", "lutram_custom", rf_2r(), "rf_2r", ["FEAT-LUTRAM"], True, None, 4, {"xc7": ["LUTRAM>=1"]},
        "computed")
    new("tc_muladd", "dsp_custom", muladd(), "muladd", ["FEAT-DSP"], True, None, 2, {"xc7": ["DSP>=1"]}, "none")
    for name, (inner, top, wrapper) in WRAPPERS.items():
        rst, depth = WRAP_DEPTH[name]
        it = std[inner]
        new(name, "wrapper", it.source + "\n" + wrapper, top, it.features + ["FEAT-HIER", "FEAT-PARAM"],
            it.sequential, rst, depth, it.expect, "none" if name == "tc_srl_top" else "computed")

    def mutant(base_tc: TestCase, name: str, golden: str, *, source=None, top=None, expect=None):
        t = base_tc.model_copy(update={"name": name, "source": source or base_tc.source, "top": top or base_tc.top,
                                       "expect": base_tc.expect if expect is None else expect,
                                       "ground_truth": "test_bug"})
        add(t, f"tc_{base_tc.generator}", golden, "test_bug_mutant")

    s = std
    mutant(s["comb_mux"], "tc_mux_prio", "none", source=_mutate(s["comb_mux"].source, "assign any = |req;",
                                                                   "assign any = |req"))
    mutant(s["addsub"], "tc_addsub_c", "none", source=_mutate(s["addsub"].source, "endmodule", "endmodul"))
    mutant(s["counter"], "tc_counter_ud", "none", source=_mutate(s["counter"].source, "output reg [W-1:0] q,",
                                                                   "output reg [W-1:0] q"))
    mutant(s["fsm_seqdet"], "tc_seqdet_b", "none", source=_mutate(s["fsm_seqdet"].source, "reg [1:0] st;",
                                                                    "reg [1:0 st;"))
    mutant(s["shreg"], "tc_shreg_en", "none", source=_mutate(s["shreg"].source, "always @(posedge clk)",
                                                               "always @(posedge clk"))
    mutant(s["lutram"], "tc_lutram_16x8", "none", top="lutram_wrap")
    mutant(s["mul_reg"], "tc_mul_reg6", "none", top="mul_reg_top")
    mutant(s["bram"], "tc_bram_64x16", "none", top="bram_sp")
    mutant(s["comb_mux"], "tc_mux_sel", "like:tc_comb_mux", expect={"xc7": ["LUT>=1", "DSP>=1"]})
    w4 = next(e["tc"] for e in out if e["tc"].name == "tc_mux_w4")
    mutant(w4, "tc_mux_w4_gen", "like:tc_mux_w4", expect={"generic": ["LUT>=1"], "xc7": ["LUT>=1"]})
    d8 = next(e["tc"] for e in out if e["tc"].name == "tc_shreg_w1d8")
    mutant(d8, "tc_srl_d8", "none", expect={"xc7": ["SRL>=1", "LUTRAM>=1"]})
    mutant(s["bram"], "tc_bram_rf", "like:tc_bram", expect={"xc7": ["BRAM>=2"]})
    b58 = next(e["tc"] for e in out if e["tc"].name == "tc_bram_32x8")
    mutant(b58, "tc_bram_32x8_c", "none", expect={"xc7": ["BRAM>=1", "CARRY>=1"]})
    names = [e["tc"].name for e in out]
    assert len(names) == len(set(names)), "duplicate testcase names in bank"
    return out


def bank_flows(plan) -> list[Flow]:
    fl = [f.model_copy(update={"in_regression": True}) for f in plan.flows if f.id != "xc7_widemux"]
    return fl[:1] + [Flow(id="generic_flatten", command="synth -flatten")] + fl[1:]


# ------------------------------------------------------------------ golden (as `vagent bless`: build=fixed families)
def _synth_families(args: tuple) -> tuple[str, dict | None]:
    key, source, top, command, workdir = args
    d = Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "design.v").write_text(source, encoding="utf-8")
    rc, _ = toolshim.run_tool(source, top, Flow(id="golden", command=command), "fixed", d)
    if rc != 0:
        return key, None
    return key, yosys.families(yosys.count_cells(yosys.load_netlist(d / "netlist.json"), top))


def compute_golden(bank: list[dict], flows: list[Flow], workers: int) -> tuple[dict, dict]:
    path, check_path = GEN / "golden.json", GEN / "golden_check.json"
    project = runner.load_golden()
    by_name = {e["tc"].name: e for e in bank}
    todo: dict[str, tuple] = {}

    def want(tc: TestCase, f: Flow) -> str:
        h = hashlib.sha1(f"{tc.source}|{tc.top}|{f.command}".encode()).hexdigest()[:16]
        todo.setdefault(h, (h, tc.source, tc.top, f.command, str(GEN / "golden_jobs" / h)))
        return h

    plan_keys: dict[str, str] = {}
    for e in bank:
        tc, mode = e["tc"], e["golden"]
        src = by_name[mode.split(":", 1)[1]] if mode.startswith("like:") else e
        smode = src["golden"]
        for f in flows:
            gk = runner.golden_key(tc.name, f.command)
            if smode == "none":
                continue
            pk = runner.golden_key(src["tc"].name, f.command)
            if smode == "project" and pk in project:
                plan_keys[gk] = f"project:{pk}"
            else:
                plan_keys[gk] = want(src["tc"], f)
    checks = {}
    for name in ("tc_addsub", "tc_counter", "tc_comb_mux"):
        for f in flows:
            pk = runner.golden_key(name, f.command)
            if pk in project:
                checks[pk] = want(by_name[name]["tc"], f)
    done = json.loads((GEN / "golden_cache.json").read_text(encoding="utf-8")) if (GEN / "golden_cache.json").exists() else {}
    pending = [a for h, a in todo.items() if h not in done]
    print(f"golden: {len(todo)} synthesis-only runs on build=fixed ({len(pending)} pending)", flush=True)
    t0 = time.perf_counter()
    if pending:
        yosys.warm()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, (h, fams) in enumerate(ex.map(_synth_families, pending), 1):
                done[h] = fams
                if i % 10 == 0 or i == len(pending):
                    print(f"  golden {i}/{len(pending)} ({time.perf_counter() - t0:.0f}s)", flush=True)
                    (GEN / "golden_cache.json").write_text(json.dumps(done), encoding="utf-8")
    golden = {}
    for gk, src in plan_keys.items():
        fams = project[src[8:]] if src.startswith("project:") else done.get(src)
        if fams is not None:
            golden[gk] = fams
    check = {pk: {"project": project[pk], "recomputed": done.get(h), "match": project[pk] == done.get(h)}
             for pk, h in checks.items()}
    path.write_text(json.dumps(golden, indent=1, sort_keys=True), encoding="utf-8")
    check_path.write_text(json.dumps(check, indent=1), encoding="utf-8")
    print(f"golden: {len(golden)} entries; consistency with golden/qor_golden.json: "
          f"{sum(c['match'] for c in check.values())}/{len(check)} match", flush=True)
    return golden, check


# ------------------------------------------------------------------ job bank
def _same_spec(d: Path, want: JobSpec) -> bool:
    if not ((d / "job.json").exists() and (d / "result.json").exists()):
        return False
    have = JobSpec.model_validate_json((d / "job.json").read_text(encoding="utf-8"))
    return have.model_dump() == want.model_dump()


def _plan_job(jobs_dir: Path, tc: TestCase, flow: Flow, golden: dict, fault: str | None) -> tuple[JobSpec, bool]:
    jid = runner.job_key(tc.name, flow.id) + (f"__{fault}" if fault else "")
    want = JobSpec(job_id=jid, testcase=tc, flow=flow, build="buggy", attempt=1,
                   golden=golden.get(runner.golden_key(tc.name, flow.command)), infra_fault=fault)
    return want, _same_spec(jobs_dir / jid, want)


def _make(jobs_dir: Path, want: JobSpec) -> JobSpec:
    fault = want.infra_fault
    spec_ = runner.make_job(jobs_dir, want.testcase, want.flow, "buggy", tag=fault or "",
                            golden={runner.golden_key(want.testcase.name, want.flow.command): want.golden}
                            if want.golden is not None else None, infra=False)
    if fault:
        spec_ = spec_.model_copy(update={"infra_fault": fault})
        (jobs_dir / spec_.job_id / "job.json").write_text(spec_.model_dump_json(indent=1), encoding="utf-8")
    assert spec_.model_dump() == want.model_dump(), spec_.job_id
    return spec_


def cmd_run(workers: int) -> None:
    workers = min(workers, MAX_WORKERS)
    GEN.mkdir(exist_ok=True)
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    bank, flows = build_bank(plan), bank_flows(plan)
    t_start = time.perf_counter()
    golden, _ = compute_golden(bank, flows, workers)
    t_golden = time.perf_counter() - t_start
    jobs_dir = GEN / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    index = {}
    todo = []
    for e in bank:
        for f in flows:
            want, done = _plan_job(jobs_dir, e["tc"], f, golden, None)
            index[want.job_id] = {"tc": e["tc"].name, "flow": f.id, "fault": None}
            if not done:
                todo.append(want)
    print(f"bank: {len(bank)} testcases x {len(flows)} flows = {len(index)} jobs ({len(todo)} to run), "
          f"{workers} workers, timeout {JOB_TIMEOUT}s", flush=True)
    t0 = time.perf_counter()
    if todo:
        runner.run_jobs([_make(jobs_dir, w) for w in todo], jobs_dir, workers, JOB_TIMEOUT)
    t_bank = time.perf_counter() - t0
    clean = {(v["tc"], v["flow"]): JobResult.model_validate_json((jobs_dir / k / "result.json").read_text(encoding="utf-8"))
             for k, v in index.items()}
    rng = random.Random(SEED)
    passing = sorted(k for k, r in clean.items() if r.status == "pass")
    rng.shuffle(passing)
    picks = {"enospc": passing[:N_ENOSPC], "hang": passing[N_ENOSPC:N_ENOSPC + N_HANG]}
    by_name = {e["tc"].name: e["tc"] for e in bank}
    fmap = {f.id: f for f in flows}
    t0 = time.perf_counter()
    for fault, pairs in picks.items():
        todo = []
        for tcn, fid in pairs:
            want, done = _plan_job(jobs_dir, by_name[tcn], fmap[fid], golden, fault)
            index[want.job_id] = {"tc": tcn, "flow": fid, "fault": fault}
            if not done:
                todo.append(want)
        print(f"infra faults: {len(pairs)} x {fault} ({len(todo)} to run)", flush=True)
        if todo:
            runner.run_jobs([_make(jobs_dir, w) for w in todo], jobs_dir, workers,
                            HANG_TIMEOUT if fault == "hang" else JOB_TIMEOUT)
    t_faults = time.perf_counter() - t0
    (GEN / "bank_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    (GEN / "bank_meta.json").write_text(json.dumps({
        "testcases": [{"name": e["tc"].name, "base": e["base"], "golden": e["golden"], "role": e["role"],
                       "generator": e["tc"].generator, "params": e["tc"].params, "top": e["tc"].top,
                       "ground_truth": e["tc"].ground_truth} for e in bank],
        "flows": [f.model_dump() for f in flows], "workers": workers, "job_timeout_s": JOB_TIMEOUT,
        "hang_timeout_s": HANG_TIMEOUT,
        "wall_s_this_invocation": {"golden": round(t_golden, 1), "bank": round(t_bank, 1),
                                   "infra_faults": round(t_faults, 1)}}, indent=1), encoding="utf-8")
    print(f"done: golden {t_golden:.0f}s, bank {t_bank:.0f}s, faults {t_faults:.0f}s", flush=True)


def cmd_probe(names: list[str], flow_ids: list[str], workers: int) -> None:
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    bank = {e["tc"].name: e["tc"] for e in build_bank(plan)}
    flows = {f.id: f for f in bank_flows(plan)}
    d = GEN / "probe"
    d.mkdir(parents=True, exist_ok=True)
    jobs = [runner.make_job(d, bank[n], flows[f], "buggy", infra=False) for n in names for f in flow_ids]
    for r in runner.run_jobs(jobs, d, min(workers, MAX_WORKERS), JOB_TIMEOUT):
        inj = Path(r.job_dir) / "injected.json"
        print(r.job_id, r.status, r.kind, r.equiv, r.families, r.message[:90],
              inj.read_text(encoding="utf-8") if inj.exists() else "-")


# ------------------------------------------------------------------ dataset construction
def base_map() -> dict[str, str]:
    meta = json.loads((GEN / "bank_meta.json").read_text(encoding="utf-8"))
    return {t["name"]: t["base"] for t in meta["testcases"]}


def _msg(m: str) -> str:
    return re.sub(r"\d+", "<n>", m)


def near_key(ev: dict, bases: dict[str, str]) -> str:
    """Same failure mode (kind + key message with every number masked) on the same testcases, where parameter
    variants and mutants of one generator count as the same testcase; flows, signature and log excerpt are ignored."""
    return json.dumps([ev["kind"], _msg(ev["message"]), sorted({bases.get(t, t) for t in ev["testcases"]})])


def structural_key(ev: dict) -> str:
    return json.dumps([ev["kind"], ev["message"], ev["fingerprint"], ev["baseline_failed"], ev["golden_match"],
                       ev["families"], ev["rerun"], ev["known_match"], ev["error_in_testcase"]])


def pattern_key(ev: dict) -> str:
    """Decision-relevant shape only: kind, whether a synthesis option is the trigger, baseline, golden, rerun,
    known-defect match and error location; message text, options, families and testcases are ignored."""
    return json.dumps([ev["kind"], any(o.startswith("-") for o in ev["fingerprint"]), ev["baseline_failed"],
                       ev["golden_match"], ev["rerun"], ev["known_match"] is not None, ev["error_in_testcase"]])


def poc_prompt(task: str, evidence: dict) -> str:
    """The exact prompt llm.structured sends, captured through its injectable call hook."""
    seen: list[str] = []

    def capture(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps({"category": "tool_defect", "confidence": 0.5, "component": "x", "rationale": "x"})

    llm.structured(task, evidence, TriageDecision, call=capture)
    return seen[0]


EXPLAINS = {"infrastructure": {"infra", "timeout"}, "known_issue": {"crash"},
            "tool_defect": {"equiv_fail", "qor_golden", "qor_expect"},
            "test_bug": {"parse_error", "elab_error", "qor_expect"}}


@functools.cache
def _injected(job_dir: str) -> list[dict]:
    p = Path(job_dir) / "injected.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


@functools.cache
def _job_label(job_dir: str) -> str | None:
    return evaluate.job_label(Path(job_dir))


def cluster_label(members: list[JobResult], kind: str) -> tuple[str | None, str]:
    """Ground-truth label of a cluster from its members' injected.json, or (None, reason) when it is unusable."""
    labels = []
    for m in members:
        entries = _injected(m.job_dir)
        classes = {x["label"] for x in entries}
        if not classes:
            return None, "unlabelled failure (no injected fault)"
        if len(classes) > 1:
            return None, "job carries ground truth from more than one class"
        if kind in ("infra", "timeout") and not any(x["detail"] in ("hang", "enospc") for x in entries):
            return None, "timeout/infra failure not caused by an injected fault"
        labels.append(_job_label(m.job_dir))
    if len(set(labels)) > 1:
        return None, "cluster mixes ground-truth classes"
    lab = labels[0]
    if kind not in EXPLAINS[lab]:
        return None, f"label {lab} does not explain failure kind {kind}"
    return lab, "ok"


def compose(rng: random.Random, names: list[str], flows: list[Flow], clean: dict, faulted: dict,
            known: list[dict]) -> list[tuple]:
    k = rng.randint(2, 7)
    tcs = rng.sample(names, k)
    if rng.random() < 0.25:
        fl = [f for f in flows if f.id in ("generic", "xc7", "xc7_flatten", "xc7_dff", "xc7_flat_noio")]
    else:
        fl = rng.sample(flows, rng.randint(3, len(flows)))
    results = {(t, f.id): clean[(t, f.id)] for t in tcs for f in fl}
    rerun_passed: set[str] = set()
    cands = sorted(p for p in results if results[p].status == "pass" and any((*p, x) in faulted for x in ("enospc", "hang")))
    if cands and rng.random() < 0.5:
        for p in rng.sample(cands, min(len(cands), 1 if rng.random() < 0.7 else 2)):
            fault = rng.choice([x for x in ("enospc", "hang") if (*p, x) in faulted])
            results[p] = faulted[(*p, fault)]
            rerun_passed.add(results[p].job_id)  # the rerun is the clean job of the same pair, which passed
    res = list(results.values())
    clusters = triage.cluster_failures(res, {f.id: f for f in fl}, known, rerun_passed)
    by_id = {r.job_id: r for r in res}
    return [(cl, [by_id[j] for j in cl.members]) for cl in clusters]


KIND_TEXT = {"parse_error": "The front end cannot parse the design", "elab_error": "Elaboration fails",
             "crash": "The tool stops with an internal error",
             "equiv_fail": "The synthesised netlist is not equivalent to the RTL",
             "qor_golden": "Primitive usage differs from the golden reference build",
             "qor_expect": "A resource expectation is not met", "timeout": "The job was killed at the timeout",
             "infra": "The worker died without producing a tool verdict"}
CONCLUSION = {"tool_defect": "This points to a defect in the synthesis tool.",
              "test_bug": "This points to a problem in the testcase, not the tool.",
              "infrastructure": "This points to the regression infrastructure, not the tool or the testcase.",
              "known_issue": "This is the already tracked open known defect."}


def target_component(ev: Evidence, label: str, known: list[dict]) -> str:
    if label == "infrastructure":
        return "regression infrastructure"
    if label == "known_issue":
        k = next((k for k in known if k["id"] == ev.known_match), None)
        return k["component"] if k else triage.component_for(ev)
    if label == "test_bug":
        return {"parse_error": "testsuite: Verilog source", "elab_error": "testsuite: testcase metadata",
                "qor_expect": "testsuite: resource expectation"}.get(ev.kind, "testsuite")
    c = triage.component_for(ev)
    return "synthesis tool" if c.startswith(("testsuite", "regression")) else c


def target_rationale(ev: Evidence, label: str) -> str:
    opts = [o for o in ev.fingerprint if o.startswith("-")]
    parts = [f"{KIND_TEXT[ev.kind]} on {', '.join(ev.flows_failed)}"
             + (f" while {', '.join(ev.flows_passed)} pass" if ev.flows_passed else " and no flow passes")]
    if opts and not ev.baseline_failed:
        parts.append(f"only flows with {' '.join(opts)} fail")
    if ev.known_match:
        parts.append(f"the message matches open known defect {ev.known_match}")
    if ev.rerun != "not_run":
        parts.append(f"an identical rerun {ev.rerun}")
    if ev.kind in ("qor_expect", "qor_golden") and ev.golden_match is not None:
        parts.append("results match the golden reference" if ev.golden_match else "results differ from the golden reference")
    if ev.kind in ("parse_error", "elab_error") and ev.error_in_testcase:
        parts.append("the error points at the testcase")
    return "; ".join(parts) + ". " + CONCLUSION[label]


NEUTRAL = ["alu", "dp", "ctrl", "core", "blk", "unit", "ip", "regr", "path", "seq", "arb", "fifo", "pipe", "sys",
           "top", "mod", "dut", "cell", "macro", "lane"]


def augment(ev: dict, rng: random.Random, forbidden: set[str]) -> tuple[dict, dict]:
    """Rename testcases consistently and shift design.v line numbers in the log excerpt."""
    ev = json.loads(json.dumps(ev))
    ren = {}
    for t in ev["testcases"]:
        while True:
            n = f"tc_{rng.choice(NEUTRAL)}_{rng.randint(1, 99):02d}"
            if n not in forbidden and n not in ren.values():
                break
        ren[t] = n
    ev["testcases"] = sorted(ren[t] for t in ev["testcases"])
    shift = rng.randint(-5, 40)
    ev["excerpt"] = re.sub(r"design\.v:(\d+)", lambda m: f"design.v:{max(1, int(m.group(1)) + shift)}", ev["excerpt"])
    return ev, {"renamed": ren, "line_shift": shift}


def cmd_build() -> None:
    t0 = time.perf_counter()
    rng = random.Random(SEED)
    index = json.loads((GEN / "bank_index.json").read_text(encoding="utf-8"))
    meta = json.loads((GEN / "bank_meta.json").read_text(encoding="utf-8"))
    bases = base_map()
    flows = [Flow.model_validate(f) for f in meta["flows"]]
    known = triage.load_known()
    clean, faulted = {}, {}
    for jid, v in index.items():
        r = JobResult.model_validate_json((GEN / "jobs" / jid / "result.json").read_text(encoding="utf-8"))
        r = r.model_copy(update={"job_dir": str(GEN / "jobs" / jid)})
        if v["fault"]:
            faulted[(v["tc"], v["flow"], v["fault"])] = r
        else:
            clean[(v["tc"], v["flow"])] = r
    names = sorted({t for t, _ in clean})
    bank_stats = {"jobs_clean": len(clean), "jobs_faulted": len(faulted),
                  "clean_status": dict(Counter(r.status for r in clean.values())),
                  "clean_failure_kinds": dict(Counter(r.kind for r in clean.values() if r.kind)),
                  "faulted_kinds": dict(Counter(r.kind for r in faulted.values())),
                  "job_seconds_clean": round(sum(r.duration_s for r in clean.values()), 1),
                  "escapes_clean": len(evaluate.escapes(list(clean.values())))}

    drops: Counter = Counter()
    seen: dict[str, dict] = {}
    n_clusters = 0
    for i in range(N_COMPOSITIONS):
        for cl, members in compose(rng, names, flows, clean, faulted, known):
            n_clusters += 1
            lab, why = cluster_label(members, cl.evidence.kind)
            if lab is None:
                drops[why] += 1
                continue
            ev = cl.evidence.model_dump()
            k = json.dumps(ev, sort_keys=True)
            if k not in seen:
                seen[k] = {"evidence": ev, "label": lab, "rules": triage.classify_rules(cl.evidence).category,
                           "composition": i}
    pool = list(seen.values())

    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    gold_ev = {g["id"]: Evidence.model_validate(g["evidence"]).model_dump() for g in gold}
    gold_exact = {json.dumps(e, sort_keys=True) for e in gold_ev.values()}
    gold_near: dict[str, str] = {}
    for gid, e in gold_ev.items():
        k = near_key(e, bases)
        gold_near[k] = f"{gold_near[k]} + {gid}" if k in gold_near else gid
    leak_exact = [p for p in pool if json.dumps(p["evidence"], sort_keys=True) in gold_exact]
    leak_near = [p for p in pool if near_key(p["evidence"], bases) in gold_near]
    near_by_item = Counter(gold_near[near_key(p["evidence"], bases)] for p in leak_near)
    pool = [p for p in pool if near_key(p["evidence"], bases) not in gold_near]

    by_class: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for p in pool:
        by_class[p["label"]][structural_key(p["evidence"])].append(p)
    chosen = []
    for lab in CATEGORIES:
        groups = by_class.get(lab, {})
        keys = sorted(groups)
        rng.shuffle(keys)
        for k in keys:
            rng.shuffle(groups[k])
        picked, depth = [], 0
        while len(picked) < CLASS_CAP and (depth < KEY_CAP or len(picked) < CLASS_MIN):
            if depth >= max((len(v) for v in groups.values()), default=0):
                break
            for k in keys:
                if depth < len(groups[k]) and len(picked) < CLASS_CAP:
                    picked.append(groups[k][depth])
            depth += 1
        chosen += picked

    forbidden = set(bases) | {t for e in gold_ev.values() for t in e["testcases"]}
    for p in chosen:
        p["near_key"] = near_key(p["evidence"], bases)
        p["structural_key"] = structural_key(p["evidence"])
        p["augmentation"] = None
        if rng.random() < AUG_FRACTION:
            p["evidence"], p["augmentation"] = augment(p["evidence"], rng, forbidden)
        p["evidence"] = Evidence.model_validate(p["evidence"]).model_dump()

    groups: dict[str, list] = defaultdict(list)
    for p in chosen:
        groups[p["near_key"]].append(p)
    val_keys: set[str] = set()
    for lab in CATEGORIES:
        gk = sorted(k for k, ps in groups.items() if Counter(x["label"] for x in ps).most_common(1)[0][0] == lab)
        rng.shuffle(gk)
        need = round(VAL_FRACTION * sum(len(groups[k]) for k in gk))
        got = 0
        for k in gk:
            if got >= need:
                break
            val_keys.add(k)
            got += len(groups[k])
    train = [p for p in chosen if p["near_key"] not in val_keys]
    val = [p for p in chosen if p["near_key"] in val_keys]

    key_labels: dict[str, Counter] = defaultdict(Counter)
    for p in train:
        key_labels[p["structural_key"]][p["label"]] += 1

    def confidence(p) -> float:
        c = key_labels.get(p["structural_key"])
        purity = c[p["label"]] / sum(c.values()) if c else 0.7
        return round(min(0.95, max(0.5, purity)), 2)

    def row(p, split: str, n: int) -> dict:
        ev = Evidence.model_validate(p["evidence"])
        conf = confidence(p)
        dec = TriageDecision(category=p["label"], confidence=conf, component=target_component(ev, p["label"], known),
                             rationale=target_rationale(ev, p["label"]), needs_human_review=conf < 0.8)
        task = TASK_TRIAGE if rng.random() < 0.5 else TASK_EVAL
        return {"id": f"{split}{n:04d}", "label": p["label"], "task": "triage" if task == TASK_TRIAGE else "eval",
                "prompt": poc_prompt(task, p["evidence"]), "target": dec.model_dump_json(),
                "rules_pred": p["rules"], "augmentation": p["augmentation"], "structural_key": p["structural_key"],
                "near_key": p["near_key"]}

    rng.shuffle(train)
    rng.shuffle(val)
    train_rows = [row(p, "tr", i) for i, p in enumerate(train)]
    val_rows = [row(p, "va", i) for i, p in enumerate(val)]

    ref = {}
    for label, run in REFERENCE_RUNS.items():
        p = ROOT / "runs" / run / "eval.json"
        if p.exists():
            items = json.loads(p.read_text(encoding="utf-8")).get("gold_llm", {}).get("items", [])
            ref[label] = {it["id"]: it["pred"] for it in items}
    test_rows = []
    for g in gold:
        ev = Evidence.model_validate(g["evidence"])
        test_rows.append({"id": g["id"], "source": g["source"], "label": g["label"],
                          "prompt_triage": poc_prompt(TASK_TRIAGE, ev.model_dump()),
                          "prompt_eval": poc_prompt(TASK_EVAL, ev.model_dump()),
                          "rules_pred": triage.classify_rules(ev).category,
                          "ref_preds": {k: v.get(g["id"]) for k, v in ref.items()}})

    test_prompts = {r["prompt_triage"] for r in test_rows} | {r["prompt_eval"] for r in test_rows}
    prompt_overlap = sum(r["prompt"] in test_prompts for r in train_rows + val_rows)
    tr_near, va_near = {p["near_key"] for p in train}, {p["near_key"] for p in val}

    def skey(ev: dict) -> str:
        return structural_key({**ev, "message": _msg(ev["message"])})

    twins = {}
    train_struct: dict[str, Counter] = defaultdict(Counter)
    train_pattern: dict[str, Counter] = defaultdict(Counter)
    for p in train:
        ev = json.loads(p["structural_key"])
        ev = dict(zip(["kind", "message", "fingerprint", "baseline_failed", "golden_match", "families", "rerun",
                       "known_match", "error_in_testcase"], ev))
        train_struct[skey(ev)][p["label"]] += 1
        train_pattern[pattern_key(ev)][p["label"]] += 1
    for gid, e in gold_ev.items():
        c, cp = train_struct.get(skey(e), Counter()), train_pattern.get(pattern_key(e), Counter())
        g = next(x for x in gold if x["id"] == gid)
        twins[gid] = {"source": g["source"], "label": g["label"], "structural_twins": sum(c.values()),
                      "structural_twin_labels": dict(c), "pattern_twins": sum(cp.values()),
                      "pattern_twin_labels": dict(cp)}

    DATA.mkdir(exist_ok=True)
    for name, rows in (("train", train_rows), ("val", val_rows), ("test", test_rows)):
        with (DATA / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    rules_ok = Counter((p["rules"] == p["label"]) for p in chosen)
    report = {
        "seed": SEED, "system_prompt": llm.SYSTEM, "tasks": {"triage": TASK_TRIAGE, "eval": TASK_EVAL},
        "bank": {**bank_stats, "testcases": len(names), "flows": [f.id for f in flows],
                 "workers": meta["workers"], "job_timeout_s": meta["job_timeout_s"],
                 "hang_timeout_s": meta["hang_timeout_s"]},
        "compositions": N_COMPOSITIONS, "clusters_seen": n_clusters, "dropped_clusters": dict(drops),
        "unique_labelled_evidence": len(seen),
        "leakage": {"exact_duplicates_of_gold_removed": len(leak_exact),
                    "near_duplicates_of_gold_removed": len(leak_near),
                    "near_duplicates_by_gold_item": dict(near_by_item),
                    "near_duplicate_definition": " ".join(near_key.__doc__.split()),
                    "train_or_val_prompt_identical_to_a_test_prompt": prompt_overlap,
                    "near_keys_shared_by_train_and_val": len(tr_near & va_near)},
        "selection": {"key_cap": KEY_CAP, "class_cap": CLASS_CAP, "class_min": CLASS_MIN,
                      "pool_after_leakage_removal": len(pool),
                      "pool_by_class": dict(Counter(p["label"] for p in pool)),
                      "structural_keys_by_class": {lab: len(by_class.get(lab, {})) for lab in CATEGORIES}},
        "augmentation": {"fraction": AUG_FRACTION, "applied": sum(p["augmentation"] is not None for p in chosen),
                         "what": "testcase names replaced consistently by random neutral names (tc_<word>_<nn>); "
                                 "design.v:<line> numbers in the log excerpt shifted by one random offset in [-5, 40]"},
        "splits": {"train": dict(Counter(r["label"] for r in train_rows)), "val": dict(Counter(r["label"] for r in val_rows)),
                   "test": dict(Counter(r["label"] for r in test_rows)),
                   "test_by_source": dict(Counter(r["source"] for r in test_rows)),
                   "train_task_mix": dict(Counter(r["task"] for r in train_rows))},
        "rules_accuracy_on_selected_synthetic_examples": round(rules_ok[True] / max(1, len(chosen)), 3),
        "gold_twins_in_train": twins,
        "twin_definitions": {"structural": "same kind, key message (numbers masked), fingerprint, baseline_failed, "
                                           "golden_match, families, rerun, known_match and error_in_testcase",
                             "pattern": " ".join(pattern_key.__doc__.split())},
        "reference_runs": list(ref),
        "build_seconds": round(time.perf_counter() - t0, 1),
    }
    (DATA / "dataset_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("splits", "leakage", "dropped_clusters", "unique_labelled_evidence",
                                             "rules_accuracy_on_selected_synthetic_examples")}, indent=1))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=MAX_WORKERS)
    sub.add_parser("build")
    p = sub.add_parser("probe")
    p.add_argument("--tc", nargs="+", required=True)
    p.add_argument("--flows", nargs="+", required=True)
    p.add_argument("--workers", type=int, default=MAX_WORKERS)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    if a.cmd == "run":
        cmd_run(a.workers)
    elif a.cmd == "build":
        cmd_build()
    else:
        cmd_probe(a.tc, a.flows, a.workers)


if __name__ == "__main__":
    main()
