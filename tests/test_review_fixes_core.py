import json

from vagent import agent as agent_mod, evaluate, llm, triage
from vagent.models import Cluster, DefectDraft, Evidence, Flow, JobResult, JobSpec, TestCase, TriageDecision

FLOWS = {f.id: f for f in [
    Flow(id="generic", command="synth"), Flow(id="xc7", command="synth_xilinx -family xc7"),
    Flow(id="xc7_flatten", command="synth_xilinx -family xc7 -flatten")]}


def jr(tc, flow, status="pass", kind=None, msg="", **kw):
    return JobResult(job_id=f"{tc}__{flow}", testcase=tc, flow=flow, build="buggy", status=status, kind=kind,
                     message=msg, **kw)


def tcase(name="tc"):
    return TestCase(name=name, generator="none", top="t", features=[], sequential=False, source="module t; endmodule")


def test_timeouts_split_by_rerun_outcome():
    hang = "job exceeded timeout of 150s and was killed"
    rs = [jr("a", "generic", "fail", "timeout", hang), jr("b", "generic", "fail", "timeout", hang)]
    cls = triage.cluster_failures(rs, FLOWS, [], {"a__generic"})
    assert len(cls) == 2
    by = {c.members[0]: c for c in cls}
    assert by["a__generic"].evidence.rerun == "passed" and by["a__generic"].decision.category == "infrastructure"
    assert by["b__generic"].evidence.rerun == "failed" and by["b__generic"].decision.category == "tool_defect"
    assert len(triage.cluster_failures(rs, FLOWS, [])) == 1


def test_rerun_pass_counts_as_passed_for_fingerprint():
    eq = "equivalence check failed: counterexample"
    rs = [jr("a", "xc7", "fail", "timeout", "job exceeded timeout of 150s and was killed"),
          jr("a", "generic"), jr("a", "xc7_flatten", "fail", "equiv_fail", eq)]
    without = triage.cluster_failures(rs, FLOWS, [])
    with_rr = triage.cluster_failures(rs, FLOWS, [], {"a__xc7"})
    eq_without = next(c for c in without if c.evidence.kind == "equiv_fail").evidence
    eq_with = next(c for c in with_rr if c.evidence.kind == "equiv_fail").evidence
    assert "xc7" not in eq_without.flows_passed and "xc7" in eq_with.flows_passed
    assert eq_without.fingerprint == ["-flatten", "synth_xilinx"] and eq_with.fingerprint == ["-flatten"]


def _cluster(msg):
    ev = Evidence(signature="s", kind="qor_expect", message=msg, testcases=["tc"], flows_failed=["xc7"],
                  flows_passed=[])
    dec = TriageDecision(category="tool_defect", confidence=0.9, component="c", rationale="r")
    return Cluster(id="C01", evidence=ev, members=["tc__xc7"], decision=dec)


def test_reduce_requires_same_failure_message():
    qe = lambda m: dict(status="fail", kind="qor_expect", msg=m)
    first = jr("tc", "xc7", **qe("QoR expectation: expected DSP48E1>=1 | x"))
    cl = _cluster(triage.key_message(first))
    spec = JobSpec(job_id="tc__xc7", testcase=tcase(), flow=FLOWS["xc7"], build="buggy")
    other = lambda pairs, tag: [jr(t.name, f.id, **qe("QoR expectation: expected LUT6>=9")) for t, f in pairs]
    assert triage.reduce_cluster(cl, {"tc__xc7": first}, {"tc__xc7": spec}, other, None) is None
    same = lambda pairs, tag: [jr(t.name, f.id, **qe("QoR expectation: expected DSP48E1>=1")) for t, f in pairs]
    assert triage.reduce_cluster(cl, {"tc__xc7": first}, {"tc__xc7": spec}, same, None) is not None


def test_reduce_cache_distinguishes_flow_base():
    qe = dict(status="fail", kind="qor_expect", msg="expected DSP48E1>=1")
    generic = jr("tc", "generic", **qe)
    cl = _cluster(triage.key_message(generic))
    cl.members = ["tc__generic", "tc__xc7"]
    specs = {"tc__generic": JobSpec(job_id="tc__generic", testcase=tcase(), flow=FLOWS["generic"], build="buggy"),
             "tc__xc7": JobSpec(job_id="tc__xc7", testcase=tcase(), flow=FLOWS["xc7"], build="buggy")}
    calls = []

    def run(pairs, tag):
        calls.extend(f.command for _, f in pairs)
        return [jr(t.name, f.id) for t, f in pairs]  # everything passes

    res = {"tc__generic": generic, "tc__xc7": jr("tc", "xc7")}
    assert triage.reduce_cluster(cl, res, specs, run, None) is None
    assert any(c.startswith("synth_xilinx") or c.startswith("synth") for c in calls)


def test_harvest_gold_tie_break_is_deterministic(tmp_path):
    res, members = [], []
    for i, lab in enumerate(["tool_defect", "test_bug", "tool_defect", "test_bug"]):
        d = tmp_path / f"j{i}"
        d.mkdir()
        (d / "injected.json").write_text(json.dumps([{"label": lab}]), encoding="utf-8")
        res.append(jr(f"t{i}", "generic", "fail", "crash", job_dir=str(d)))
        members.append(res[-1].job_id)
    cl = _cluster("m")
    cl.members = members
    assert evaluate.harvest_gold([cl], res, "r")[0]["label"] == "test_bug"


def _agent(tmp_path, mode="rules"):
    a = agent_mod.Agent.__new__(agent_mod.Agent)
    a.dir, a.mode, a.run_id = tmp_path, mode, "r"
    a.state = {"stages": {}}
    return a


def test_llm_draft_cannot_change_facts(tmp_path, monkeypatch):
    cl = _cluster("QoR expectation unmet: DSP48E1>=1")
    cl.evidence.excerpt = "real excerpt"
    a = _agent(tmp_path, "llm")
    a.mode = "rules"
    base = a.draft_for(cl)
    a.mode = "llm"
    forged = base.model_copy(update={"title": "A nicer title here", "actual": "invented", "expected": "invented",
                                     "component": "invented"})
    monkeypatch.setattr(llm, "structured", lambda *args, **kw: forged)
    out = a.draft_for(cl)
    assert out.title == "A nicer title here"
    assert (out.actual, out.expected, out.component) == (base.actual, base.expected, base.component)


def test_verify_fix_early_exit_records_stage(tmp_path, monkeypatch):
    (tmp_path / "clusters.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(agent_mod, "TRACKER_PATH", tmp_path / "t.sqlite")
    a = _agent(tmp_path)
    a.stage_verify_fix()
    assert "verify_fix" in json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["stages"]


def test_verify_fix_rerun_keeps_earlier_evidence(tmp_path, monkeypatch):
    (tmp_path / "clusters.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(agent_mod, "TRACKER_PATH", tmp_path / "t.sqlite")
    earlier = {"defects": [{"id": "DEF-0001", "closed": True}], "delta": {"fixed": ["j1"]}}
    (tmp_path / "fixverify.json").write_text(json.dumps(earlier), encoding="utf-8")
    a = _agent(tmp_path)
    assert a.stage_verify_fix() == earlier
    assert json.loads((tmp_path / "fixverify.json").read_text(encoding="utf-8")) == earlier
