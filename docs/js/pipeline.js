// One regression job in the browser: a line-for-line port of vagent/toolshim.py, vagent/oracles.py
// and vagent/worker.py. `runYosys(args, files)` must resolve to {code, files} (see yosys-worker.js).

export const FAMILY_PATTERNS = {
  DSP: /^DSP48/,
  BRAM: /^RAMB(18|36)/,
  LUTRAM: /^RAM(16|32|64|128|256)(X1[SD]|M)/,
  SRL: /^SRL(16E|C32E)$/,
  CARRY: /^CARRY[48]$/,
  LUT: /^LUT[1-6]$/,
  FF: /^FD[RSCP]E?$|^\$_S?DFFE?_/,
  MUXF: /^MUXF[5-9]$/,
  IO: /^(IBUF|OBUF|OBUFT|IOBUF|BUFG)$/,
};

export const DEFECTS = {
  "TD-DSP-FLATTEN": "DSP48E1 A-port bit order swapped when -flatten is used",
  "TD-LUTRAM-NOIOPAD": "distributed RAM read address bits swapped when -noiopad is used",
  "TD-FSM-DFF": "FSM output-decode LUT inverted when abc9 runs with -dff",
  "TD-CARRY-FLATTEN": "carry chains (CARRY4) not used for adders when -flatten is used (QoR regression)",
  "KDB-0001": "internal error in wreduce on parameter-overridden hierarchy in generic synth (known, open)",
};

const EQUIV_UNSUPPORTED = new Set(["BRAM"]);
const MUL = /[\w)\]}]\s*\*\s*[\w({]/;
const ADD = /[\w)\]}]\s*\+\s*[\w({]/;
const ROW = /^\s+(\d+\s+)?\\(gold|gate)_(\S+)\s+\S+\s+\S+\s+([01xz]+)\s*$/;
export const SAT_TIMEOUT = 60;

export function flowInfo(flow) {
  const toks = flow.command.split(/\s+/).filter(Boolean);
  const base = toks[0];
  const groups = [];
  for (let i = 1; i < toks.length;) {
    if (toks[i] === "-family") { i += 2; continue; }
    if (i + 1 < toks.length && !toks[i + 1].startsWith("-")) { groups.push(`${toks[i]} ${toks[i + 1]}`); i += 2; }
    else { groups.push(toks[i]); i += 1; }
  }
  return { ...flow, base, family: base === "synth_xilinx" ? "xc7" : "generic", options: groups };
}

export const goldenKey = (tc, command) => `${tc}|${command.split(/\s+/).filter(Boolean).join(" ")}`;

const stripComments = (src) => src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*/g, "");
const sorted = (obj) => Object.keys(obj).sort();
const lines = (s) => s.split(/\r?\n/);

function* cellsOf(netlist) {
  for (const m of sorted(netlist.modules)) {
    const cells = netlist.modules[m].cells;
    for (const c of sorted(cells)) yield cells[c];
  }
}

function mutDsp(netlist) {
  for (const c of cellsOf(netlist)) {
    if (c.type.startsWith("DSP48")) {
      const a = c.connections.A;
      [a[0], a[1]] = [a[1], a[0]];
      return true;
    }
  }
  return false;
}

function mutLutram(netlist) {
  for (const c of cellsOf(netlist)) {
    const conn = c.connections;
    if (!FAMILY_PATTERNS.LUTRAM.test(c.type)) continue;
    const ports = ["ADDRA", "ADDRB", "ADDRC"].filter((p) => p in conn && conn[p].length > 1);
    for (const p of ports) [conn[p][0], conn[p][1]] = [conn[p][1], conn[p][0]];
    if (!ports.length && "DPRA0" in conn && "DPRA1" in conn) {
      [conn.DPRA0, conn.DPRA1] = [conn.DPRA1, conn.DPRA0];
      ports.push("DPRA");
    }
    if (ports.length) return true;
  }
  return false;
}

function mutFsmOutput(netlist, top) {
  const mod = netlist.modules[top];
  const outBits = new Set();
  for (const p of Object.values(mod.ports)) if (p.direction === "output") p.bits.forEach((b) => outBits.add(b));
  const cells = mod.cells;
  const hits = (arr, set) => (arr || []).some((b) => set.has(b));
  for (const c of Object.values(cells)) {
    if ((c.type === "OBUF" || c.type === "OBUFT") && hits(c.connections.O, outBits)) c.connections.I.forEach((b) => outBits.add(b));
  }
  const ffInputs = new Set();
  for (const c of Object.values(cells)) {
    if (c.type.startsWith("FD") && hits(c.connections.Q, outBits)) {
      for (const port of ["D", "R", "S", "CE"]) (c.connections[port] || []).forEach((b) => { if (Number.isInteger(b)) ffInputs.add(b); });
    }
  }
  for (const name of sorted(cells)) {
    const c = cells[name];
    if (c.type.startsWith("LUT") && hits(c.connections.O, ffInputs)) {
      c.parameters.INIT = [...c.parameters.INIT].map((ch) => (ch === "0" ? "1" : "0")).join("");
      return true;
    }
  }
  return false;
}

export function countCells(netlist, top) {
  const mods = netlist.modules;
  const isLeaf = (t) => {
    const m = mods[t];
    return !m || "blackbox" in (m.attributes || {}) || !m.cells || !Object.keys(m.cells).length;
  };
  const acc = {};
  const walk = (name) => {
    for (const c of Object.values(mods[name].cells)) {
      if (isLeaf(c.type)) acc[c.type] = (acc[c.type] || 0) + 1;
      else walk(c.type);
    }
  };
  walk(top);
  return Object.fromEntries(sorted(acc).map((k) => [k, acc[k]]));
}

export function families(cells) {
  const out = {};
  for (const [t, n] of Object.entries(cells)) {
    for (const [fam, pat] of Object.entries(FAMILY_PATTERNS)) if (pat.test(t)) out[fam] = (out[fam] || 0) + n;
  }
  return Object.fromEntries(sorted(out).map((k) => [k, out[k]]));
}

const text = (f) => (f == null ? "" : typeof f === "string" ? f : new TextDecoder().decode(f));

async function yosysScript(runYosys, name, script, files) {
  const r = await runYosys(["-q", "-l", `${name}.log`, "-s", `${name}.ys`], { ...files, [`${name}.ys`]: script });
  return { code: r.code, files: r.files, log: text(r.files[`${name}.log`]) };
}

// toolshim.run_tool: the tool under test, with defects that fire only under feature + option combinations
export async function runTool(runYosys, tc, flow, build) {
  const rtl = stripComments(tc.source);
  const opts = flow.options;
  const injected = [];
  let cmd = flow.command;
  const noIndex = rtl.replace(/\[[^\]]*\]/g, "");
  const hiddenNocarry = build === "buggy" && flow.base === "synth_xilinx" && opts.includes("-flatten")
    && ADD.test(noIndex) && !MUL.test(noIndex);
  if (hiddenNocarry) cmd += " -nocarry";
  let script = `read_verilog design.v\nhierarchy -check -top ${tc.top}\n${cmd} -top ${tc.top}\nwrite_json netlist.json\n`;
  const r = await yosysScript(runYosys, "synth", script, { "design.v": tc.source });
  let rc = r.code;
  let log = r.log;
  let netText = r.files["netlist.json"] != null ? text(r.files["netlist.json"]) : null;
  if (hiddenNocarry) {
    log = log.replaceAll(" -nocarry", "");
    script = script.replaceAll(" -nocarry", "");
    if (rc === 0) injected.push("TD-CARRY-FLATTEN");
  }
  if (rc === 0 && flow.base === "synth" && /#\s*\(\s*\./.test(rtl)) {
    let cut = log.indexOf("Executing WREDUCE pass");
    cut = cut >= 0 ? log.indexOf("\n", cut) : log.length;
    if (cut < 0) cut = log.length - 1;
    log = log.slice(0, cut) + "\nERROR: Assert `GetSize(sig) == width' failed in pass wreduce.\n";
    netText = null;
    rc = 1;
    injected.push("KDB-0001");
  }
  let netlist = netText ? JSON.parse(netText) : null;
  if (rc === 0 && build === "buggy") {
    const cells = countCells(netlist, tc.top);
    const types = Object.keys(cells);
    if (opts.includes("-flatten") && types.some((t) => t.startsWith("DSP48")) && mutDsp(netlist)) injected.push("TD-DSP-FLATTEN");
    if (opts.includes("-noiopad") && families(cells).LUTRAM && mutLutram(netlist)) injected.push("TD-LUTRAM-NOIOPAD");
    if (opts.includes("-dff") && log.includes("Extracting FSM") && mutFsmOutput(netlist, tc.top)) injected.push("TD-FSM-DFF");
  }
  return { rc, log, script, netlist, injected };
}

export function equivalenceScript(tc, flow) {
  const top = tc.top;
  const techmap = flow.family === "xc7" ? "techmap -wb -D EQUIV -autoproc -map +/xilinx/cells_sim.v\n" : "";
  let sat = `sat -verify -prove-asserts -show-inputs -show-outputs -timeout ${SAT_TIMEOUT}`;
  if (tc.sequential) {
    sat += ` -set-init-zero -seq ${tc.depth}`;
    if (tc.reset) sat += ` -set-at 1 in_${tc.reset} 1 -prove-skip 1`;
  }
  return `read_verilog design.v\nhierarchy -check -top ${top}\nproc\nflatten\nmemory\nopt_clean\n`
    + `design -stash gold_d\n`
    + `read_json netlist.json\nhierarchy -top ${top}\n${techmap}flatten\nopt_clean\ndesign -stash gate_d\n`
    + `design -copy-from gold_d -as gold ${top}\ndesign -copy-from gate_d -as gate ${top}\n`
    + `async2sync\nmiter -equiv -flatten -make_assert -make_outputs -ignore_gold_x gold gate miter\n`
    + `hierarchy -top miter\n${sat} miter\n`;
}

export function mismatchedOutputs(log) {
  const vals = new Map();
  for (const line of lines(log)) {
    const m = ROW.exec(line);
    if (!m) continue;
    const [, step, side, port, bits] = m;
    const key = `${step || ""}|${port}`;
    if (!vals.has(key)) vals.set(key, { port });
    vals.get(key)[side] = bits;
  }
  const bad = new Set();
  for (const v of vals.values()) if (v.gold != null && v.gate != null && v.gold !== v.gate) bad.add(v.port);
  return [...bad].sort();
}

export async function equivalence(runYosys, tc, flow, netlist, fams) {
  const unsupported = Object.keys(fams).filter((f) => EQUIV_UNSUPPORTED.has(f)).sort();
  if (unsupported.length) return { verdict: "skipped", note: `netlist contains ${JSON.stringify(unsupported)}; no trusted SAT model`, log: "", script: "" };
  const script = equivalenceScript(tc, flow);
  const r = await yosysScript(runYosys, "equiv", script, { "design.v": tc.source, "netlist.json": JSON.stringify(netlist) });
  const log = r.log;
  const kind = tc.sequential ? `bounded ${tc.depth} cycles from reset` : "full combinational proof";
  if (log.includes("SAT proof finished - no model found: SUCCESS") && r.code === 0) return { verdict: tc.sequential ? "bounded" : "proven", note: kind, log, script };
  if (log.includes("model found: FAIL")) {
    const outs = mismatchedOutputs(log);
    return { verdict: "fail", note: "counterexample: outputs differ on " + (outs.length ? outs : ["<unknown>"]).join(", "), log, script };
  }
  if (log.includes("SAT solver: TIMEOUT")) return { verdict: "inconclusive", note: `SAT timeout after ${SAT_TIMEOUT}s`, log, script };
  const err = lines(log).find((l) => l.startsWith("ERROR")) || "unknown oracle error";
  return { verdict: "inconclusive", note: `oracle error: ${err.slice(0, 200)}`, log, script };
}

export function checkExpectations(tc, flow, cells, fams) {
  const failures = [];
  for (const rule of (tc.expect || {})[flow.family] || []) {
    const [name, n] = rule.split(">=");
    const have = fams[name] ?? cells[name] ?? 0;
    if (have < Number(n)) failures.push(`expected ${name}>=${n}, got ${have}`);
  }
  return failures;
}

export function goldenDrift(fams, golden) {
  if (golden == null) return [];
  const drift = [];
  for (const fam of ["DSP", "BRAM", "LUTRAM", "SRL", "CARRY", "MUXF"]) {
    const a = golden[fam] || 0, b = fams[fam] || 0;
    if (a !== b) drift.push(`${fam} ${b < a ? "decreased" : "increased"} (${a}->${b})`);
  }
  const lg = golden.LUT || 0, lb = fams.LUT || 0;
  if (lg && lb > lg * 1.25) drift.push(`LUT increased >25% (${lg}->${lb})`);
  return drift;
}

function firstError(log) {
  const l = lines(log).find((x) => x.includes("ERROR"));
  return l ? l.trim() : "tool exited with non-zero status and no ERROR line";
}

export function classifyToolError(msg) {
  if (/syntax error|Parser error|parse error|unexpected/i.test(msg)) return "parse_error";
  if (/not found|hierarchy|Can't find|is not part of the design|Unknown module/i.test(msg)) return "elab_error";
  return "crash";
}

function excerpt(log, needle, before = 6, after = 3) {
  const ls = lines(log);
  let idx = ls.findIndex((l) => needle && l.includes(needle));
  if (idx < 0) idx = ls.length - 1;
  return ls.slice(Math.max(0, idx - before), idx + after + 1).join("\n");
}

// worker.run_job: tool -> QoR checks -> equivalence oracle. onStage(name, detail) reports progress.
export async function runJob(runYosys, tc, flowIn, build, golden, onStage = () => {}) {
  const flow = flowIn.options ? flowIn : flowInfo(flowIn);
  const t0 = performance.now();
  const res = { testcase: tc.name, flow: flow.id, build, status: "pass", kind: null, message: "", equiv: "not_run",
    equiv_note: "", cells: {}, families: {}, qor_failures: [], golden_match: null, excerpt: "" };
  onStage("synth");
  const tool = await runTool(runYosys, tc, flow, build);
  res.injected = tool.injected;
  res.synth_script = tool.script;
  res.synth_log = tool.log;
  if (tool.rc !== 0) {
    const msg = firstError(tool.log);
    Object.assign(res, { status: "fail", kind: classifyToolError(msg), message: msg, excerpt: excerpt(tool.log, msg) });
  } else {
    const cells = countCells(tool.netlist, tc.top);
    const fams = families(cells);
    Object.assign(res, { cells, families: fams });
    res.qor_failures = checkExpectations(tc, flow, cells, fams);
    const drift = goldenDrift(fams, golden);
    res.golden_match = golden == null ? null : !drift.length;
    res.drift = drift;
    onStage("equiv");
    const eq = await equivalence(runYosys, tc, flow, tool.netlist, fams);
    Object.assign(res, { equiv: eq.verdict, equiv_note: eq.note, equiv_script: eq.script, equiv_log: eq.log });
    if (eq.verdict === "inconclusive" && eq.note.startsWith("oracle error")) {
      Object.assign(res, { status: "fail", kind: "infra", message: `equivalence oracle could not run: ${eq.note}`, excerpt: excerpt(eq.log, "ERROR") });
    } else if (eq.verdict === "fail") {
      Object.assign(res, { status: "fail", kind: "equiv_fail", message: `equivalence check failed: ${eq.note}`, excerpt: excerpt(eq.log, "model found: FAIL", 2, 1) });
    } else if (drift.length) {
      res.status = "fail"; res.kind = "qor_golden";
      res.message = "QoR drift vs golden: " + drift.join("; ") + (res.qor_failures.length ? " | expectation: " + res.qor_failures.join("; ") : "");
    } else if (res.qor_failures.length) {
      Object.assign(res, { status: "fail", kind: "qor_expect", message: "QoR expectation unmet: " + res.qor_failures.join("; ") });
    }
    if (res.kind === "qor_golden" || res.kind === "qor_expect") res.excerpt = "cells: " + JSON.stringify(fams);
  }
  res.duration_s = Math.round(performance.now() - t0) / 1000;
  onStage("done");
  return res;
}
