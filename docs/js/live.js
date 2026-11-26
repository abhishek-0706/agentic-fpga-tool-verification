import { DEFECTS, flowInfo, goldenKey, runJob } from "./pipeline.js";
import { KIND_LABEL, badge, esc, famTable } from "./ui.js";

const $ = (s) => document.querySelector(s);
const PRESETS = [
  ["Multiplier with -flatten", "tc_mul_reg", "xc7_flatten"],
  ["State machine with -dff", "tc_fsm_seqdet", "xc7_dff"],
  ["Distributed RAM with -noiopad", "tc_lutram", "xc7_noiopad"],
  ["Adder with -flatten (QoR)", "tc_addsub", "xc7_flatten"],
  ["Known issue in generic synth", "tc_param_hier", "generic"],
  ["Broken legacy test", "tc_legacy_cmp4", "xc7"],
  ["Clean job", "tc_comb_mux", "xc7"],
];
let data, worker, seq = 0, loaded = false, busy = false;
const pending = new Map();
const state = { tc: null, flow: null, build: "buggy" };

function setStep(id, cls, html) {
  const li = $(id);
  li.className = cls || "";
  if (html !== undefined) li.querySelector(".body").innerHTML = html;
}

function getWorker() {
  if (worker) return worker;
  worker = new Worker(new URL("./yosys-worker.js", import.meta.url), { type: "module" });
  worker.onmessage = ({ data: m }) => {
    const p = pending.get(m.id);
    if (!p) return;
    if (m.progress) {
      const { done, total } = m.progress;
      const pctDone = total ? Math.round((100 * done) / total) : 0;
      setStep("#st-load", "run", `Downloading Yosys: ${(done / 1e6).toFixed(1)} of ${(total / 1e6).toFixed(1)} MB<div class="bar"><i style="width:${pctDone}%"></i></div>`);
      return;
    }
    pending.delete(m.id);
    if (!loaded) { loaded = true; setStep("#st-load", "ok", "Yosys 0.71 is running in your browser."); }
    if (m.error) p.reject(new Error(m.error));
    else p.resolve({ code: m.code, files: m.files });
  };
  worker.onerror = (e) => {
    for (const p of pending.values()) p.reject(new Error(e.message || "the Yosys worker could not start (module workers need a current browser)"));
    pending.clear();
    worker = null;
  };
  return worker;
}

function runYosys(args, files) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    getWorker().postMessage({ id, args, files });
  });
}

const tcByName = (n) => data.testcases.find((t) => t.name === n);
const flowById = (id) => data.flows.find((f) => f.id === id);

function renderMeta() {
  const tc = state.tc;
  const feats = tc.features.map((f) => data.features.find((x) => x.id === f)?.title || f);
  $("#tc-meta").innerHTML = `${esc(feats.join("; "))}${tc.origin === "legacy" ? " · legacy test" : ""} · top <code>${esc(tc.top)}</code>`
    + (tc.sequential ? ` · sequential, ${tc.depth}-cycle bounded proof` : " · combinational, full proof");
  const edited = $("#src").value !== tc.source;
  $("#src-note").textContent = edited
    ? `Edited design: the golden reference is not used. Keep the top module named ${tc.top}.`
    : "";
}

export function selectJob(tcName, flowId, build = state.build) {
  state.tc = tcByName(tcName);
  state.flow = flowById(flowId);
  $("#sel-tc").value = tcName;
  $("#sel-flow").value = flowId;
  $("#src").value = state.tc.source;
  setBuild(build);
  renderMeta();
}

function setBuild(b) {
  state.build = b;
  $("#b-buggy").setAttribute("aria-pressed", String(b === "buggy"));
  $("#b-fixed").setAttribute("aria-pressed", String(b === "fixed"));
}

function timer(id, label) {
  const t0 = performance.now();
  const h = setInterval(() => setStep(id, "run", `${esc(label)} ${((performance.now() - t0) / 1000).toFixed(0)} s`), 500);
  return () => clearInterval(h);
}

async function run() {
  if (busy) return;
  busy = true;
  $("#run").disabled = true;
  $("#verdict").innerHTML = "";
  const src = $("#src").value;
  const edited = src !== state.tc.source;
  const tc = { ...state.tc, source: src };
  const flow = flowInfo(state.flow);
  const golden = edited ? null : data.golden[goldenKey(tc.name, flow.command)] ?? null;
  for (const s of ["#st-synth", "#st-qor", "#st-equiv"]) setStep(s, "", "");
  if (!loaded) setStep("#st-load", "run", "Starting the Yosys worker...");
  let stop = () => {};
  try {
    const res = await runJob(runYosys, tc, flow, state.build, golden, (stage) => {
      stop();
      if (stage === "synth") stop = timer("#st-synth", `Running ${flow.command} -top ${tc.top}...`);
      if (stage === "equiv") stop = timer("#st-equiv", "Proving the netlist against the RTL with SAT...");
    });
    stop();
    render(res, tc, flow, golden, edited);
  } catch (e) {
    stop();
    setStep(loaded ? "#st-synth" : "#st-load", "bad", `Could not run Yosys: ${esc(e.message)}`);
  } finally {
    busy = false;
    $("#run").disabled = false;
  }
}

function counterexample(log) {
  const ls = (log || "").split(/\r?\n/);
  const start = ls.findIndex((l) => /^\s+Time\s+Signal Name/.test(l));
  if (start < 0) return "";
  const end = ls.findIndex((l, i) => i > start && !l.trim());
  return ls.slice(start, end < 0 ? undefined : end).join("\n");
}

function render(res, tc, flow, golden, edited) {
  const synthOk = !res.kind || !["parse_error", "elab_error", "crash"].includes(res.kind);
  const logTail = res.synth_log.split(/\r?\n/).slice(-14).join("\n");
  setStep("#st-synth", synthOk ? "ok" : "bad", `
    ${synthOk ? "Netlist written." : `${badge(res.kind, KIND_LABEL[res.kind])} ${esc(res.message)}`}
    <details><summary>Script</summary><pre class="code">${esc(res.synth_script)}</pre></details>
    <details ${synthOk ? "" : "open"}><summary>${synthOk ? "End of synth.log" : "Log excerpt"}</summary><pre class="code">${esc(synthOk ? logTail : res.excerpt)}</pre></details>`);
  if (!synthOk) {
    setStep("#st-qor", "", '<span class="note">Skipped: no netlist.</span>');
    setStep("#st-equiv", "", '<span class="note">Skipped: no netlist.</span>');
  } else {
    const qorBad = res.drift?.length || res.qor_failures.length;
    setStep("#st-qor", qorBad ? "bad" : "ok", `
      ${res.drift?.length ? `<p>${badge("qor_golden", "golden drift")} ${esc(res.drift.join("; "))}</p>` : ""}
      ${res.qor_failures.length ? `<p>${badge("qor_expect", "expectation")} ${esc(res.qor_failures.join("; "))}</p>` : ""}
      ${!qorBad ? `<p>${golden ? "Primitive counts match the golden reference build." : "No golden reference for this job."}${(tc.expect[flow.family] || []).length ? ` Expectations met: ${esc(tc.expect[flow.family].join(", "))}.` : ""}</p>` : ""}
      ${famTable(res.families, golden)}`);
    const eqCls = ["proven", "bounded"].includes(res.equiv) ? "ok" : res.equiv === "fail" ? "bad" : "";
    setStep("#st-equiv", eqCls, `${badge(res.equiv)} ${esc(res.equiv_note)}
      ${res.equiv_script ? `<details><summary>Equivalence script</summary><pre class="code">${esc(res.equiv_script)}</pre></details>` : ""}
      ${res.equiv === "fail" ? `<details open><summary>Counterexample: values per cycle (gold is the RTL, gate is the netlist)</summary><pre class="code">${esc(counterexample(res.equiv_log) || res.excerpt)}</pre></details>` : ""}`);
  }
  const recorded = !edited && state.build === "buggy" ? data.results.find((r) => r.job_id === `${tc.name}__${flow.id}`) : null;
  let compare = "";
  if (recorded) {
    const same = recorded.status === res.status && recorded.kind === res.kind && recorded.equiv === res.equiv;
    compare = `<p class="note" style="margin:6px 0 0">${same ? "Matches" : "Differs from"} the recorded regression result for this job (${esc(recorded.status)}${recorded.kind ? ", " + esc(KIND_LABEL[recorded.kind]) : ""}, ${esc(recorded.equiv)}).</p>`;
  }
  const truth = res.injected.map((i) => `<li><code>${esc(i)}</code>: ${esc(DEFECTS[i])}</li>`);
  if (tc.ground_truth && !edited) truth.push(`<li>The testcase itself is labelled <code>${esc(tc.ground_truth)}</code>: the bug is in the test, not the tool.</li>`);
  $("#verdict").innerHTML = `<div class="verdict ${res.status}">
      <div class="t">${res.status === "pass" ? "PASS" : "FAIL"} ${res.kind ? badge(res.kind, KIND_LABEL[res.kind]) : ""} <span class="note">${esc(res.duration_s)} s in your browser</span></div>
      ${res.message ? `<div style="margin-top:4px">${esc(res.message)}</div>` : ""}${compare}</div>
    <details class="card"><summary><b>Reveal the ground truth</b> (what the shim injected; triage never sees this)</summary>
      <ul style="margin:8px 0 0">${truth.length ? truth.join("") : "<li>Nothing was injected: the tool behaved correctly on this job.</li>"}</ul>
      <p class="note" style="margin:8px 0 0">${state.build === "buggy" ? "Switch the build to fixed and run again to see the defect disappear." : "The fixed build turns the injected tool defects off; the known issue stays because it is still open."}</p></details>`;
}

export function initLive(d) {
  data = d;
  $("#presets").innerHTML = PRESETS.map(([label, tc, flow], i) => `<button type="button" data-i="${i}">${esc(label)}</button>`).join("");
  $("#presets").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]");
    if (!b) return;
    const [, tc, flow] = PRESETS[Number(b.dataset.i)];
    selectJob(tc, flow, "buggy");
  });
  $("#sel-tc").innerHTML = d.testcases.map((t) => `<option value="${esc(t.name)}">${esc(t.name.replace(/^tc_/, ""))}${t.origin === "legacy" ? " (legacy)" : ""}</option>`).join("");
  $("#sel-flow").innerHTML = d.flows.map((f) => `<option value="${esc(f.id)}">${esc(f.id)}: ${esc(f.command)}${f.in_regression ? "" : " (not in regression)"}</option>`).join("");
  $("#sel-tc").addEventListener("change", (e) => selectJob(e.target.value, state.flow.id));
  $("#sel-flow").addEventListener("change", (e) => { state.flow = flowById(e.target.value); });
  $("#b-buggy").addEventListener("click", () => setBuild("buggy"));
  $("#b-fixed").addEventListener("click", () => setBuild("fixed"));
  $("#src").addEventListener("input", renderMeta);
  $("#reset-src").addEventListener("click", () => { $("#src").value = state.tc.source; renderMeta(); });
  $("#run").addEventListener("click", run);
  selectJob("tc_mul_reg", "xc7_flatten", "buggy");
}
