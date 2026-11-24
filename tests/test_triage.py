from vagent import triage
from vagent.models import Evidence, Flow, JobResult

FLOWS = {f.id: f for f in [
    Flow(id="generic", command="synth"), Flow(id="xc7", command="synth_xilinx -family xc7"),
    Flow(id="xc7_flatten", command="synth_xilinx -family xc7 -flatten"),
    Flow(id="xc7_dff", command="synth_xilinx -family xc7 -dff"),
    Flow(id="xc7_flat_noio", command="synth_xilinx -family xc7 -flatten -noiopad")]}


def jr(tc, flow, status="pass", kind=None, msg="", **kw):
    return JobResult(job_id=f"{tc}__{flow}", testcase=tc, flow=flow, build="buggy", status=status, kind=kind,
                     message=msg, **kw)


def test_normalize_removes_volatile_tokens():
    a = triage.normalize("design.v:8: ERROR: syntax error at C:\\tmp\\x\\design.v $auto$ff.cc:337:slice$1159 0x1f 42")
    b = triage.normalize("design.v:19: ERROR: syntax error at C:\\other\\design.v $auto$ff.cc:12:slice$7 0xff 7")
    assert a == b


def test_fingerprint_isolates_trigger_option():
    failed = [FLOWS["xc7_flatten"], FLOWS["xc7_flat_noio"]]
    passed = [FLOWS["generic"], FLOWS["xc7"], FLOWS["xc7_dff"]]
    fp, base = triage.fingerprint(failed, passed)
    assert fp == ["-flatten"] and base is False
    fp, base = triage.fingerprint(list(FLOWS.values()), [])
    assert fp == [] and base is True


def _results():
    eq = "equivalence check failed: counterexample: outputs differ on p"
    rs = [jr("tc_mul", f) for f in ("generic", "xc7", "xc7_dff")]
    rs += [jr("tc_mul", f, "fail", "equiv_fail", eq, families={"DSP": 1}) for f in ("xc7_flatten", "xc7_flat_noio")]
    rs += [jr("tc_macc", f) for f in ("generic", "xc7", "xc7_dff")]
    rs += [jr("tc_macc", f, "fail", "equiv_fail", eq.replace(" p", " acc"), families={"DSP": 1})
           for f in ("xc7_flatten", "xc7_flat_noio")]
    rs += [jr("tc_bad", f, "fail", "parse_error", f"design.v:{n}: ERROR: syntax error, unexpected TOK_ASSIGN")
           for n, f in enumerate(FLOWS)]
    rs += [jr("tc_cnt", "xc7", "fail", "timeout", "job exceeded timeout of 150s and was killed")]
    rs += [jr("tc_ph", "generic", "fail", "crash", "ERROR: Assert `GetSize(sig) == width' failed in pass wreduce.")]
    rs += [jr("tc_ph", f) for f in ("xc7", "xc7_flatten")]
    return rs


def test_clustering_groups_same_root_cause_across_testcases():
    clusters = triage.cluster_failures(_results(), FLOWS, triage.load_known())
    by_kind = {c.evidence.kind: c for c in clusters}
    eqc = by_kind["equiv_fail"]
    assert len(eqc.members) == 4 and eqc.evidence.testcases == ["tc_macc", "tc_mul"]
    assert eqc.evidence.fingerprint == ["-flatten"] and eqc.decision.category == "tool_defect"
    assert "DSP48" in eqc.decision.component
    assert by_kind["parse_error"].decision.category == "test_bug" and len(by_kind["parse_error"].members) == 5
    assert by_kind["crash"].decision.category == "known_issue"
    assert by_kind["crash"].evidence.known_match == "KDB-0001"


def test_rules_for_infra_and_stale_expectation():
    ev = Evidence(signature="s", kind="timeout", message="m", testcases=["t"], flows_failed=["xc7"],
                  flows_passed=[], rerun="passed")
    assert triage.classify_rules(ev).category == "infrastructure"
    ev = ev.model_copy(update={"rerun": "failed"})
    assert triage.classify_rules(ev).category == "tool_defect"
    ev = Evidence(signature="s", kind="qor_expect", message="QoR expectation unmet: DSP>=1", testcases=["t"],
                  flows_failed=["xc7"], flows_passed=["generic"], fingerprint=["synth_xilinx"],
                  baseline_failed=True, golden_match=True)
    assert triage.classify_rules(ev).category == "test_bug"
    ev = ev.model_copy(update={"kind": "qor_golden", "golden_match": False, "fingerprint": ["-flatten"],
                               "baseline_failed": False, "message": "QoR drift vs golden: CARRY decreased"})
    d = triage.classify_rules(ev)
    assert d.category == "tool_defect" and "CARRY" in d.component


def test_signature_is_stable_and_option_sensitive():
    assert triage.signature("equiv_fail", "x", ["-flatten"]) == triage.signature("equiv_fail", "x", ["-flatten"])
    assert triage.signature("equiv_fail", "x", ["-flatten"]) != triage.signature("equiv_fail", "x", ["-dff"])


def test_qor_drift_key_uses_primary_symptom():
    a = jr("tc_addsub", "xc7_flatten", "fail", "qor_golden",
           "QoR drift vs golden: CARRY decreased (10->0); MUXF increased (0->4); LUT increased >25% (18->40)")
    b = jr("tc_counter", "xc7_flatten", "fail", "qor_golden", "QoR drift vs golden: CARRY decreased (2->0)")
    assert triage.key_message(a) == triage.key_message(b) == "QoR drift vs golden: CARRY decreased"
