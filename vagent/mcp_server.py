"""MCP server exposing the agent's tools. Drafting is allowed; approving/filing is not (CLI only)."""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from .agent import PROFILES, TRACKER_PATH, Agent, _dump, latest_run
from .tracker import Tracker


def _agent(run_dir: str) -> Agent:
    return Agent(run_dir=Path(run_dir) if run_dir else latest_run(), mode=None, workers=4)


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x

    srv = Server("vagent-fpga-tool-verification")

    @srv.tool()
    def run_regression(profile: str = "quick", workers: int = 4) -> dict:
        """Plan from the spec, generate testcases and run the regression matrix on build=buggy."""
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}")
        with contextlib.redirect_stdout(sys.stderr):
            ag = Agent(profile=profile, workers=workers)
            ag.stage_plan()
            ag.stage_generate()
            res = ag.stage_regress()
        return {"run_dir": str(ag.dir), "jobs": len(res), "passed": sum(r.status == "pass" for r in res),
                "failures": [{"job": r.job_id, "kind": r.kind, "message": r.message[:200]}
                             for r in res if r.status == "fail"]}

    @srv.tool()
    def triage(run_dir: str = "") -> list[dict]:
        """Cluster and classify the failures of a run (default: latest)."""
        with contextlib.redirect_stdout(sys.stderr):
            ag = _agent(run_dir)
            clusters = ag.clusters() if (ag.dir / "clusters.json").exists() else ag.stage_triage()
        return [{"cluster": c.id, "category": c.decision.category, "confidence": c.decision.confidence,
                 "component": c.decision.component, "kind": c.evidence.kind, "members": len(c.members),
                 "trigger_options": c.evidence.fingerprint, "message": c.evidence.message,
                 "reproducer": c.reproducer.flow_command if c.reproducer else None} for c in clusters]

    @srv.tool()
    def get_coverage(run_dir: str = "") -> dict:
        """Feature x flow coverage matrix, gaps and proposed next tests."""
        with contextlib.redirect_stdout(sys.stderr):
            ag = _agent(run_dir)
            if not (ag.dir / "coverage.json").exists():
                if not (ag.dir / "results_effective.json").exists():
                    ag.stage_triage()
                ag.stage_coverage()
        cov = json.loads((ag.dir / "coverage.json").read_text(encoding="utf-8"))
        for p in cov["proposals"]:
            p.pop("verilog", None)
        return cov

    @srv.tool()
    def draft_defect(cluster_id: str, run_dir: str = "") -> dict:
        """Draft (never file) a defect report for a triaged cluster. Filing needs `vagent approve --by`."""
        with contextlib.redirect_stdout(sys.stderr):
            ag = _agent(run_dir)
            clusters = ag.clusters()
            cl = next((c for c in clusters if c.id == cluster_id), None)
            if cl is None:
                return {"error": f"unknown cluster {cluster_id}"}
            d = ag.draft_for(cl)
            if d is None:
                return {"error": f"{cluster_id} is {cl.decision.category}; no defect report is drafted"}
            did, new = Tracker(TRACKER_PATH).upsert_draft(d, ag.run_id)
            cl.defect_id = did
            _dump(ag.dir / "clusters.json", [c.model_dump() for c in clusters])
        return {"defect_id": did, "new": new, "status": "draft", "title": d.title,
                "next_step": f"human approval required: python -m vagent approve {did} --by <name>"}

    return srv


def serve() -> None:
    build_server().run()
