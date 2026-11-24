"""Stage 2: testcase generation from templates, plus the legacy (hand-written) suite."""
from __future__ import annotations

import json

from . import ROOT
from .models import TestCase, TestPlan


def comb_mux(W=8):
    return f"""module comb_mux #(parameter W = {W}) (
  input  [W-1:0] a, b, c, d,
  input  [1:0]   sel,
  input  [3:0]   req,
  output reg [W-1:0] y,
  output reg [1:0]   grant,
  output             any,
  output             parity
);
  always @* begin
    case (sel)
      2'd0: y = a;
      2'd1: y = b & c;
      2'd2: y = c | d;
      default: y = a ^ d;
    endcase
  end
  always @* begin
    casez (req)
      4'b1???: grant = 2'd3;
      4'b01??: grant = 2'd2;
      4'b001?: grant = 2'd1;
      default: grant = 2'd0;
    endcase
  end
  assign any = |req;
  assign parity = ^a;
endmodule
"""


def addsub(W=16):
    return f"""module addsub #(parameter W = {W}) (
  input  [W-1:0] a, b,
  input          sub,
  output [W:0]   y
);
  assign y = sub ? {{1'b0, a}} - {{1'b0, b}} : {{1'b0, a}} + {{1'b0, b}};
endmodule
"""


def mul_reg(W=6):
    return f"""module mul_reg #(parameter W = {W}) (
  input              clk,
  input  [W-1:0]     a, b,
  output reg [2*W-1:0] p
);
  always @(posedge clk) p <= a * b;
endmodule
"""


def macc(W=6, AW=16):
    return f"""module macc #(parameter W = {W}, parameter AW = {AW}) (
  input               clk, rst,
  input  [W-1:0]      a, b,
  output reg [AW-1:0] acc
);
  always @(posedge clk)
    if (rst) acc <= {{AW{{1'b0}}}};
    else     acc <= acc + a * b;
endmodule
"""


def fsm_seqdet():
    return """module fsm_seqdet (
  input      clk, rst, din,
  output reg hit
);
  localparam S0 = 2'd0, S1 = 2'd1, S2 = 2'd2, S3 = 2'd3;  // detects 1101
  reg [1:0] st;
  always @(posedge clk) begin
    if (rst) begin
      st <= S0; hit <= 1'b0;
    end else begin
      hit <= 1'b0;
      case (st)
        S0: st <= din ? S1 : S0;
        S1: st <= din ? S2 : S0;
        S2: st <= din ? S2 : S3;
        S3: begin st <= din ? S1 : S0; hit <= din; end
      endcase
    end
  end
endmodule
"""


def counter(W=8):
    return f"""module counter #(parameter W = {W}) (
  input              clk, rst, en, up,
  output reg [W-1:0] q,
  output             tc
);
  always @(posedge clk)
    if (rst)     q <= {{W{{1'b0}}}};
    else if (en) q <= up ? q + 1'b1 : q - 1'b1;
  assign tc = up ? &q : ~|q;
endmodule
"""


def shreg(W=2, DEPTH=16):
    return f"""module shreg #(parameter W = {W}, parameter DEPTH = {DEPTH}) (
  input          clk, en,
  input  [W-1:0] d,
  output [W-1:0] q
);
  reg [W*DEPTH-1:0] sr;
  always @(posedge clk)
    if (en) sr <= {{sr[W*DEPTH-W-1:0], d}};
  assign q = sr[W*DEPTH-1 -: W];
endmodule
"""


def lutram(AW=4, DW=8):
    return f"""module lutram #(parameter AW = {AW}, parameter DW = {DW}) (
  input           clk, we,
  input  [AW-1:0] wa, ra,
  input  [DW-1:0] d,
  output [DW-1:0] q
);
  reg [DW-1:0] mem [0:(1<<AW)-1];
  always @(posedge clk) if (we) mem[wa] <= d;
  assign q = mem[ra];
endmodule
"""


def bram(AW=6, DW=16):
    return f"""module bram #(parameter AW = {AW}, parameter DW = {DW}) (
  input               clk, we,
  input  [AW-1:0]     addr,
  input  [DW-1:0]     d,
  output reg [DW-1:0] q
);
  (* ram_style = "block" *) reg [DW-1:0] mem [0:(1<<AW)-1];
  always @(posedge clk) begin
    if (we) mem[addr] <= d;
    q <= mem[addr];
  end
endmodule
"""


def param_hier(N=3, W=4):
    return f"""module ph_lane #(parameter W = 4) (
  input              clk,
  input  [W-1:0]     a, b,
  output reg [W-1:0] y
);
  always @(posedge clk) y <= (a + b) ^ {{W{{a[0]}}}};
endmodule

module param_hier #(parameter N = {N}, parameter W = {W}) (
  input              clk,
  input  [N*W-1:0]   a, b,
  output [N*W-1:0]   y,
  output [2*W-1:0]   z
);
  genvar i;
  generate
    for (i = 0; i < N; i = i + 1) begin : lane
      ph_lane #(.W(W)) u (.clk(clk), .a(a[i*W +: W]), .b(b[i*W +: W]), .y(y[i*W +: W]));
    end
  endgenerate
  ph_lane #(.W(2*W)) wide (.clk(clk), .a(a[2*W-1:0]), .b(b[2*W-1:0]), .y(z));
endmodule
"""


# name -> (function, default params, top, sequential, reset port, bounded-check depth)
GENERATORS = {
    "comb_mux": (comb_mux, {"W": 8}, "comb_mux", False, None, 1),
    "addsub": (addsub, {"W": 16}, "addsub", False, None, 1),
    "mul_reg": (mul_reg, {"W": 6}, "mul_reg", True, None, 2),
    "macc": (macc, {"W": 6, "AW": 16}, "macc", True, "rst", 3),
    "fsm_seqdet": (fsm_seqdet, {}, "fsm_seqdet", True, "rst", 8),
    "counter": (counter, {"W": 8}, "counter", True, "rst", 6),
    "shreg": (shreg, {"W": 2, "DEPTH": 16}, "shreg", True, None, 19),
    "lutram": (lutram, {"AW": 4, "DW": 8}, "lutram", True, None, 4),
    "bram": (bram, {"AW": 6, "DW": 16}, "bram", True, None, 3),
    "param_hier": (param_hier, {"N": 3, "W": 4}, "param_hier", True, None, 3),
}


def make_testcase(gen: str, features: list[str], expect: dict[str, list[str]], params: dict | None = None,
                  name: str | None = None) -> TestCase:
    fn, defaults, top, seq, rst, depth = GENERATORS[gen]
    p = {**defaults, **(params or {})}
    if gen == "shreg":
        depth = p["DEPTH"] + 3
    suffix = "" if p == defaults else "_" + "_".join(f"{k}{v}" for k, v in p.items())
    return TestCase(name=name or f"tc_{gen}{suffix}", generator=gen, params=p, top=top, features=features,
                    sequential=seq, reset=rst, depth=depth, expect=expect, source=fn(**p))


def _merge_expect(plan: TestPlan, feats: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in plan.features:
        if f.id in feats:
            for fam, rules in f.expect.items():
                out.setdefault(fam, [])
                out[fam] += [r for r in rules if r not in out[fam]]
    return out


def generated_suite(plan: TestPlan) -> list[TestCase]:
    gens: dict[str, list[str]] = {}
    for f in plan.features:
        for g in f.generators:
            gens.setdefault(g, []).append(f.id)
    unknown = set(gens) - set(GENERATORS)
    if unknown:
        raise ValueError(f"spec references unknown generators: {sorted(unknown)}")
    return [make_testcase(g, feats, _merge_expect(plan, feats)) for g, feats in gens.items()]


def legacy_suite() -> list[TestCase]:
    """Hand-written legacy tests. Some contain deliberate testcase bugs (ground truth in manifest)."""
    d = ROOT / "testsuite" / "legacy"
    out = []
    for m in json.loads((d / "manifest.json").read_text(encoding="utf-8")):
        out.append(TestCase(name=m["name"], generator="legacy", top=m["top"], features=m["features"],
                            sequential=m["sequential"], reset=m.get("reset"), depth=m.get("depth", 3),
                            expect=m.get("expect", {}), source=(d / m["file"]).read_text(encoding="utf-8"),
                            origin="legacy", ground_truth=m.get("_ground_truth")))
    return out
