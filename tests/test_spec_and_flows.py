from pathlib import Path

import pytest

from vagent import ROOT, generators, spec
from vagent.models import Flow


def test_spec_parses_into_plan():
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    ids = {f.id for f in plan.features}
    assert {"FEAT-DSP", "FEAT-BRAM", "FEAT-ASYNC-RST"} <= ids
    assert plan.flow("xc7_flatten").command == "synth_xilinx -family xc7 -flatten"
    assert not plan.flow("xc7_widemux").in_regression
    dsp = next(f for f in plan.features if f.id == "FEAT-DSP")
    assert dsp.expect == {"xc7": ["DSP>=1"]} and dsp.generators == ["mul_reg", "macc"]
    assert next(f for f in plan.features if f.id == "FEAT-ASYNC-RST").generators == []


def test_bad_expectation_rejected(tmp_path: Path):
    p = tmp_path / "s.md"
    p.write_text("## Flows\n| id | command | regression |\n|--|--|--|\n| g | `synth` | yes |\n"
                 "## Features\n### FEAT-X: x\n- expect.xc7: DSP at least one\n")
    with pytest.raises(ValueError):
        spec.parse_spec(p)


def test_flow_option_groups_and_reduction():
    f = Flow(id="x", command="synth_xilinx -family xc7 -flatten -widemux 5 -noiopad")
    assert f.option_groups == ["-flatten", "-widemux 5", "-noiopad"]
    assert f.family == "xc7"
    assert f.with_options(["-noiopad"]).command == "synth_xilinx -family xc7 -noiopad"
    assert Flow(id="g", command="synth").option_groups == []


def test_generated_suite_covers_generators():
    plan = spec.parse_spec(ROOT / "spec" / "feature_spec.md")
    suite = generators.generated_suite(plan)
    names = {t.name for t in suite}
    assert "tc_mul_reg" in names and "tc_param_hier" in names
    macc = next(t for t in suite if t.name == "tc_macc")
    assert set(macc.features) == {"FEAT-DSP", "FEAT-SEQ"} and macc.reset == "rst"
    legacy = generators.legacy_suite()
    assert all(t.ground_truth == "test_bug" for t in legacy)


def test_fault_triggers_see_arithmetic_in_generated_rtl():
    import re

    from vagent.toolshim import _ADD, _MUL, _strip_comments

    def ops(src):
        no_index = re.sub(r"\[[^\]]*\]", "", _strip_comments(src))
        return bool(_ADD.search(no_index)), bool(_MUL.search(no_index))

    assert ops(generators.addsub()) == (True, False)
    assert ops(generators.counter()) == (True, False)
    assert ops(generators.mul_reg())[1] and ops(generators.macc())[1]
    assert ops(generators.bram()) == (False, False)  # (* ram_style *) attribute is not a multiply
