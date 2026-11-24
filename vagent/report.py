"""Stage 12: one self-contained static HTML report per run."""
from __future__ import annotations

import json
from html import escape as e
from pathlib import Path

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--mute:#5f6368;--line:#dcdcd8;--card:#fff;
--ok:#1e7b4a;--okbg:#dff3e6;--warn:#8a5a00;--warnbg:#fbefd2;--bad:#a1261c;--badbg:#f8dcd8;--na:#6b6b6b;--nabg:#ececea}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#e8e8ea;--mute:#a0a0a8;--line:#34343a;--card:#1c1c20;
--ok:#7fd8a4;--okbg:#173524;--warn:#f0c56a;--warnbg:#3a2e12;--bad:#ff9d93;--badbg:#401c19;--na:#a8a8a8;--nabg:#2a2a2e}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1180px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.mute{color:var(--mute)}.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:22px}.tile span{color:var(--mute);font-size:12px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top;font-size:13px}
th{font-weight:600}code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre{white-space:pre-wrap;margin:0;max-height:160px;overflow:auto}
.s-verified,.c-pass{background:var(--okbg);color:var(--ok)}.s-qor_only{background:var(--warnbg);color:var(--warn)}
.s-fail,.c-fail{background:var(--badbg);color:var(--bad)}.s-not_run{background:var(--nabg);color:var(--na)}
.hm td{text-align:center;font-size:12px}.hm td.req0{opacity:.55}.legend span{display:inline-block;padding:2px 8px;margin-right:6px;border-radius:4px}
"""


def _table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    h = "".join(f"<th>{e(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def _load(d: Path, name: str):
    p = d / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def render(agent, m: dict, tracker) -> Path:
    d = agent.dir
    clusters = _load(d, "clusters.json") or []
    cov = _load(d, "coverage.json") or {}
    fix = _load(d, "fixverify.json") or {}
    ev = _load(d, "eval.json") or {}
    rules_acc = ev.get("live_rules", {}).get("accuracy", "n/a")
    acc = f"{rules_acc} rules, {ev['live_llm']['accuracy']} LLM" if ev.get("live_llm") else rules_acc
    tiles = [("jobs", m["jobs"]), ("pass rate", f"{m['pass_rate'] * 100:.1f}%"), ("failures", m["failed"]),
             ("clusters", m["clusters"]), ("defects filed", m["defects"]["filed"] + m["defects"]["closed"]),
             ("defects closed", m["defects"]["closed"]),
             ("coverage verified", f"{cov.get('matrix', {}).get('pct_verified', 'n/a')}%"),
             ("triage accuracy (live)", acc)]
    parts = [f"<h1>Agentic FPGA tool verification: run {e(m['run_id'])}</h1>",
             f"<div class='mute'>Tool under test: {e(m['tool'])} (open-source analogue; emulated builds "
             f"<code>buggy</code>/<code>fixed</code>). Profile {e(m['profile'])}, reasoning mode "
             f"<b>{e(m['mode'])}</b>.</div>",
             "<div class='tiles'>" + "".join(f"<div class='tile'><b>{e(str(v))}</b><span>{e(k)}</span></div>"
                                             for k, v in tiles) + "</div>"]

    res = agent.results()
    flows = sorted({r.flow for r in res}, key=lambda f: (f != "generic", f))
    grid = {(r.testcase, r.flow): r for r in res}
    rows = []
    for tc in sorted({r.testcase for r in res}):
        row = [f"<b>{e(tc)}</b>"]
        for fl in flows:
            r = grid.get((tc, fl))
            if r is None:
                row.append("-")
            elif r.status == "pass":
                row.append(f"<span class='c-pass' style='display:block;padding:3px'>pass ({e(r.equiv)})</span>")
            else:
                row.append(f"<span class='c-fail' style='display:block;padding:3px'>FAIL {e(r.kind or '')}</span>")
        rows.append(row)
    parts.append("<h2>Regression results (build=buggy, first attempt)</h2>")
    parts.append(_table(["testcase"] + flows, rows, "hm"))

    parts.append("<h2>Stage timings and run metrics</h2>")
    parts.append(_table(["stage", "seconds"], [[e(k), f"{v}"] for k, v in sorted(m["stage_seconds"].items())]))
    parts.append(f"<p class='mute'>Failure kinds: {e(json.dumps(m['failure_kinds']))}. Equivalence verdicts: "
                 f"{e(json.dumps(m['equiv_verdicts']))}. LLM counters: {e(json.dumps(m['llm']) or '{}')}.</p>")

    parts.append("<h2>Failure clusters (triage)</h2>")
    rows = []
    for c in clusters:
        evd, dec, rep = c["evidence"], c["decision"], c.get("reproducer")
        rrow = (f"{e(rep['flow_command'])}<br><span class='mute'>removed {e(', '.join(rep['removed_options']) or 'none')}; "
                f"params {e(json.dumps(rep['params']))}; {rep['trials']} trials</span>") if rep else "-"
        rows.append([f"<b>{e(c['id'])}</b><br><code>{e(evd['signature'])}</code>", e(evd["kind"]),
                     f"<b>{e(dec['category'])}</b> ({dec['confidence']:.2f}, {e(c['decided_by'])})<br>"
                     f"<span class='mute'>{e(dec['component'])}</span>",
                     f"{len(c['members'])} jobs<br>{e(', '.join(evd['testcases']))}",
                     e(", ".join(evd["fingerprint"]) or "-"), e(evd["message"]) + f"<br><span class='mute'>"
                     f"{e(dec['rationale'])}</span>", rrow, e(c.get("defect_id") or evd.get("known_match") or "-")])
    parts.append(_table(["cluster", "kind", "classification", "members", "trigger options", "evidence",
                         "minimal reproducer", "defect"], rows))

    parts.append("<h2>Defects (tracker)</h2>")
    rows = [[e(x["id"]), e(x["status"]), e(x["severity"]), e(x["category"]), e(x["title"]),
             e(x.get("approved_by") or "-"), str(x["occurrences"])] for x in tracker.all()]
    parts.append(_table(["id", "status", "sev", "category", "title", "approved by", "seen"], rows))

    parts.append("<h2>Fix verification (build=fixed)</h2>")
    if fix.get("defects"):
        dl = fix["delta"]
        parts.append(f"<p>Fixed {len(dl['fixed'])}, still failing {len(dl['still_failing'])}, new failures "
                     f"{len(dl['new_failures'])}, still passing {len(dl['still_passing'])}.</p>")
        parts.append(_table(["defect", "title", "reproducer passes", "affected pass", "closed"],
                            [[e(x["id"]), e(x["title"]), str(x["reproducer_passes"]), str(x["affected_pass"]),
                              str(x["closed"])] for x in fix["defects"]]))
        if dl["still_failing"] or dl["new_failures"]:
            parts.append(f"<p class='mute'>Still failing: {e(', '.join(dl['still_failing']) or '-')}. New failures: "
                         f"{e(', '.join(dl['new_failures']) or '-')}</p>")
    else:
        parts.append("<p class='mute'>No filed defects were verified in this run (drafts need human approval).</p>")

    if cov:
        mx = cov["matrix"]
        parts.append(f"<h2>Coverage: feature x flow ({mx['verified']}/{mx['required']} required cells formally "
                     f"verified, {mx['pct_verified']}%)</h2>")
        parts.append("<div class='legend'><span class='s-verified'>verified</span><span class='s-qor_only'>QoR only"
                     "</span><span class='s-fail'>fail</span><span class='s-not_run'>not run</span> faded = not "
                     "required by spec</div>")
        rows = []
        for fid, row in mx["cells"].items():
            tds = [f"<b>{e(fid)}</b><br><span class='mute'>{e(mx['features'][fid][:48])}</span>"]
            for fl in mx["flows"]:
                c = row[fl]
                tds.append(f"<span class='s-{c['status']}' style='display:block;padding:4px'>"
                           f"{e(c['status'].replace('_', ' '))}</span>" if c["required"] else
                           f"<span class='s-{c['status']}' style='display:block;padding:4px;opacity:.5'>"
                           f"{e(c['status'].replace('_', ' '))}</span>")
            rows.append(tds)
        parts.append(_table(["feature"] + mx["flows"], rows, "hm"))
        parts.append("<h2>Coverage gaps and proposed next tests</h2>")
        parts.append(_table(["feature", "flow", "gap", "detail"],
                            [[e(g.get("feature", "-")), e(g.get("flow", "-")), e(g["type"]), e(g["detail"])]
                             for g in cov["gaps"]]))
        parts.append(_table(["feature", "source", "gate", "status", "rationale", "design"],
                            [[e(p["feature"]), e(p["source"]), e(str(p.get("admitted"))), e(p.get("status", "")),
                              e(p.get("rationale", "")), f"<pre>{e(p.get('verilog', ''))}</pre>"] for p in cov["proposals"]]))

    if ev:
        parts.append("<h2>Triage evaluation</h2>")
        for key, title in (("live_rules", "Live run, job level, rules mode (labels from fault injection)"),
                           ("gold_rules", "Gold set, rules mode"), ("live_llm", "Live run, LLM mode"),
                           ("gold_llm", "Gold set, LLM mode")):
            r = ev.get(key)
            if not r:
                continue
            parts.append(f"<p><b>{e(title)}</b>: accuracy {r['accuracy']} on n={r['n']}</p>")
            parts.append(_table(["class", "support", "precision", "recall", "F1"],
                                [[e(c), str(v["support"]), str(v["precision"]), str(v["recall"]), str(v["f1"])]
                                 for c, v in r["per_class"].items()]))
        parts.append(f"<p class='mute'>LLM mode: {e(str(ev.get('llm') or 'run'))}. Escapes (injected tool defect "
                     f"not flagged by any oracle): {e(', '.join(ev.get('escapes', [])) or 'none')}.</p>")

    html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Verification Run Report</title><style>{CSS}</style>"
            f"</head><body><main>{''.join(parts)}</main></body></html>")
    out = d / "report.html"
    out.write_text(html, encoding="utf-8")
    return out
