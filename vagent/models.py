from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["tool_defect", "test_bug", "infrastructure", "known_issue"]
CATEGORIES: tuple[str, ...] = ("tool_defect", "test_bug", "infrastructure", "known_issue")
FailureKind = Literal[
    "parse_error", "elab_error", "crash", "equiv_fail", "qor_expect", "qor_golden", "timeout", "infra"
]
EquivVerdict = Literal["proven", "bounded", "fail", "inconclusive", "skipped", "not_run"]


class Flow(BaseModel):
    id: str
    command: str
    in_regression: bool = True

    @property
    def base(self) -> str:
        return self.command.split()[0]

    @property
    def family(self) -> str:
        return "xc7" if self.base == "synth_xilinx" else "generic"

    @property
    def option_groups(self) -> list[str]:
        """Optional flags as groups ("-widemux 5" stays together). -family is part of the base."""
        toks, groups, i = self.command.split()[1:], [], 0
        while i < len(toks):
            if toks[i] == "-family":
                i += 2
                continue
            if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
                groups.append(f"{toks[i]} {toks[i + 1]}")
                i += 2
            else:
                groups.append(toks[i])
                i += 1
        return groups

    def with_options(self, groups: list[str], suffix: str = "min") -> "Flow":
        base = [self.base] + (["-family", "xc7"] if self.family == "xc7" else [])
        return Flow(id=f"{self.id}~{suffix}", command=" ".join(base + groups))


class Feature(BaseModel):
    id: str
    title: str
    constructs: list[str] = []
    generators: list[str] = []
    expect: dict[str, list[str]] = {}
    flows: list[str] = []
    priority: str = "P2"


class TestPlan(BaseModel):
    __test__ = False
    spec_path: str
    flows: list[Flow]
    features: list[Feature]

    def flow(self, fid: str) -> Flow:
        return next(f for f in self.flows if f.id == fid)

    @property
    def regression_flows(self) -> list[Flow]:
        return [f for f in self.flows if f.in_regression]


class TestCase(BaseModel):
    __test__ = False
    name: str
    generator: str
    params: dict[str, int] = {}
    top: str
    features: list[str]
    sequential: bool
    reset: Optional[str] = None
    depth: int = 3
    expect: dict[str, list[str]] = {}
    source: str
    origin: Literal["generated", "legacy", "proposal"] = "generated"
    ground_truth: Optional[str] = None  # hidden label for evaluation only; triage never reads it


class JobSpec(BaseModel):
    job_id: str
    testcase: TestCase
    flow: Flow
    build: Literal["buggy", "fixed"]
    attempt: int = 1
    infra_fault: Optional[Literal["hang", "enospc"]] = None
    golden: Optional[dict[str, int]] = None
    sat_timeout: int = 60


class JobResult(BaseModel):
    job_id: str
    testcase: str
    flow: str
    build: str
    attempt: int = 1
    status: Literal["pass", "fail"]
    kind: Optional[FailureKind] = None
    message: str = ""
    equiv: EquivVerdict = "not_run"
    equiv_note: str = ""
    cells: dict[str, int] = {}
    families: dict[str, int] = {}
    qor_failures: list[str] = []
    golden_match: Optional[bool] = None
    excerpt: str = ""
    duration_s: float = 0.0
    job_dir: str = ""


class Evidence(BaseModel):
    signature: str
    kind: FailureKind
    message: str
    excerpt: str = ""
    testcases: list[str]
    flows_failed: list[str]
    flows_passed: list[str]
    fingerprint: list[str] = []
    baseline_failed: bool = False
    golden_match: Optional[bool] = None
    families: list[str] = []
    rerun: Literal["not_run", "passed", "failed"] = "not_run"
    known_match: Optional[str] = None
    error_in_testcase: bool = False


class TriageDecision(BaseModel):
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    component: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=1200)
    needs_human_review: bool = False


class Reproducer(BaseModel):
    testcase: str
    params: dict[str, int]
    flow_command: str
    removed_options: list[str] = []
    design_lines: int
    job_dir: str
    trials: int


class Cluster(BaseModel):
    id: str
    evidence: Evidence
    members: list[str]
    decision: TriageDecision
    decided_by: Literal["rules", "llm"] = "rules"
    rules_decision: Optional[TriageDecision] = None
    reproducer: Optional[Reproducer] = None
    defect_id: Optional[str] = None


class DefectDraft(BaseModel):
    title: str = Field(min_length=8, max_length=160)
    component: str = Field(min_length=1, max_length=120)
    severity: Literal["S1", "S2", "S3", "S4"]
    category: Literal["tool_defect", "test_bug"]
    summary: str = Field(min_length=1, max_length=2000)
    repro_steps: list[str] = Field(min_length=1)
    expected: str
    actual: str
    affected: list[str]
    signature: str


class TestProposal(BaseModel):
    __test__ = False
    feature_id: str
    top: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    rationale: str
    verilog: str = Field(min_length=20, max_length=8000)
    expect_xc7: list[str] = []
