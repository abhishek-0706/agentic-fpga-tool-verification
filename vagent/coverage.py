"""Stage 9: feature x flow coverage, gap report and next-test proposals (gated by Yosys)."""
from __future__ import annotations

from . import llm, obs, yosys
from .models import JobResult, TestCase, TestPlan, TestProposal

RANK = {"not_run": 0, "fail": 1, "qor_only": 2, "verified": 3}

TEMPLATES = {
    "FEAT-ASYNC-RST": TestProposal(
        feature_id="FEAT-ASYNC-RST", top="arst_counter", expect_xc7=["FF>=4"],
        rationale="No testcase exercises asynchronous reset/preset flip-flops (FDCE/FDPE mapping).",
        verilog="""module arst_counter #(parameter W = 4) (
  input              clk, arst, en,
  output reg [W-1:0] q
);
  always @(posedge clk or posedge arst)
    if (arst)    q <= {W{1'b0}};
    else if (en) q <= q + 1'b1;
endmodule
"""),
    "FEAT-WIDEMUX": TestProposal(
        feature_id="FEAT-WIDEMUX", top="mux32", expect_xc7=["MUXF>=1"],
        rationale="No testcase drives 32:1 multiplexers, so MUXF7/MUXF8 inference and -widemux are untested.",
        verilog="""module mux32 (
  input  [31:0] d,
  input  [4:0]  s,
  output        y
);
  assign y = d[s];
endmodule
"""),
}


def cell_status(r: JobResult) -> str:
    if r.status == "fail":
        return "fail"
    return "verified" if r.equiv in ("proven", "bounded") else "qor_only"


def matrix(plan: TestPlan, suite: list[TestCase], results: list[JobResult]) -> dict:
    flows = [f.id for f in plan.flows]
    by_tc = {tc.name: tc for tc in suite}
    cells: dict[str, dict[str, dict]] = {}
    for feat in plan.features:
        row = {}
        for fid in flows:
            hits = [r for r in results if r.flow == fid and feat.id in by_tc[r.testcase].features]
            best = max((cell_status(r) for r in hits), key=RANK.get, default="not_run")
            row[fid] = {"status": best, "tests": sorted({r.testcase for r in hits}),
                        "required": fid in feat.flows}
        cells[feat.id] = row
    required = [(f, fl) for f, row in cells.items() for fl, c in row.items() if c["required"]]
    verified = [(f, fl) for f, fl in required if cells[f][fl]["status"] == "verified"]
    return {"flows": flows, "features": {f.id: f.title for f in plan.features}, "cells": cells,
            "required": len(required), "verified": len(verified),
            "pct_verified": round(100 * len(verified) / max(1, len(required)), 1)}


def gaps(plan: TestPlan, cov: dict) -> list[dict]:
    out = []
    for feat in plan.features:
        if not feat.generators:
            out.append({"feature": feat.id, "type": "no_test", "detail": "no generator/testcase for this feature"})
        for fl, c in cov["cells"][feat.id].items():
            if c["required"] and c["status"] != "verified" and feat.generators:
                out.append({"feature": feat.id, "flow": fl, "type": c["status"],
                            "detail": {"not_run": "required flow not in regression",
                                       "fail": "only failing results",
                                       "qor_only": "no formal verdict (equivalence skipped/inconclusive)"}[c["status"]]})
    unused = [f.id for f in plan.flows if not f.in_regression]
    for fl in unused:
        out.append({"flow": fl, "type": "flow_not_run", "detail": "flow declared in spec but not in regression"})
    return out


def propose(plan: TestPlan, gap_list: list[dict], mode: str) -> list[dict]:
    """Propose next tests for features with no testcase. Every proposal must pass the Yosys gate."""
    out = []
    for g in [g for g in gap_list if g["type"] == "no_test"]:
        feat = next(f for f in plan.features if f.id == g["feature"])
        prop, source = None, "template"
        if mode == "llm":
            prop = llm.structured(
                "Write one small synthesizable Verilog-2005 module that exercises this FPGA synthesis feature, "
                "with resource expectations for AMD/Xilinx 7-series (e.g. FF>=4, MUXF>=1).",
                {"feature": feat.model_dump()}, TestProposal)
            source = "llm" if prop else "template (llm fallback)"
        prop = prop or TEMPLATES.get(feat.id)
        if prop is None:
            out.append({"feature": feat.id, "source": "none", "admitted": False, "gate": "no template"})
            continue
        ok, why = yosys.gate(prop.verilog, prop.top)
        obs.event("proposal_gate", feature=feat.id, source=source, admitted=ok)
        out.append({"feature": feat.id, "source": source, "top": prop.top, "admitted": ok, "gate": why or "clean",
                    "rationale": prop.rationale, "expect_xc7": prop.expect_xc7, "verilog": prop.verilog,
                    "status": "awaiting human review" if ok else "rejected by gate"})
    for g in [g for g in gap_list if g["type"] == "qor_only"][:1]:
        out.append({"feature": g["feature"], "source": "rule", "admitted": None,
                    "rationale": "Formal check skipped for this primitive. Add simulation of the netlist against "
                                 "the vendor primitive models (or a commercial equivalence checker that models "
                                 "the primitive) to close this gap.", "status": "recommendation"})
    return out
