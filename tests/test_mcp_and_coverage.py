import asyncio

import pytest

from vagent import ROOT, coverage, spec
from vagent.generators import generated_suite
from vagent.models import JobResult


def test_coverage_matrix_and_gaps():
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    suite = generated_suite(plan)
    res = [JobResult(job_id="a", testcase="tc_mul_reg", flow="xc7", build="buggy", status="pass", equiv="bounded"),
           JobResult(job_id="b", testcase="tc_bram", flow="xc7", build="buggy", status="pass", equiv="skipped"),
           JobResult(job_id="c", testcase="tc_mul_reg", flow="xc7_flatten", build="buggy", status="fail",
                     kind="equiv_fail")]
    cov = coverage.matrix(plan, suite, res)
    assert cov["cells"]["FEAT-DSP"]["xc7"]["status"] == "verified"
    assert cov["cells"]["FEAT-BRAM"]["xc7"]["status"] == "qor_only"
    assert cov["cells"]["FEAT-DSP"]["xc7_flatten"]["status"] == "fail"
    assert cov["cells"]["FEAT-DSP"]["xc7_widemux"]["required"] is False
    gaps = coverage.gaps(plan, cov)
    assert {"feature": "FEAT-ASYNC-RST", "type": "no_test", "detail": "no generator/testcase for this feature"} in gaps
    assert any(g.get("flow") == "xc7_widemux" and g["type"] == "flow_not_run" for g in gaps)


def test_mcp_server_exposes_tools():
    pytest.importorskip("mcp")
    from vagent.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    assert names == {"run_regression", "triage", "get_coverage", "draft_defect"}
