"""Thin helpers around Yosys (YoWASP build, runs in-process as WebAssembly)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

FAMILY_PATTERNS = {
    "DSP": r"^DSP48",
    "BRAM": r"^RAMB(18|36)",
    "LUTRAM": r"^RAM(16|32|64|128|256)(X1[SD]|M)",
    "SRL": r"^SRL(16E|C32E)$",
    "CARRY": r"^CARRY[48]$",
    "LUT": r"^LUT[1-6]$",
    "FF": r"^FD[RSCP]E?$|^\$_S?DFFE?_",
    "MUXF": r"^MUXF[5-9]$",
    "IO": r"^(IBUF|OBUF|OBUFT|IOBUF|BUFG)$",
}


def run_script(script: str, cwd: Path, name: str) -> int:
    """Write <name>.ys into cwd and run it with log <name>.log. Returns Yosys exit code."""
    import yowasp_yosys

    (cwd / f"{name}.ys").write_text(script, encoding="utf-8")
    old = os.getcwd()
    os.chdir(cwd)
    try:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return yowasp_yosys.run_yosys(["-q", "-l", f"{name}.log", "-s", f"{name}.ys"])
    finally:
        os.chdir(old)


_warm = False


def warm() -> None:
    """Compile (or load) the WebAssembly module once before parallel workers start, so they share the cache."""
    global _warm
    if not _warm:
        import yowasp_yosys

        yowasp_yosys.run_yosys(["-q", "-p", ""])
        _warm = True


def version() -> str:
    try:
        from importlib.metadata import version as v

        return f"Yosys via yowasp-yosys {v('yowasp-yosys')}"
    except Exception:
        return "unknown"


def count_cells(netlist: dict, top: str) -> dict[str, int]:
    mods = netlist["modules"]

    def is_leaf(t: str) -> bool:
        m = mods.get(t)
        return m is None or "blackbox" in m.get("attributes", {}) or not m.get("cells")

    def walk(name: str, mult: int, acc: dict[str, int]) -> None:
        for c in mods[name]["cells"].values():
            t = c["type"]
            if is_leaf(t):
                acc[t] = acc.get(t, 0) + mult
            else:
                walk(t, mult, acc)

    acc: dict[str, int] = {}
    walk(top, 1, acc)
    return dict(sorted(acc.items()))


def families(cells: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t, n in cells.items():
        for fam, pat in FAMILY_PATTERNS.items():
            if re.match(pat, t):
                out[fam] = out.get(fam, 0) + n
    return dict(sorted(out.items()))


def load_netlist(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tmp_root() -> Path:
    from . import RUNS

    p = RUNS / "_tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def gate(source: str, top: str) -> tuple[bool, str]:
    """Deterministic admission gate for any proposed design (LLM or template):
    parse, elaborate with the given top, run proc and `check -assert`."""
    with tempfile.TemporaryDirectory(prefix="vgate_", dir=_tmp_root(), ignore_cleanup_errors=True) as d:
        p = Path(d)
        (p / "design.v").write_text(source, encoding="utf-8")
        rc = run_script(f"read_verilog design.v\nhierarchy -check -top {top}\nproc\ncheck -assert\n", p, "gate")
        log = (p / "gate.log").read_text(encoding="utf-8", errors="replace") if (p / "gate.log").exists() else ""
        errs = [l for l in log.splitlines() if "ERROR" in l or "Warning" in l]
        return rc == 0, "\n".join(errs[-5:])
