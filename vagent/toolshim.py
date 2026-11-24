"""The "tool under test": Yosys wrapped as two emulated builds of a synthesis tool.

build="buggy" injects defects that only fire under specific feature + option conditions, the way
real tool regressions do. build="fixed" turns the tool defects off. The known issue (KDB-0001)
is present in both builds because it is an open, already-tracked defect.

Ground truth for every injected defect is written to injected.json in the job directory. Triage
never reads that file; only evaluation does.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import yosys
from .models import Flow

DEFECTS = {
    "TD-DSP-FLATTEN": "DSP48E1 A-port bit order swapped when -flatten is used",
    "TD-LUTRAM-NOIOPAD": "distributed RAM read address bits swapped when -noiopad is used",
    "TD-FSM-DFF": "FSM output-decode LUT inverted when abc9 runs with -dff",
    "TD-CARRY-FLATTEN": "carry chains (CARRY4) not used for adders when -flatten is used (QoR regression)",
    "KDB-0001": "internal error in wreduce on parameter-overridden hierarchy in generic synth (known, open)",
}

_MUL = re.compile(r"[\w\)\]\}]\s*\*\s*[\w\(\{]")
_ADD = re.compile(r"[\w\)\]\}]\s*\+\s*[\w\(\{]")


def _strip_comments(src: str) -> str:
    return re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def _cells(netlist: dict):
    for mname in sorted(netlist["modules"]):
        for cname in sorted(netlist["modules"][mname]["cells"]):
            yield mname, cname, netlist["modules"][mname]["cells"][cname]


def _mut_dsp(netlist: dict) -> bool:
    for _, _, c in _cells(netlist):
        if c["type"].startswith("DSP48"):
            a = c["connections"]["A"]
            a[0], a[1] = a[1], a[0]
            return True
    return False


def _mut_lutram(netlist: dict) -> bool:
    for _, _, c in _cells(netlist):
        conn = c["connections"]
        if re.match(yosys.FAMILY_PATTERNS["LUTRAM"], c["type"]):
            ports = [p for p in ("ADDRA", "ADDRB", "ADDRC") if p in conn and len(conn[p]) > 1]
            for p in ports:
                conn[p][0], conn[p][1] = conn[p][1], conn[p][0]
            if not ports and "DPRA0" in conn and "DPRA1" in conn:
                conn["DPRA0"], conn["DPRA1"] = conn["DPRA1"], conn["DPRA0"]
                ports = ["DPRA"]
            if ports:
                return True
    return False


def _mut_fsm_output(netlist: dict, top: str) -> bool:
    mod = netlist["modules"][top]
    out_bits = {b for p in mod["ports"].values() if p["direction"] == "output" for b in p["bits"]}
    cells = mod["cells"]
    for c in cells.values():  # look through output buffers
        if c["type"] in ("OBUF", "OBUFT") and set(c["connections"]["O"]) & out_bits:
            out_bits.update(c["connections"]["I"])
    ff_inputs = set()
    for c in cells.values():
        if c["type"].startswith("FD") and set(c["connections"].get("Q", [])) & out_bits:
            for port in ("D", "R", "S", "CE"):
                ff_inputs.update(b for b in c["connections"].get(port, []) if isinstance(b, int))
    for name in sorted(cells):
        c = cells[name]
        if c["type"].startswith("LUT") and set(c["connections"]["O"]) & ff_inputs:
            c["parameters"]["INIT"] = "".join("1" if ch == "0" else "0" for ch in c["parameters"]["INIT"])
            return True
    return False


def run_tool(source: str, top: str, flow: Flow, build: str, workdir: Path) -> tuple[int, list[str]]:
    """Synthesise design.v in workdir. Writes netlist.json and synth.log. Returns (rc, injected ids)."""
    rtl = _strip_comments(source)
    opts = flow.option_groups
    injected: list[str] = []
    cmd = flow.command
    no_index = re.sub(r"\[[^\]]*\]", "", rtl)
    hidden_nocarry = (build == "buggy" and flow.base == "synth_xilinx" and "-flatten" in opts
                      and _ADD.search(no_index) and not _MUL.search(no_index))
    if hidden_nocarry:
        cmd += " -nocarry"
    script = f"read_verilog design.v\nhierarchy -check -top {top}\n{cmd} -top {top}\nwrite_json netlist.json\n"
    rc = yosys.run_script(script, workdir, "synth")
    log_p, net_p = workdir / "synth.log", workdir / "netlist.json"
    log = log_p.read_text(encoding="utf-8", errors="replace") if log_p.exists() else ""
    if hidden_nocarry:  # the defect is internal: hide the extra flag from the user-visible script and log
        log = log.replace(" -nocarry", "")
        (workdir / "synth.ys").write_text(script.replace(" -nocarry", ""), encoding="utf-8")
        if rc == 0:
            injected.append("TD-CARRY-FLATTEN")

    if rc == 0 and flow.base == "synth" and re.search(r"#\s*\(\s*\.", rtl):
        cut = log.find("Executing WREDUCE pass")
        cut = log.find("\n", cut) if cut >= 0 else len(log)
        log = log[:cut] + "\nERROR: Assert `GetSize(sig) == width' failed in pass wreduce.\n"
        net_p.unlink(missing_ok=True)
        rc = 1
        injected.append("KDB-0001")

    if rc == 0 and build == "buggy":
        net = yosys.load_netlist(net_p)
        cells = yosys.count_cells(net, top)
        types = set(cells)
        changed = False
        if "-flatten" in opts and any(t.startswith("DSP48") for t in types) and _mut_dsp(net):
            injected.append("TD-DSP-FLATTEN")
            changed = True
        if "-noiopad" in opts and yosys.families(cells).get("LUTRAM") and _mut_lutram(net):
            injected.append("TD-LUTRAM-NOIOPAD")
            changed = True
        if "-dff" in opts and "Extracting FSM" in log and _mut_fsm_output(net, top):
            injected.append("TD-FSM-DFF")
            changed = True
        if changed:
            net_p.write_text(json.dumps(net), encoding="utf-8")

    log_p.write_text(log, encoding="utf-8")
    return rc, injected
