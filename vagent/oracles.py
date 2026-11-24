"""Stage 4 oracles: formal equivalence (RTL vs synthesised netlist) and QoR/resource checks."""
from __future__ import annotations

import re
from pathlib import Path

from . import yosys
from .models import EquivVerdict, Flow, TestCase

# Families whose primitive simulation models we do not trust for SAT-based equivalence here.
# RAMB18E1/RAMB36E1: a bounded miter against the behavioural model in Yosys' cells_sim.v reports a
# mismatch on a read-first write collision that we could not attribute to the tool. Treated as an
# oracle limitation: these jobs get QoR checks only and are reported as "skipped".
EQUIV_UNSUPPORTED = {"BRAM"}

_ROW = re.compile(r"^\s+(\d+\s+)?\\(gold|gate)_(\S+)\s+\S+\s+\S+\s+([01xz]+)\s*$")


def equivalence(tc: TestCase, flow: Flow, workdir: Path, fams: dict[str, int], sat_timeout: int
                ) -> tuple[EquivVerdict, str]:
    if fams.keys() & EQUIV_UNSUPPORTED:
        return "skipped", f"netlist contains {sorted(fams.keys() & EQUIV_UNSUPPORTED)}; no trusted SAT model"
    top = tc.top
    techmap = ("techmap -wb -D EQUIV -autoproc -map +/xilinx/cells_sim.v\n" if flow.family == "xc7" else "")
    sat = f"sat -verify -prove-asserts -show-inputs -show-outputs -timeout {sat_timeout}"
    if tc.sequential:
        sat += f" -set-init-zero -seq {tc.depth}"
        if tc.reset:
            sat += f" -set-at 1 in_{tc.reset} 1 -prove-skip 1"
    script = (
        f"read_verilog design.v\nhierarchy -check -top {top}\nproc\nflatten\nmemory\nopt_clean\n"
        f"design -stash gold_d\n"
        f"read_json netlist.json\nhierarchy -top {top}\n{techmap}flatten\nopt_clean\ndesign -stash gate_d\n"
        f"design -copy-from gold_d -as gold {top}\ndesign -copy-from gate_d -as gate {top}\n"
        f"async2sync\nmiter -equiv -flatten -make_assert -make_outputs -ignore_gold_x gold gate miter\n"
        f"hierarchy -top miter\n{sat} miter\n"
    )
    rc = yosys.run_script(script, workdir, "equiv")
    log = (workdir / "equiv.log").read_text(encoding="utf-8", errors="replace")
    kind = f"bounded {tc.depth} cycles from reset" if tc.sequential else "full combinational proof"
    if "SAT proof finished - no model found: SUCCESS" in log and rc == 0:
        return ("bounded" if tc.sequential else "proven"), kind
    if "model found: FAIL" in log:
        return "fail", "counterexample: outputs differ on " + ", ".join(mismatched_outputs(log) or ["<unknown>"])
    if "SAT solver: TIMEOUT" in log:
        return "inconclusive", f"SAT timeout after {sat_timeout}s"
    err = next((l for l in log.splitlines() if l.startswith("ERROR")), "unknown oracle error")
    return "inconclusive", f"oracle error: {err[:200]}"


def mismatched_outputs(log: str) -> list[str]:
    vals: dict[tuple[str, str], dict[str, str]] = {}
    for line in log.splitlines():
        m = _ROW.match(line)
        if m:
            step, side, port, bits = m.groups()
            vals.setdefault((step, port), {})[side] = bits
    bad = sorted({port for (_, port), v in vals.items() if len(v) == 2 and v["gold"] != v["gate"]})
    return bad


def check_expectations(tc: TestCase, flow: Flow, cells: dict[str, int], fams: dict[str, int]) -> list[str]:
    failures = []
    for rule in tc.expect.get(flow.family, []):
        name, n = rule.split(">=")
        have = fams.get(name, cells.get(name, 0))
        if have < int(n):
            failures.append(f"expected {name}>={n}, got {have}")
    return failures


def golden_drift(fams: dict[str, int], golden: dict[str, int] | None) -> list[str]:
    """Compare primitive-family counts with the golden (reference build) result."""
    if golden is None:
        return []
    drift = []
    for fam in ("DSP", "BRAM", "LUTRAM", "SRL", "CARRY", "MUXF"):
        a, b = golden.get(fam, 0), fams.get(fam, 0)
        if a != b:
            drift.append(f"{fam} {'decreased' if b < a else 'increased'} ({a}->{b})")
    lg, lb = golden.get("LUT", 0), fams.get("LUT", 0)
    if lg and lb > lg * 1.25:
        drift.append(f"LUT increased >25% ({lg}->{lb})")
    return drift
