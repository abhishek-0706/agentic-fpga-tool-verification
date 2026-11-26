import { initLive, selectJob } from "./live.js";
import { KIND_LABEL, badge, esc, famTable } from "./ui.js";

const $ = (s) => document.querySelector(s);
const pct = (x) => (x == null ? "-" : x.toFixed(3));
const REG_FLOWS = ["generic", "xc7", "xc7_flatten", "xc7_dff", "xc7_flat_noio"];

const STAGES = [
  ["1", "Spec to test plan", "Markdown feature spec to a validated plan of flows, features and expectations.", "1_spec_to_plan"],
  ["2", "Test generation", "Parameterised Verilog generators plus a legacy suite; every design must pass a Yosys parse and elaborate gate.", "2_test_generation"],
  ["3", "Regression runner", "Designs x flows, one process per job, timeouts and parallel workers.", "3_4_execute_and_oracles"],
  ["4", "Oracles", "SAT equivalence of RTL against the netlist, QoR expectations and golden drift.", null],
  ["5", "Fault injection", "Option-triggered tool defects, broken legacy tests and infrastructure faults, each with a hidden label.", null],
  ["6", "Triage", "Normalise, sign, cluster, compare flows, re-run suspects, match known defects, classify, delta-reduce.", "6_triage"],
  ["7", "Defect drafts and approval", "SQLite tracker with de-duplication; a person approves before anything is filed.", "7_defect_management", "human"],
  ["8", "Fix verification", "Re-run reproducers and affected jobs on the fixed build; close only what passes.", "8_fix_verification"],
  ["9", "Coverage", "Feature by flow matrix, gaps and gated test proposals.", "9_coverage"],
  ["10", "Optional LLM", "Gemini, Anthropic or any OpenAI-compatible model; schema-validated, rules fallback.", null, "opt"],
  ["11", "Evaluation", "Accuracy on live ground truth and on a labelled gold set.", "11_evaluation"],
  ["12", "Report and MCP", "HTML report, metrics and event log; MCP server exposes the stages as tools.", null],
];

function route() {
  const id = (location.hash || "#overview").slice(1).split("/")[0];
  const known = ["overview", "live", "regression", "triage", "coverage", "evaluation"];
  const view = known.includes(id) ? id : "overview";
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("active", s.id === `view-${view}`));
  document.querySelectorAll("nav.tabs a").forEach((a) => {
    if (a.getAttribute("href") === `#${view}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function overview(d) {
  const m = d.metrics, ev = d.evaluation;
  const closed = d.fixverify.defects.filter((x) => x.closed).length;
  const kpis = [
    [m.jobs, "regression jobs (13 designs x 5 flows)"],
    [`${m.failed} → ${m.clusters}`, "failures grouped into clusters"],
    [d.defects.length, "defect drafts, each approved by a person"],
    [`${closed} / ${d.fixverify.defects.length}`, "tool defects closed by fix verification"],
    [ev.rules.escapes.length, "escapes: injected defects no oracle flagged"],
    [`${pct(ev.rules.gold_rules.accuracy)} / ${pct(ev.llm?.gold_llm?.accuracy)}`, "gold-set triage accuracy: rules / LLM"],
  ];
  $("#kpis").innerHTML = kpis.map(([v, l]) => `<div class="card kpi"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("");
  $("#pipeline").innerHTML = STAGES.map(([n, t, desc, key, cls]) => {
    const s = key && m.stage_seconds[key] != null ? `${m.stage_seconds[key]}s` : "";
    return `<div class="stage ${cls || ""}"><div class="n">STAGE ${n}</div><div class="t">${esc(t)}</div><div class="d">${esc(desc)}</div><div class="s">${esc(s)}</div></div>`;
  }).join("");
}

function cellClass(r) {
  if (r.status === "pass") return "pass";
  if (["qor_golden", "qor_expect"].includes(r.kind)) return "qor";
  if (["parse_error", "elab_error"].includes(r.kind)) return "tb";
  if (r.ground_truth.some((g) => g.detail.startsWith("KDB"))) return "kdb";
  return "fail";
}

function regression(d) {
  const byId = Object.fromEntries(d.results.map((r) => [r.job_id, r]));
  $("#legend").innerHTML = [
    ["pass", "pass (proven or bounded equivalence)"], ["fail", "not equivalent or crash"], ["test_bug", "QoR drift or design does not parse"],
    ["known_issue", "known open defect"],
  ].map(([c, l]) => `${badge(c, c === "pass" ? "pass" : c === "fail" ? "fail" : c === "test_bug" ? "QoR / parse" : "known")} ${esc(l)}`).join("");
  const head = `<tr><th>Design</th>${REG_FLOWS.map((f) => `<th>${esc(f)}</th>`).join("")}</tr>`;
  const rows = d.testcases.map((tc) => `<tr><th class="rowh" scope="row">${esc(tc.name.replace(/^tc_/, ""))}${tc.origin === "legacy" ? ' <span class="tag">legacy</span>' : ""}</th>${REG_FLOWS.map((f) => {
    const r = byId[`${tc.name}__${f}`];
    if (!r) return "<td></td>";
    const label = r.status === "pass" ? r.equiv : KIND_LABEL[r.kind] || r.kind;
    const extra = r.first_attempt ? "re-run passed" : r.status === "pass" ? "pass" : "fail";
    return `<td><button class="cellbtn ${cellClass(r)}" data-job="${esc(r.job_id)}" aria-pressed="false">${esc(label)}<small>${esc(extra)}</small></button></td>`;
  }).join("")}</tr>`).join("");
  $("#matrix").innerHTML = head + rows;
  $("#matrix").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-job]");
    if (!b) return;
    document.querySelectorAll("#matrix .cellbtn").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    jobDetail(d, byId[b.dataset.job]);
  });
}

function jobDetail(d, r) {
  const tc = d.testcases.find((t) => t.name === r.testcase);
  const flow = d.flows.find((f) => f.id === r.flow);
  const gt = r.ground_truth.map((g) => g.detail);
  $("#job-detail").innerHTML = `
    <div class="row" style="justify-content:space-between"><h3 class="mono" style="margin:0">${esc(r.job_id)}</h3>
      <button class="btn" id="run-live" type="button">Run this job live</button></div>
    <dl class="kv">
      <dt>Flow</dt><dd><code>${esc(flow.command)}</code></dd>
      <dt>Status</dt><dd>${badge(r.status)} ${r.kind ? badge(r.kind, KIND_LABEL[r.kind] || r.kind) : ""}</dd>
      <dt>Equivalence</dt><dd>${badge(r.equiv)} ${esc(r.equiv_note)}</dd>
      ${r.message ? `<dt>Message</dt><dd>${esc(r.message)}</dd>` : ""}
      ${r.first_attempt ? `<dt>First attempt</dt><dd>${badge(r.first_attempt.kind)} ${esc(r.first_attempt.message)} (re-run by triage, then passed)</dd>` : ""}
      <dt>Golden match</dt><dd>${r.golden_match == null ? "no golden" : r.golden_match ? "yes" : "no"}</dd>
      <dt>Duration</dt><dd>${esc(r.duration_s)} s</dd>
    </dl>
    ${Object.keys(r.families || {}).length ? famTable(r.families, null) : ""}
    ${r.excerpt ? `<details open><summary>Log excerpt</summary><pre class="code">${esc(r.excerpt)}</pre></details>` : ""}
    <details><summary>Design source (${esc(tc.top)})</summary><pre class="code">${esc(tc.source)}</pre></details>
    <details><summary>Ground truth (hidden from triage, used only by evaluation)</summary>
      <p style="margin-top:6px">${gt.length ? gt.map((g) => badge("info", g)).join(" ") : "Nothing injected: a clean job."}</p></details>`;
  $("#run-live").addEventListener("click", () => { location.hash = "#live"; selectJob(r.testcase, r.flow, "buggy"); });
}

function triage(d) {
  const defects = Object.fromEntries(d.defects.map((x) => [x.id, x]));
  $("#clusters").innerHTML = d.clusters.map((c) => {
    const e = c.evidence, dec = c.decision || {}, rep = c.reproducer;
    return `<article class="card cluster">
      <header><span class="id">${esc(c.id)}</span>${badge(dec.category, dec.category?.replace("_", " "))}
        ${badge(c.decided_by, `decided by ${c.decided_by}`)} <span class="note">${esc(dec.component || "")}</span>
        ${c.defect_id ? `<span class="tag">${esc(c.defect_id)} ${esc(defects[c.defect_id]?.status || "")}</span>` : ""}</header>
      <p style="margin-bottom:6px">${esc(dec.rationale)}</p>
      <dl class="kv">
        <dt>Signature</dt><dd><code>${esc(e.signature)}</code> ${badge(e.kind, KIND_LABEL[e.kind] || e.kind)} ${esc(e.message)}</dd>
        <dt>Fails on</dt><dd>${e.flows_failed.map((f) => `<span class="tag">${esc(f)}</span>`).join(" ")}</dd>
        <dt>Passes on</dt><dd>${e.flows_passed.length ? e.flows_passed.map((f) => `<span class="tag">${esc(f)}</span>`).join(" ") : "none"}</dd>
        ${e.fingerprint.length ? `<dt>Trigger</dt><dd><code>${esc(e.fingerprint.join(" "))}</code></dd>` : ""}
        <dt>Jobs</dt><dd>${c.members.length}: ${c.members.map((m) => `<span class="tag">${esc(m)}</span>`).join(" ")}</dd>
        ${e.rerun && e.rerun !== "not_run" ? `<dt>Re-run</dt><dd>${esc(e.rerun)}</dd>` : ""}
        ${e.known_match ? `<dt>Known defect</dt><dd>${esc(e.known_match)}</dd>` : ""}
        ${rep ? `<dt>Minimal reproducer</dt><dd><code>${esc(rep.flow_command)}</code>, ${esc(rep.design_lines)} design lines, options removed: ${esc(rep.removed_options.join(" ") || "none")} (${esc(rep.trials)} trials)</dd>` : ""}
      </dl>
      <details><summary>Evidence excerpt</summary><pre class="code">${esc(e.excerpt)}</pre></details>
    </article>`;
  }).join("");

  const delta = d.fixverify.delta;
  $("#fixverify").innerHTML = [["fixed", delta.fixed.length], ["still failing", delta.still_failing.length],
    ["new failures", delta.new_failures.length], ["still passing", delta.still_passing.length]]
    .map(([l, v]) => `<div class="card kpi"><div class="v">${v}</div><div class="l">${esc(l)} on the fixed build</div></div>`).join("");
  $("#defects").innerHTML = `<tr><th>ID</th><th>Title</th><th>Severity</th><th>Category</th><th>Status</th><th>Approved by</th></tr>`
    + d.defects.map((x) => {
      const b = x.body || {};
      return `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.title)}
        <details><summary>Report</summary><dl class="kv"><dt>Summary</dt><dd>${esc(b.summary)}</dd><dt>Expected</dt><dd>${esc(b.expected)}</dd>
        <dt>Actual</dt><dd><pre class="code">${esc(b.actual)}</pre></dd><dt>Reproduce</dt><dd>${(b.repro_steps || []).map((s) => `<div><code>${esc(s)}</code></div>`).join("")}</dd></dl></details></td>
        <td>${badge(x.severity)}</td><td>${badge(x.category, x.category.replace("_", " "))}</td><td>${badge(x.status)}</td><td>${esc(x.approved_by || "-")}</td></tr>`;
    }).join("");
  $("#audit").innerHTML = `<tr><th>Time (UTC)</th><th>Defect</th><th>Action</th><th>Actor</th></tr>`
    + d.audit.map((a) => `<tr><td class="mono">${esc(a.ts.replace("T", " ").slice(0, 19))}</td><td class="mono">${esc(a.defect_id)}</td><td>${esc(a.action.replaceAll("_", " "))}</td><td>${esc(a.actor)}</td></tr>`).join("");
}

function coverage(d) {
  const mx = d.coverage.matrix;
  $("#cov-lead").textContent = `${mx.verified} of ${mx.required} required feature and flow combinations verified (${mx.pct_verified}%). Failing cells are the injected defects; not-run cells are flows the spec declares but the regression does not run yet.`;
  $("#cov-matrix").innerHTML = `<tr><th>Feature</th>${mx.flows.map((f) => `<th>${esc(f)}</th>`).join("")}</tr>`
    + Object.entries(mx.cells).map(([feat, row]) => `<tr><th class="rowh" scope="row" title="${esc(mx.features[feat])}">${esc(feat.replace("FEAT-", ""))}</th>${mx.flows.map((f) => {
      const c = row[f];
      const cls = c.status === "verified" ? "pass" : c.status === "fail" ? "fail" : "";
      return `<td><span class="cellbtn ${cls}" style="display:block;cursor:default">${esc(c.status.replace("_", " "))}<small>${c.required ? "required" : "optional"}</small></span></td>`;
    }).join("")}</tr>`).join("");
  $("#gaps").innerHTML = d.coverage.gaps.map((g) => `<div style="margin-bottom:6px">${badge(g.type === "fail" ? "fail" : "idle", g.type.replaceAll("_", " "))} <span class="mono">${esc(g.feature || g.flow)}${g.feature && g.flow ? " x " + esc(g.flow) : ""}</span> <span class="note">${esc(g.detail)}</span></div>`).join("");
  $("#proposals").innerHTML = d.coverage.proposals.map((p) => `<div style="margin-bottom:12px"><div>${badge("info", p.feature)} ${badge(p.admitted ? "pass" : "idle", p.admitted ? `gate: ${p.gate}` : p.status)}</div>
    <p class="note" style="margin:4px 0">${esc(p.rationale)}</p>${p.verilog ? `<details><summary>Proposed Verilog (${esc(p.top)}), ${esc(p.status)}</summary><pre class="code">${esc(p.verilog)}</pre></details>` : ""}</div>`).join("");
}

function evalTable(title, e) {
  if (!e) return "";
  const cls = Object.keys(e.per_class);
  return `<div class="card"><h3>${esc(title)}</h3><p class="note">n = ${e.n}, accuracy ${pct(e.accuracy)}</p>
    <div class="scroll"><table><tr><th>Class</th><th class="num">Support</th><th class="num">Precision</th><th class="num">Recall</th></tr>
    ${cls.map((c) => { const p = e.per_class[c]; return `<tr><td>${esc(c.replace("_", " "))}</td><td class="num">${p.support}</td><td class="num">${pct(p.precision)}</td><td class="num">${pct(p.recall)}</td></tr>`; }).join("")}</table></div>
    <details><summary>Confusion matrix (rows: truth, columns: prediction)</summary><div class="scroll" style="margin-top:6px"><table>
    <tr><th></th>${cls.map((c) => `<th class="num">${esc(c.split("_")[0])}</th>`).join("")}</tr>
    ${cls.map((t) => `<tr><td>${esc(t.replace("_", " "))}</td>${cls.map((p) => `<td class="num">${e.confusion[t][p]}</td>`).join("")}</tr>`).join("")}</table></div></details></div>`;
}

function evaluation(d) {
  const r = d.evaluation.rules, l = d.evaluation.llm;
  $("#eval-kpis").innerHTML = [
    [pct(r.live_rules.accuracy), `rules, live run (n=${r.live_rules.n})`], [pct(r.gold_rules.accuracy), `rules, gold set (n=${r.gold_rules.n})`],
    [pct(l?.live_llm?.accuracy), "LLM, live run"], [pct(l?.gold_llm?.accuracy), "LLM, gold set"],
    [pct(r.gold_rules.by_source?.handwritten), "rules on the 12 handwritten hard cases"],
  ].map(([v, lab]) => `<div class="card kpi"><div class="v">${esc(v)}</div><div class="l">${esc(lab)}</div></div>`).join("");
  $("#eval-tables").innerHTML = evalTable("Rules, gold set", r.gold_rules) + evalTable("LLM, gold set", l?.gold_llm);
  $("#eval-note").innerHTML = `Rules results come from run <code>${esc(d.run.id)}</code>. LLM results come from run <code>${esc(l?.run_id || "-")}</code>
    with <code>openai/gpt-oss-120b</code> on Groq's free tier at temperature 0. On this data the rules are more accurate than the LLM, so rules stay
    the default; the LLM's value here is readable defect prose and test proposals, both behind validation and human approval. The gold set
    is small and written by the same author as the injected faults: these numbers catch regressions, they are not a benchmark.`;
}

async function main() {
  window.addEventListener("hashchange", route);
  route();
  const d = await (await fetch("data/run.json")).json();
  $("#run-id").textContent = d.run.id;
  $("#tool").textContent = d.run.tool;
  overview(d);
  regression(d);
  triage(d);
  coverage(d);
  evaluation(d);
  initLive(d);
}

main().catch((e) => {
  document.querySelector("main").insertAdjacentHTML("afterbegin", `<div class="callout">Could not load the run data: ${esc(e.message)}</div>`);
});
