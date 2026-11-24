"""Stage 11: triage evaluation against labelled data (gold set + live ground truth)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable

from .models import CATEGORIES, Cluster, Evidence, JobResult, TriageDecision

PRIORITY = ("infrastructure", "test_bug", "known_issue", "tool_defect")


def metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (label, predicted) -> accuracy, per-class precision/recall/F1, confusion matrix."""
    n = len(pairs)
    acc = sum(l == p for l, p in pairs) / n if n else 0.0
    per = {}
    for c in CATEGORIES:
        tp = sum(l == c and p == c for l, p in pairs)
        fp = sum(l != c and p == c for l, p in pairs)
        fn = sum(l == c and p != c for l, p in pairs)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
        per[c] = {"support": tp + fn, "precision": _r(prec), "recall": _r(rec), "f1": _r(f1)}
    conf = {l: {p: sum(1 for a, b in pairs if a == l and b == p) for p in CATEGORIES} for l in CATEGORIES}
    return {"n": n, "accuracy": round(acc, 3), "per_class": per, "confusion": conf}


def _r(x):
    return None if x is None else round(x, 3)


def job_label(job_dir: Path) -> str | None:
    p = job_dir / "injected.json"
    if not p.exists():
        return None
    labels = {e["label"] for e in json.loads(p.read_text(encoding="utf-8"))}
    return next((l for l in PRIORITY if l in labels), None)


def live_eval(clusters: list[Cluster], results: list[JobResult]) -> dict:
    """Job-level: every failing job inherits its cluster's category; labels come from injected.json."""
    by_job = {r.job_id: r for r in results}
    pairs, unlabelled = [], []
    for cl in clusters:
        for j in cl.members:
            lab = job_label(Path(by_job[j].job_dir))
            if lab is None:
                unlabelled.append(j)
            else:
                pairs.append((lab, cl.decision.category))
    m = metrics(pairs)
    m["unlabelled_failures"] = unlabelled
    return m


def escapes(results: list[JobResult]) -> list[str]:
    """Jobs where a tool defect was injected but no oracle flagged a failure."""
    out = []
    for r in results:
        p = Path(r.job_dir) / "injected.json"
        if r.status == "pass" and p.exists():
            if any(e["label"] == "tool_defect" for e in json.loads(p.read_text(encoding="utf-8"))):
                out.append(r.job_id)
    return out


def gold_eval(path: Path, classify: Callable[[Evidence], TriageDecision]) -> dict:
    items = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    pairs, rows = [], []
    for it in items:
        pred = classify(Evidence.model_validate(it["evidence"]))
        pairs.append((it["label"], pred.category))
        rows.append({"id": it["id"], "source": it["source"], "label": it["label"], "pred": pred.category,
                     "ok": it["label"] == pred.category})
    m = metrics(pairs)
    m["items"] = rows
    m["by_source"] = {s: metrics([(r["label"], r["pred"]) for r in rows if r["source"] == s])["accuracy"]
                      for s in sorted({r["source"] for r in rows})}
    return m


def harvest_gold(clusters: list[Cluster], results: list[JobResult], run_id: str) -> list[dict]:
    """One gold item per cluster (distinct evidence bundle), labelled by the majority ground-truth label."""
    by_job = {r.job_id: r for r in results}
    items = []
    for cl in clusters:
        labels = [job_label(Path(by_job[j].job_dir)) for j in cl.members]
        labels = [l for l in labels if l]
        if not labels:
            continue
        counts = Counter(labels)
        lab = min(counts, key=lambda l: (-counts[l], PRIORITY.index(l) if l in PRIORITY else len(PRIORITY), l))
        items.append({"id": f"{run_id}:{cl.id}", "source": "harvested", "label": lab,
                      "evidence": cl.evidence.model_dump()})
    return items
