import importlib.util
import json
import random
from pathlib import Path

import pytest

from vagent import ROOT
from vagent.models import Evidence, JobResult, TriageDecision

spec = importlib.util.spec_from_file_location("make_dataset", ROOT / "finetune" / "make_dataset.py")
md = importlib.util.module_from_spec(spec)
spec.loader.exec_module(md)

DATA = ROOT / "finetune" / "data"


def _gold():
    return [json.loads(l) for l in md.GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]


def _job(tmp_path, name, kind, injected):
    d = tmp_path / name
    d.mkdir()
    if injected is not None:
        (d / "injected.json").write_text(json.dumps(injected), encoding="utf-8")
    return JobResult(job_id=name, testcase="t", flow="xc7", build="buggy", status="fail", kind=kind, job_dir=str(d))


def test_task_strings_are_the_ones_the_agent_sends():
    src = (ROOT / "vagent" / "agent.py").read_text(encoding="utf-8")
    flat = " ".join(src.split()).replace('" "', "")
    assert md.TASK_TRIAGE in flat and md.TASK_EVAL in flat


def test_local_eval_uses_the_same_task_strings():
    s = importlib.util.spec_from_file_location("eval_local", ROOT / "finetune" / "eval_local.py")
    el = importlib.util.module_from_spec(s)
    s.loader.exec_module(el)
    assert el.TASKS == {"eval": md.TASK_EVAL, "triage": md.TASK_TRIAGE}


def test_prompt_is_captured_from_llm_structured():
    ev = Evidence.model_validate(_gold()[0]["evidence"]).model_dump()
    p = md.poc_prompt(md.TASK_EVAL, ev)
    assert p.startswith(f"Task: {md.TASK_EVAL}\n\nJSON schema:\n{json.dumps(TriageDecision.model_json_schema())}")
    assert p.endswith(json.dumps(ev, default=str))


def test_cluster_label_uses_ground_truth_and_rejects_unusable_clusters(tmp_path):
    td = [{"label": "tool_defect", "detail": "TD-DSP-FLATTEN"}]
    a, b = _job(tmp_path, "a", "equiv_fail", td), _job(tmp_path, "b", "equiv_fail", td)
    assert md.cluster_label([a, b], "equiv_fail") == ("tool_defect", "ok")
    none = _job(tmp_path, "c", "equiv_fail", None)
    assert md.cluster_label([a, none], "equiv_fail")[0] is None
    both = _job(tmp_path, "d", "qor_golden", td + [{"label": "test_bug", "detail": "testcase"}])
    assert md.cluster_label([both], "qor_golden")[0] is None
    tb = _job(tmp_path, "e", "equiv_fail", [{"label": "test_bug", "detail": "testcase"}])
    assert md.cluster_label([a, tb], "equiv_fail") == (None, "cluster mixes ground-truth classes")
    slow = _job(tmp_path, "f", "timeout", td)
    assert md.cluster_label([slow], "timeout")[0] is None
    hang = _job(tmp_path, "g", "timeout", [{"label": "infrastructure", "detail": "hang"}])
    assert md.cluster_label([hang], "timeout") == ("infrastructure", "ok")
    assert md.cluster_label([tb], "equiv_fail")[0] is None


def test_near_duplicate_key_maps_parameter_variants_to_their_testcase():
    gold = {g["id"]: Evidence.model_validate(g["evidence"]).model_dump() for g in _gold()}
    c04 = gold["20261123-215943-full:C04"]
    bases = {"tc_mul_w5": "tc_mul_reg", "tc_macc_w5": "tc_macc"}
    variant = {**c04, "testcases": ["tc_macc_w5", "tc_mul_w5"], "flows_failed": ["xc7_flatten"], "signature": "x"}
    assert md.near_key(variant, bases) == md.near_key(c04, {})
    other = {**variant, "testcases": ["tc_mul_w5", "tc_muladd"]}
    assert md.near_key(other, bases) != md.near_key(c04, {})
    c08 = gold["20261123-215943-full:C08"]
    assert md.near_key({**c08, "message": "job exceeded timeout of 600s and was killed"}, {}) == md.near_key(c08, {})


def test_augmentation_renames_consistently_and_shifts_lines():
    ev = {"testcases": ["tc_b", "tc_a"], "excerpt": "design.v:8: ERROR: x\ndesign.v:8: again"}
    out, info = md.augment(ev, random.Random(1), forbidden={"tc_a", "tc_b"})
    assert sorted(info["renamed"]) == ["tc_a", "tc_b"] and not set(out["testcases"]) & {"tc_a", "tc_b"}
    n = max(1, 8 + info["line_shift"])
    assert out["excerpt"] == f"design.v:{n}: ERROR: x\ndesign.v:{n}: again"


@pytest.mark.parametrize("label", ["tool_defect", "test_bug", "infrastructure", "known_issue"])
def test_targets_validate(label):
    ev = Evidence.model_validate(_gold()[3]["evidence"])
    d = TriageDecision(category=label, confidence=0.9, component=md.target_component(ev, label, md.triage.load_known()),
                       rationale=md.target_rationale(ev, label))
    assert d.component and len(d.rationale) <= 1200


@pytest.mark.skipif(not (DATA / "train.jsonl").exists(), reason="dataset not generated")
def test_shipped_dataset_has_no_gold_items():
    gold = _gold()
    gold_near = {md.near_key(Evidence.model_validate(g["evidence"]).model_dump(), {}) for g in gold}
    gold_prompts = {md.poc_prompt(t, Evidence.model_validate(g["evidence"]).model_dump())
                    for g in gold for t in (md.TASK_TRIAGE, md.TASK_EVAL)}
    for name in ("train", "val"):
        for line in (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            assert r["near_key"] not in gold_near and r["prompt"] not in gold_prompts
            assert TriageDecision.model_validate_json(r["target"]).category == r["label"]
