"""Stage 1: parse the markdown feature spec into a structured, validated TestPlan."""
from __future__ import annotations

import re
from pathlib import Path

from .models import Feature, Flow, TestPlan

_FEATURE_HDR = re.compile(r"^###\s+(FEAT-[A-Z0-9-]+):\s*(.+)$")
_BULLET = re.compile(r"^-\s+([a-z0-9_.]+):\s*(.*)$")
_EXPECT = re.compile(r"^[A-Za-z0-9_]+\s*>=\s*\d+$")


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def parse_spec(path: Path) -> TestPlan:
    text = path.read_text(encoding="utf-8")
    flows: list[Flow] = []
    features: list[Feature] = []
    section, cur = None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if section == "flows" and line.startswith("|") and "`" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            flows.append(Flow(id=cells[0], command=cells[1].strip("`"), in_regression=cells[2] == "yes"))
        elif section == "features":
            m = _FEATURE_HDR.match(line)
            if m:
                cur = Feature(id=m.group(1), title=m.group(2))
                features.append(cur)
                continue
            b = _BULLET.match(line)
            if cur and b:
                key, val = b.group(1), b.group(2)
                if key == "constructs":
                    cur.constructs = _split(val)
                elif key == "generators":
                    cur.generators = _split(val)
                elif key.startswith("expect."):
                    rules = _split(val)
                    bad = [r for r in rules if not _EXPECT.match(r)]
                    if bad:
                        raise ValueError(f"{cur.id}: bad expectation {bad}")
                    cur.expect[key.split(".", 1)[1]] = [r.replace(" ", "") for r in rules]
                elif key == "flows":
                    cur.flows = _split(val)
                elif key == "priority":
                    cur.priority = val
    flow_ids = {f.id for f in flows}
    for f in features:
        if f.flows in ([], ["all"]):
            f.flows = [x.id for x in flows if x.in_regression]
        unknown = set(f.flows) - flow_ids
        if unknown:
            raise ValueError(f"{f.id}: unknown flows {sorted(unknown)}")
    if not flows or not features:
        raise ValueError("spec has no flows or no features")
    return TestPlan(spec_path=str(path), flows=flows, features=features)
