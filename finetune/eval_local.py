"""Score an OpenAI-compatible endpoint (for example the fine-tuned model behind llama-server) on the PoC gold set
with the same call, prompt, parsing and rules fallback as `vagent` stage 11. Writes finetune/results/<name>.json and
does not touch runs/.

  LLM_PROVIDER=openai_compat LLM_BASE_URL=http://localhost:8080/v1 LLM_MODEL=triage-qwen python finetune/eval_local.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vagent import evaluate, llm, obs, triage  # noqa: E402
from vagent.models import TriageDecision  # noqa: E402

GOLD = ROOT / "eval" / "triage_gold.jsonl"
TASKS = {  # the strings vagent/agent.py uses: stage 11 gold evaluation and stage 6 triage
    "eval": "Classify this regression failure cluster as tool_defect, test_bug, infrastructure or known_issue.",
    "triage": "Classify this regression failure cluster as tool_defect, test_bug, infrastructure or known_issue, "
              "name the tool component, and justify from the evidence only.",
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=sorted(TASKS), default="eval")
    ap.add_argument("--name", default=None, help="output file name (default: LLM_MODEL)")
    ap.add_argument("--out-dir", type=Path, default=HERE / "results")
    a = ap.parse_args(argv)
    if llm.provider_name() != "openai_compat":
        raise SystemExit("set LLM_PROVIDER=openai_compat, LLM_BASE_URL and LLM_MODEL")
    name = re.sub(r"[^\w.-]+", "_", a.name or os.environ["LLM_MODEL"])
    events_dir = a.out_dir / f"local_{name}_{a.task}"
    events_dir.mkdir(parents=True, exist_ok=True)
    obs.init(events_dir)
    strict: list[str] = []

    def classify(ev):
        d = llm.structured(TASKS[a.task], ev.model_dump(), TriageDecision)
        strict.append(d.category if d else "invalid")
        return d or triage.classify_rules(ev)

    t0 = time.perf_counter()
    poc = evaluate.gold_eval(GOLD, classify)
    rows = poc["items"]
    strict_by = {s: evaluate.metrics([(r["label"], p) for r, p in zip(rows, strict) if s in ("all", r["source"])])
                 for s in ("all", "harvested", "handwritten")}
    out = {"endpoint": os.environ.get("LLM_BASE_URL"), "model": os.environ.get("LLM_MODEL"), "task": TASKS[a.task],
           "seconds": round(time.perf_counter() - t0, 1), "invalid_outputs": strict.count("invalid"),
           "strict": {s: {"n": m["n"], "accuracy": m["accuracy"], "per_class": m["per_class"]}
                      for s, m in strict_by.items()},
           "poc_style_with_rules_fallback": {"accuracy": poc["accuracy"], "by_source": poc["by_source"],
                                             "per_class": poc["per_class"]},
           "items": [{**r, "strict_pred": p} for r, p in zip(rows, strict)]}
    path = a.out_dir / f"local_{name}_{a.task}.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"strict accuracy {strict_by['all']['accuracy']} (harvested {strict_by['harvested']['accuracy']}, "
          f"handwritten {strict_by['handwritten']['accuracy']}), invalid {out['invalid_outputs']}/{len(rows)}; "
          f"PoC-style with rules fallback {poc['accuracy']} -> {path}")


if __name__ == "__main__":
    main()
