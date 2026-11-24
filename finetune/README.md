# Fine-tuning a small open model for failure triage (optional)

This folder fine-tunes a small open-source instruct model with LoRA so that it classifies regression failure
clusters the way the PoC's triage stage does (`tool_defect`, `test_bug`, `infrastructure`, `known_issue`), and
compares it, on the same 22-item gold set, with the rule classifier and with the hosted `openai/gpt-oss-120b` runs
already reported in the main README. Training runs in a free Kaggle or Colab notebook on one T4 GPU.

Status: the notebook has been run end to end only as a CPU smoke test with a 0.5B model (see
[What was verified locally](#what-was-verified-locally)). The 1.5B GPU run has not been done yet, so there are no
accuracy numbers here. With 22 test items the comparison is a sanity check, not a benchmark; see [Caveats](#caveats).

## Files

| Path | What it is |
|---|---|
| `make_dataset.py` | `run`: runs the job bank (Yosys through the PoC's runner, worker and toolshim) into `_gen/`; `build`: turns it into labelled examples in `data/` |
| `build_notebook.py` | writes `triage_lora_finetune.ipynb` with `data/*.jsonl` embedded (gzip + base64) |
| `triage_lora_finetune.ipynb` | the self-contained notebook you upload to Kaggle or Colab |
| `eval_local.py` | scores any OpenAI-compatible endpoint (for example the fine-tuned model in llama-server) on the gold set with the PoC's own code; does not touch `runs/` |
| `data/train.jsonl`, `data/val.jsonl`, `data/test.jsonl` | the prompts and targets (test = the 22 gold items) |
| `data/dataset_report.json` | every number quoted below: bank, drops, leakage checks, class balance, structural twins |
| `results/` | put the notebook's `results.json` here |
| `_gen/`, `.venv-smoke/` | generated job bank (about 2.8 GB of job directories; only `build` needs it, so it can be deleted once `data/` exists) and the local smoke-test venv; both gitignored |

## How the training data was made

Labels come from ground truth (`injected.json`, written by the worker and toolshim when a fault is injected), never
from the rule classifier, so the model is not taught to imitate the rules. All numbers are in
`data/dataset_report.json`.

**1. Job bank** (`make_dataset.py run`, real Yosys runs through `runner.make_job`/`runner.run_jobs`, the worker and
the toolshim, `build=buggy`): 44 testcases x 7 flows = 308 jobs, plus 54 jobs with an emulated infrastructure fault.

| Testcases | n | Ground truth comes from |
|---|---|---|
| the PoC's 10 generated testcases (default parameters) and its 3 legacy testcases | 13 | toolshim defects, KDB-0001, the legacy suite's own `_ground_truth` |
| parameter variants of the generators (for example `comb_mux` W=4, `mul_reg` W=5, `lutram` 8x4) | 10 | toolshim defects, KDB-0001 |
| new designs: two more FSMs, a 2-read-port LUTRAM, a multiply-add (DSP), four hierarchical wrappers that instantiate a generator with a `#(.P())` override | 8 | toolshim defects, KDB-0001 |
| testcase-bug mutants: 5 syntax errors, 3 wrong top-module names, 5 stale or impossible resource expectations | 13 | `TestCase.ground_truth = "test_bug"`, the mechanism the legacy suite uses |

Flows: `generic`, `xc7`, `xc7_flatten`, `xc7_noiopad`, `xc7_dff`, `xc7_flat_noio` from the spec (`xc7_widemux` left
out to save time) plus `synth -flatten`. No new fault types were added to `vagent`; the bank only exercises the
existing ones (TD-DSP-FLATTEN, TD-LUTRAM-NOIOPAD, TD-FSM-DFF, TD-CARRY-FLATTEN, KDB-0001, testcase bugs, `hang`,
`enospc`) on more designs and flow combinations. Result: 166 jobs pass, 142 fail (42 parse, 28 elaboration,
12 crash, 21 equivalence, 10 golden drift, 29 expectation), 0 escapes. Golden QoR references were computed the way
`vagent bless` does (primitive families from `build=fixed`, synthesis only) into `_gen/golden.json`; the project's
`golden/qor_golden.json` was used unchanged for the PoC's own testcases, and 18 of 18 project entries recomputed this
way matched exactly. Some testcases deliberately have no golden, as new testcases often do. The 54 infrastructure
faults (36 `enospc`, 18 `hang`) were set through `JobSpec.infra_fault` on randomly chosen passing jobs; hangs are
killed at 150 s, the PoC's default per-job timeout. All other jobs ran with at most 4 workers and a 600 s timeout so
that CPU contention could not produce timeouts; none occurred. Yosys wall time on this shared laptop: about 100
minutes in total (golden 11 min, bank 76 min, faults 3 min, plus 13 min to redo the hangs: a first run killed them at
30 s, and because the timeout value is part of the failure message, every training timeout would have said "30s", a
cue that does not exist in real runs). That is more than the 30 to 60 minutes planned, because the CPU was shared,
partly with this work's own CPU smoke test.

**2. Small regressions and triage** (`make_dataset.py build`, under a minute): 6,000 random regressions drawn from the bank
(2 to 7 testcases, 3 to 7 flows; a quarter of them use the PoC's 5 regression flows; half of them have one or two
passing jobs replaced by their infrastructure-faulted version). Each is clustered by `vagent.triage.cluster_failures`
(the stage 6 code), which builds the `Evidence` exactly as the PoC does. The rerun of an infrastructure suspect is
the clean job of the same testcase and flow, which is what a rerun without the injected fault produces. Each cluster
is labelled from its members' `injected.json` with `vagent.evaluate.job_label`; a cluster is dropped when a member has
no injected fault, a job carries labels of two classes, the members disagree, a timeout or worker death was not an
injected hang or disk-full fault, or the label cannot produce that failure kind (1 cluster was dropped, for mixed
classes). This gave 5,691 distinct labelled evidence bundles.

**3. Leakage control against the 22 gold items (the test set)**: 8 bundles identical to a gold item and 1,058
near-duplicates were removed. Near-duplicate means the same failure kind and key message (numbers masked) on the same
set of testcases, counting parameter variants and mutants of a generator as that generator's testcase, whatever the
flows, signature hash or log excerpt; for example every KDB-0001 cluster on `param_hier` alone and every DSP cluster
on exactly `{macc, mul_reg}` is gone. After the split, no training or validation prompt is identical to a test prompt
and no near-duplicate group is shared between training and validation (checked again by
`tests/test_finetune_dataset.py`).

**4. Selection, augmentation, split**: round-robin over structural patterns (at most 14 examples per pattern, at
most 130 and at least 60 per class) gave 380 examples. On 122 of them (32%) the testcase names were replaced
consistently by random neutral names (`tc_<word>_<nn>`) and the `design.v:<line>` numbers in the log excerpt were
shifted by one random offset, so the model cannot key on names such as `tc_legacy_*`. Validation takes whole
near-duplicate groups until it holds at least 15% of each class (15 to 22% in the end):

| Split | tool_defect | test_bug | infrastructure | known_issue | total |
|---|---|---|---|---|---|
| train | 106 | 101 | 51 | 51 | 309 |
| validation | 24 | 29 | 9 | 9 | 71 |
| test (gold set: 10 harvested + 12 handwritten) | 10 | 6 | 4 | 2 | 22 |

The rule classifier scores 0.945 on these 380 synthetic examples; its misses are stale expectations on testcases
without a golden, which it calls tool defects.

**5. Prompt and target**: the user message is the exact string `llm.structured` sends (captured through its `call`
hook): task text + `TriageDecision` JSON schema + evidence JSON, with the PoC's system prompt. Both task strings the
PoC uses occur (triage 141, gold evaluation 168 of the training rows), so the model works for `demo --mode llm` and
for the gold evaluation. The target is a valid `TriageDecision`: the ground-truth category; a component name from the
PoC's vocabulary (`triage.component_for` for tool defects); a one-sentence template rationale built from facts in the
evidence plus the true class; a confidence equal to the share of that class among training examples with the same
structural pattern, capped at 0.95 (every pattern turned out to be pure, so every target says 0.95 and the
confidence carries no information); `needs_human_review` when that confidence is below 0.8 (never, here). Examples
are 1,390 to 1,960 characters, which is 534 tokens on average and at most 655 with the Qwen2.5 tokenizer (prompt and answer
together), so sequences are short for a T4 (`MAX_LEN` is 1,536 and the notebook stops if any example is longer).

**How in-distribution is each test item?** The report counts, for every gold item, training examples with the same
decision-relevant pattern (kind, option-triggered or not, baseline result, golden result, rerun result, known-defect
match, error location). All 10 harvested items have such look-alikes, as intended. Among the 12 handwritten items:

| Item | Label | Pattern look-alikes in training |
|---|---|---|
| hw01 persistent hang, hw03 syntax error in the tool's library, hw04 uninitialised register, hw06 closed known defect reappears, hw09 DSP count drops on all xc7 flows, hw10 worker died with no rerun | various | none |
| hw02 OOM kill, passes on rerun | infrastructure | 24, all infrastructure |
| hw05 stale expectation without golden | test_bug | 4, all test_bug |
| hw07 open known defect (the same pattern as harvested C07) | known_issue | 14, all known_issue |
| hw08 missing internal module only with `-flatten` | tool_defect | 2, both test_bug |
| hw11 LUT count grows only with `-dff` | tool_defect | 28, all tool_defect |
| hw12 hierarchy-probing testcase fails only with `-flatten` | test_bug | 19, all tool_defect |

So the notebook also reports accuracy on the 6 handwritten items without look-alikes (`hw, no twin`), the cleanest
out-of-distribution signal here (rules get 5 of these 6 right). On hw08 and hw12 the look-alikes in training carry
the other label, so a model that learned the training data well is expected to get them wrong; that is what makes
them hard cases.

## Base model

`Qwen/Qwen2.5-1.5B-Instruct`, revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` (checked through
`https://huggingface.co/api/models/Qwen/Qwen2.5-1.5B-Instruct`: `gated: false`, `license: apache-2.0`). Why this one:
Apache-2.0 and not gated, so no access request and no licence questions; 1.5B parameters fit a T4 in fp16 for LoRA
without bitsandbytes; text-only with a plain chat template (no "thinking" mode to switch off); good at emitting JSON;
supported by llama.cpp for the GGUF step. `Qwen2.5-3B-Instruct` is not Apache-2.0 (Qwen research licence), and the
newer `Qwen3.5` small models are multimodal, which adds complexity on a T4 for no benefit here. The smoke test uses
`Qwen/Qwen2.5-0.5B-Instruct` (same tokenizer and chat template). To try another model, change `MODEL_ID` and
`MODEL_REVISION` in the first code cell.

Training settings (first code cell): LoRA r 16, alpha 32, dropout 0.05 on all attention and MLP projections; 3 epochs;
learning rate 2e-4, cosine schedule, 5% warmup; batch 4 x 4 gradient accumulation; fp16 (a T4 has no bf16); loss on
the answer tokens only; the checkpoint with the lowest validation loss is kept; seed 42; greedy decoding for
evaluation (`repetition_penalty` 1.0).

## Run it on Kaggle (free weekly GPU quota)

1. Sign in at https://www.kaggle.com. Phone-verify the account once (Settings > Phone verification); without it
   notebooks get neither a GPU nor internet access.
2. Create > New Notebook. In the notebook: File > Import Notebook > upload `finetune/triage_lora_finetune.ipynb`.
3. Right-hand panel > Session options: Accelerator = **GPU T4 x2** (the notebook uses one of the two), Internet =
   **On**.
4. Run all (the double-arrow button, or Run > Run all). The first code cell prints the model; the install cell
   takes a minute or two; the base model downloads (about 3 GB).
5. When the last cells have printed the results table, open the Output section of the right-hand panel
   (`/kaggle/working/triage_lora`) and download `results.json` and `triage_lora_adapter.zip`. The `merged/` folder
   (about 3 GB) is only needed for the GGUF step.

## Run it on Colab

1. https://colab.research.google.com > File > Upload notebook > `finetune/triage_lora_finetune.ipynb`.
2. Runtime > Change runtime type > **T4 GPU** > Save.
3. Runtime > Run all. Keep the tab open (free sessions disconnect when idle).
4. At the end the browser downloads `results.json` and `triage_lora_adapter.zip` (allow multiple downloads if asked).
   Everything is also in `/content/triage_lora` (Files panel on the left).

Expected time on one T4: roughly 15 to 30 minutes in total. This is an estimate, not a measurement (no T4 was
available here): 309 examples x 3 epochs of about 534 tokens is about half a million training tokens; the rest is package install and model download (a few minutes), 230 short greedy generations for
the base and fine-tuned evaluations, and saving the 3 GB merged model. The notebook prints the time of every phase
and stores it in `results.json` (`seconds`).

If the training cell fails with a non-finite loss (fp16 overflow), set `DTYPE = torch.float32` in the model cell and
`fp16=False` in the training cell and run all again; it is about twice as slow.

## Bring the results back

Copy `results.json` into `finetune/results/` (keep the name, or add a suffix such as `results_kaggle_t4.json`). It
contains, for the base and the fine-tuned model and for both PoC task prompts: accuracy on all 22 items, on the 10
harvested and on the 12 handwritten items, per-class precision and recall, the confusion matrix, the number of
outputs that did not validate as `TriageDecision`, the accuracy with the PoC's rules fallback, every prediction and raw
output, the training and validation losses, timings, library versions and the SHA-256 of the embedded data. The same
file holds the rules and gpt-oss-120b rows for the same items, so the comparison is self-contained.

## Optional: run the fine-tuned model locally behind an OpenAI-compatible server

The PoC already talks to any OpenAI-compatible `/chat/completions` endpoint (`vagent/llm.py`, provider
`openai_compat`), so the fine-tuned model drops in without code changes.

1. In the notebook set `RUN_GGUF = True` (first code cell) and run the last cell, or run it after the rest. It clones
   llama.cpp at tag `b12651`, converts `merged/` to `triage-qwen2.5-1.5b-instruct-q8_0.gguf` (about 1.6 GB) and
   downloads it. q8_0 is an 8-bit quantisation: close to the fp16 model, but not identical to what the notebook
   evaluated, so score it again (step 4) before quoting it.
2. Download a llama.cpp release for Windows CPU, for example `llama-b12651-bin-win-cpu-x64.zip` from
   https://github.com/ggml-org/llama.cpp/releases/tag/b12651, and unzip it into a folder of its own.
3. Start the server (PowerShell, in that folder):
   ```powershell
   .\llama-server.exe -m <path>\triage-qwen2.5-1.5b-instruct-q8_0.gguf --port 8080 -c 4096 --jinja
   ```
4. Score it on the gold set with the PoC's own evaluation code; this writes `finetune/results/local_<name>_eval.json`
   and does not touch `runs/`:
   ```powershell
   $env:LLM_PROVIDER = "openai_compat"; $env:LLM_BASE_URL = "http://localhost:8080/v1"; $env:LLM_MODEL = "triage-qwen"
   .\.venv\Scripts\python.exe finetune\eval_local.py
   ```
5. To run the whole lifecycle with it, keep the same three variables and run
   `.\.venv\Scripts\python.exe -m vagent demo --mode llm`. Note that `demo` creates a new run, rewrites
   `runs/latest.txt` and adds drafts to `runs/tracker.sqlite`; copy those two files somewhere first if you want to
   keep your approved run as the latest. If a server rejects JSON mode, set `$env:LLM_JSON_MODE = "0"`.

On a laptop CPU a 1.5B q8_0 model needs a few seconds per triage decision; the 22-item gold set takes a few minutes.

## Regenerate the data or the notebook

```powershell
.\.venv\Scripts\python.exe finetune\make_dataset.py run      # Yosys job bank into finetune\_gen (about 100 min here on 4 workers)
.\.venv\Scripts\python.exe finetune\make_dataset.py build    # data\*.jsonl and data\dataset_report.json (under a minute)
.\.venv\Scripts\python.exe finetune\build_notebook.py        # triage_lora_finetune.ipynb
```

`run` is resumable (finished jobs are skipped). It uses at most 4 workers and a 600 s per-job timeout so that CPU
contention cannot create timeouts that look like infrastructure failures; only the deliberately injected hangs use
the PoC's default 150 s, because they sleep until killed and the timeout appears in the failure message.

## What was verified locally

No GPU was available, so the notebook was run on the CPU in a separate venv (`finetune/.venv-smoke`, Python 3.14.4,
CPU torch 2.14.1 and the pinned transformers 5.22.2, peft 0.21.3, trl 1.17.1, datasets 5.1.0, accelerate 1.15.0;
the project venv and `requirements.txt` are unchanged) with
`jupyter nbconvert --execute` and `TRIAGE_SMOKE=1`, which only swaps in `Qwen/Qwen2.5-0.5B-Instruct`, 12 training
examples, 3 optimiser steps (batch 1 x 2), 4 test items (2 harvested, 2 handwritten) and 2 validation items. Every
other line is the code that runs on the T4.

Verified on the CPU: the pin check, decoding the embedded data, the schema check (the prompts contain exactly the
`TriageDecision` schema), the token-length check, base-model evaluation with both PoC task strings, LoRA training with
TRL (`SFTTrainer`, prompt-completion data, loss on the answer only, evaluation, checkpoint, best-checkpoint reload),
saving the adapter, merging, saving the merged model, evaluating the merged model, writing `results.json`, zipping the
adapter, and the GGUF cell (llama.cpp `b12651` converter, q8_0, 525 MB; rope base 1,000,000 and the chat template are
in its metadata). The final run of the shipped notebook (its code cells are byte-identical to the file in this folder)
finished every cell without an error in 14 minutes on the CPU plus 78 s for the GGUF. All 16 test generations parsed
as `TriageDecision`; the smoke accuracy numbers mean nothing (3 steps on 0.5B). An earlier run had found two problems
in the GGUF cell (the converter needs `sentencepiece` for Qwen; installing `gguf-py` with pip was unnecessary and
failed on Windows), both fixed before the final run.

Local serving: `llama-server.exe` from the `b12651` Windows CPU release served the smoke GGUF, and
`finetune/eval_local.py` scored all 22 gold items through the PoC's unchanged `openai_compat` client in JSON mode:
22 of 22 outputs validated as `TriageDecision`, 0 fallbacks to rules, 62 seconds.

Not verified: anything that needs a GPU (fp16 mixed precision and its loss scaling, T4 memory, real run time), the
1.5B model itself, a full 3-epoch run, the Kaggle and Colab images (their preinstalled torch and how pip resolves the
pins against them; locally the pins and `sentencepiece` were already installed, so the two `pip install` branches did
not run), the Kaggle and Colab UI steps and downloads, and of course the accuracy the fine-tuned model will reach.

## Caveats

- The faults are synthetic. Tool defects are injected by `vagent/toolshim.py` into Yosys output, testcase bugs are
  deliberate mutations, infrastructure faults are emulated. A model that learns them has learned this PoC's fault
  model, not real synthesis bugs.
- The test set is small: 22 items, so one item is 4.5 points of accuracy overall, 10 points on the 10 harvested items
  and 8.3 points on the 12 handwritten items. Differences of one or two items are noise.
- Single seed, single training run, greedy decoding. A second run with another seed can move the numbers by an item or
  two; the PoC's own gpt-oss-120b runs moved from 0.682 to 0.727 between two runs at temperature 0.
- The harvested items were produced by an earlier version of the triage code (before the review fixes) and differ
  slightly from what the current code produces (for example, a flow that passed on rerun is now listed in
  `flows_passed`); the training data comes from the current code.
- The handwritten items are the honest headline but they are not a clean out-of-distribution set either: the person
  who wrote the generator had read them. No fault family was added to imitate them; `dataset_report.json` lists, for
  every gold item, how many training examples share its exact structural pattern or its decision-relevant pattern
  ("structural" and "pattern" twins, table above) so you can see which hard cases have look-alikes in training. Two
  generator choices do create look-alikes: some testcases have no golden (which gives hw05's pattern) and some sampled
  regressions leave out the baseline flow (which makes a testcase bug look option-triggered, as in hw08 and hw12).
  Both happen in real regressions, so they were kept and are disclosed here rather than removed.
- The rationale and component in the training targets are template text written from the evidence and the true
  label, and the confidence is the share of that label among training examples with the same structural pattern.
  Only the category is scored.
- The gpt-oss-120b rows are copied from the PoC's earlier runs (same gold set, same eval prompt, via Groq), not re-run
  in the notebook.
