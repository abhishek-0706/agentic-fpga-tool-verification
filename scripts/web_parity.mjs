// Checks that the browser port (docs/js/pipeline.js) reproduces the recorded Python results.
// Setup once:  npm install --no-save @yowasp/yosys@0.71.1284
// Run:         node scripts/web_parity.mjs [--quick]
// Every regression job runs on build=buggy and is compared with docs/data/run.json (status, failure kind,
// equivalence verdict, primitive families, golden match, injected defects). The jobs that fix verification
// closed are then re-run on build=fixed and must pass.
import { readFileSync } from "node:fs";
import { runYosys } from "@yowasp/yosys";
import { flowInfo, goldenKey, runJob } from "../docs/js/pipeline.js";

const data = JSON.parse(readFileSync(new URL("../docs/data/run.json", import.meta.url), "utf8"));
const quick = process.argv.includes("--quick");
const tcs = Object.fromEntries(data.testcases.map((t) => [t.name, t]));
const flows = Object.fromEntries(data.flows.map((f) => [f.id, flowInfo(f)]));

async function runner(args, files) {
  try {
    return { code: 0, files: await runYosys(args, files, { stdout: null, stderr: null }) };
  } catch (e) {
    if (e && typeof e.code === "number" && e.files) return { code: e.code, files: e.files };
    throw e;
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const toolDefects = (gt) => gt.map((g) => g.detail).filter((d) => d.startsWith("TD-") || d.startsWith("KDB-")).sort();
let jobs = data.results;
if (quick) jobs = jobs.filter((r) => r.ground_truth.some((g) => g.detail.startsWith("TD-")) || r.testcase === "tc_comb_mux");
let bad = 0;
for (const want of jobs) {
  const tc = tcs[want.testcase], flow = flows[want.flow];
  const got = await runJob(runner, tc, flow, "buggy", data.golden[goldenKey(tc.name, flow.command)] ?? null);
  const checks = {
    status: [got.status, want.status], kind: [got.kind, want.kind], equiv: [got.equiv, want.equiv],
    families: [got.families, want.families], golden_match: [got.golden_match, want.golden_match],
    injected: [[...got.injected].sort(), toolDefects(want.ground_truth)],
  };
  const diffs = Object.entries(checks).filter(([, [g, w]]) => !same(g, w));
  if (diffs.length) bad++;
  console.log(`${diffs.length ? "DIFF" : "ok  "} ${want.job_id.padEnd(30)} ${got.status}/${got.kind ?? "-"}/${got.equiv} ${got.duration_s}s`
    + diffs.map(([k, [g, w]]) => `\n      ${k}: web=${JSON.stringify(g)} python=${JSON.stringify(w)}`).join(""));
}
for (const job of data.fixverify.delta.fixed) {
  const [tcName, flowId] = job.split("__");
  const tc = tcs[tcName], flow = flows[flowId];
  const got = await runJob(runner, tc, flow, "fixed", data.golden[goldenKey(tc.name, flow.command)] ?? null);
  if (got.status !== "pass") bad++;
  console.log(`${got.status === "pass" ? "ok  " : "DIFF"} ${job.padEnd(30)} fixed build: ${got.status}/${got.kind ?? "-"}/${got.equiv}`);
}
console.log(bad ? `\n${bad} mismatch(es)` : "\nall jobs match the recorded Python results");
process.exit(bad ? 1 : 0);
