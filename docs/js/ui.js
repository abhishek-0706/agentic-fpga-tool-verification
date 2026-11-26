export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const badge = (cls, label = cls) => `<span class="b ${esc(cls)}">${esc(label)}</span>`;
export const KIND_LABEL = { parse_error: "parse error", elab_error: "elab error", equiv_fail: "not equivalent", qor_golden: "QoR drift",
  qor_expect: "QoR unmet", crash: "crash", timeout: "timeout", infra: "infra" };

export function famTable(fams, golden) {
  const keys = [...new Set([...Object.keys(fams || {}), ...Object.keys(golden || {})])].sort();
  if (!keys.length) return '<p class="note">No mapped primitive families (generic gates only).</p>';
  return `<div class="scroll"><table><tr><th>Family</th><th class="num">This netlist</th>${golden ? '<th class="num">Golden</th>' : ""}</tr>${keys.map((k) => {
    const a = (fams || {})[k] ?? 0, g = golden ? golden[k] ?? 0 : null;
    return `<tr><td>${esc(k)}</td><td class="num">${a}</td>${golden ? `<td class="num">${g}${g !== a ? " " + badge("fail", "differs") : ""}</td>` : ""}</tr>`;
  }).join("")}</table></div>`;
}
