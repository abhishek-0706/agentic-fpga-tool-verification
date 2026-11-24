import json
import threading

import pytest

from vagent import obs, oracles, worker
from vagent.models import DefectDraft, Flow, TestCase
from vagent.tracker import ApprovalRequired, Tracker


def draft(sig="abc"):
    return DefectDraft(title="[x] netlist not equivalent with -flatten", component="synth_xilinx: DSP48 mapping",
                       severity="S1", category="tool_defect", summary="s", repro_steps=["run it"],
                       expected="e", actual="a", affected=["j1"], signature=sig)


def test_closed_defect_is_reopened_as_draft(tmp_path):
    tr = Tracker(tmp_path / "t.sqlite")
    did, _ = tr.upsert_draft(draft(), "run1")
    tr.approve(did, "A")
    tr.close(did, "ok")
    did2, new = tr.upsert_draft(draft(), "run2")
    row = tr.get(did)
    assert did2 == did and new and row["status"] == "draft" and row["approved_by"] is None
    assert row["closed_at"] is None and row["occurrences"] == 2 and row["last_run"] == "run2"
    assert tr.audit()[-1]["action"] == "reopened"
    tr.approve(did, "B")


def test_reject_validates(tmp_path):
    tr = Tracker(tmp_path / "t.sqlite")
    did, _ = tr.upsert_draft(draft(), "run1")
    with pytest.raises(ApprovalRequired):
        tr.reject(did, " ", "r")
    with pytest.raises(ValueError):
        tr.reject("DEF-9999", "A", "r")
    tr.reject(did, "A", "dup")
    with pytest.raises(ValueError):
        tr.reject(did, "A", "again")
    assert [a["action"] for a in tr.audit()] == ["drafted", "rejected"]
    assert tr.get(did)["status"] == "rejected"


def test_obs_event_threadsafe(tmp_path):
    obs.init(tmp_path)

    def work():
        for i in range(200):
            obs.event("e", i=i)
            obs.inc("n")

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600 and all(json.loads(l)["event"] == "e" for l in lines)
    assert obs.counters["n"] == 1600


def test_sat_timeout_requires_real_message(tmp_path, monkeypatch):
    tc = TestCase(name="t", generator="g", top="t", features=[], sequential=False, source="module t; endmodule")
    flow = Flow(id="generic", command="synth")

    def fake(logtext):
        def run_script(script, workdir, name):
            (workdir / "equiv.log").write_text(logtext, encoding="utf-8")
            return 1
        monkeypatch.setattr(oracles.yosys, "run_script", run_script)
        return oracles.equivalence(tc, flow, tmp_path, {}, 5)

    v, note = fake("Interrupted SAT solver: TIMEOUT!\n")
    assert v == "inconclusive" and note.startswith("SAT timeout")
    v, note = fake("ERROR: Module `TIMEOUT_SIGNAL' not found\n")
    assert v == "inconclusive" and note.startswith("oracle error")


def test_worker_writes_ground_truth_utf8(tmp_path):
    worker.write_ground_truth(tmp_path, "x", "café")
    worker.write_ground_truth(tmp_path, "y", "z")
    assert len(json.loads((tmp_path / "injected.json").read_text(encoding="utf-8"))) == 2


def test_run_regression_rejects_bad_profile_before_creating_run(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from vagent import agent
    from vagent.mcp_server import build_server

    monkeypatch.setattr(agent, "RUNS", tmp_path)
    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    assert "run_regression" in tools
    with pytest.raises(Exception):
        asyncio.run(build_server().call_tool("run_regression", {"profile": "../../x"}))
    assert list(tmp_path.iterdir()) == []
