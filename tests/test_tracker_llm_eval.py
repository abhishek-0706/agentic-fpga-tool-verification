import json

import pytest

from vagent import evaluate, llm, obs
from vagent.models import DefectDraft, TriageDecision
from vagent.tracker import ApprovalRequired, Tracker


def draft(sig="abc"):
    return DefectDraft(title="[x] netlist not equivalent with -flatten", component="synth_xilinx: DSP48 mapping",
                       severity="S1", category="tool_defect", summary="s", repro_steps=["run it"],
                       expected="e", actual="a", affected=["j1"], signature=sig)


def test_tracker_dedup_and_human_gate(tmp_path):
    tr = Tracker(tmp_path / "t.sqlite")
    did, new = tr.upsert_draft(draft(), "run1")
    assert new and did == "DEF-0001"
    did2, new2 = tr.upsert_draft(draft(), "run2")
    assert did2 == did and not new2 and tr.get(did)["occurrences"] == 2
    with pytest.raises(ApprovalRequired):
        tr.close(did, "fix verified")  # cannot close a draft: it was never filed by a human
    with pytest.raises(ApprovalRequired):
        tr.approve(did, "  ")
    tr.approve(did, "Reviewer A")
    assert tr.get(did)["status"] == "filed" and tr.get(did)["approved_by"] == "Reviewer A"
    tr.close(did, "fix verified")
    assert tr.get(did)["status"] == "closed"
    assert [a["action"] for a in tr.audit()] == ["drafted", "duplicate_seen", "approved_and_filed",
                                                 "closed_fix_verified"]


def test_llm_structured_validates_and_falls_back(tmp_path):
    obs.init(tmp_path)
    good = json.dumps({"category": "test_bug", "confidence": 0.8, "component": "testsuite", "rationale": "r"})
    out = llm.structured("t", {}, TriageDecision, call=lambda p: f"```json\n{good}\n```")
    assert out and out.category == "test_bug"
    bad = json.dumps({"category": "cosmic_rays", "confidence": 3, "component": "", "rationale": ""})
    assert llm.structured("t", {}, TriageDecision, call=lambda p: bad) is None
    assert llm.structured("t", {}, TriageDecision, call=lambda p: "I think it is a tool bug") is None

    def boom(p):
        raise TimeoutError("no network")

    assert llm.structured("t", {}, TriageDecision, call=boom) is None
    events = [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events == ["llm_ok", "llm_fallback", "llm_fallback", "llm_fallback"]


def test_provider_selection(monkeypatch):
    for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert llm.provider_name() is None
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "some-open-model")
    assert llm.provider_name() == "openai_compat"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm.provider_name() == "gemini"


def test_metrics():
    m = evaluate.metrics([("tool_defect", "tool_defect"), ("test_bug", "tool_defect"), ("test_bug", "test_bug"),
                          ("infrastructure", "infrastructure")])
    assert m["accuracy"] == 0.75
    assert m["per_class"]["tool_defect"]["precision"] == 0.5 and m["per_class"]["tool_defect"]["recall"] == 1.0
    assert m["per_class"]["test_bug"]["recall"] == 0.5
    assert m["per_class"]["known_issue"]["support"] == 0
