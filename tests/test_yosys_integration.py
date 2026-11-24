"""Runs real Yosys (YoWASP). Generic `synth` only, so it stays fast enough for CI."""
import json

from vagent import ROOT, generators, runner, spec, yosys
from vagent.worker import run_job


def test_gate_admits_good_and_rejects_bad_verilog():
    ok, _ = yosys.gate(generators.comb_mux(), "comb_mux")
    assert ok
    ok, why = yosys.gate("module m(input a, output y); assign y = a\nendmodule\n", "m")
    assert not ok and "ERROR" in why
    ok, _ = yosys.gate(generators.comb_mux(), "wrong_top")
    assert not ok


def test_generic_job_end_to_end(tmp_path):
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    tc = generators.make_testcase("comb_mux", ["FEAT-COMB"], {})
    job = runner.make_job(tmp_path, tc, plan.flow("generic"), "buggy", infra=False)
    res = run_job(tmp_path / job.job_id)
    assert res.status == "pass" and res.equiv == "proven"
    assert res.families.get("FF", 0) == 0


def test_known_issue_crash_is_emulated(tmp_path):
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    tc = generators.make_testcase("param_hier", ["FEAT-HIER"], {})
    job = runner.make_job(tmp_path, tc, plan.flow("generic"), "fixed", infra=False)
    res = run_job(tmp_path / job.job_id)
    assert res.status == "fail" and res.kind == "crash" and "wreduce" in res.message
    labels = json.loads((tmp_path / job.job_id / "injected.json").read_text())
    assert labels == [{"label": "known_issue", "detail": "KDB-0001"}]
