// Runs Yosys (YoWASP WebAssembly build, same 0.71 release as the Python package) off the main thread.
import { runYosys } from "https://cdn.jsdelivr.net/npm/@yowasp/yosys@0.71.1284/gen/bundle.js";

const fetched = new Map();

self.onmessage = async ({ data: { id, args, files } }) => {
  const fetchProgress = ({ source, totalLength, doneLength }) => {
    fetched.set(source, [doneLength, totalLength]);
    let done = 0, total = 0;
    for (const [d, t] of fetched.values()) { done += d; total += t; }
    self.postMessage({ id, progress: { done, total } });
  };
  try {
    self.postMessage({ id, files: await runYosys(args, files, { stdout: null, stderr: null, fetchProgress }), code: 0 });
  } catch (e) {
    if (e && typeof e.code === "number" && e.files) self.postMessage({ id, code: e.code, files: e.files });
    else self.postMessage({ id, error: String((e && e.message) || e) });
  }
};
