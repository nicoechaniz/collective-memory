// Jerarquía de etiquetas + nombres de nubes semánticas (versión keywords).
import type { NodeData } from "./api";

export type LabelLevel = "auto" | 1 | 2 | 3;

/** tier 1 = proyectos · 2 = curados/hubs (degree ≥ p90) · 3 = el resto */
export function tierMap(nodes: NodeData[]): Map<string, number> {
  const degs = nodes.filter((n) => n.kind !== "project").map((n) => n.degree).sort((a, b) => a - b);
  const p90 = degs.length ? degs[Math.floor(0.9 * (degs.length - 1))] : Infinity;
  const m = new Map<string, number>();
  for (const n of nodes) m.set(n.id, n.kind === "project" ? 1 : n.curated || n.degree >= p90 ? 2 : 3);
  return m;
}

const STOP = new Set([
  "de","la","el","los","las","del","y","en","a","un","una","unas","unos","para","por","con","sobre","al","se","que","su","sus","es","lo","como","mas","más",
  "the","of","and","in","to","for","on","an","by","with","from","at","is","as","or","this","that",
  "memoria","informe","reporte","report","notes","nota","notas","doc","docs","documento","documentos","readme","index","plan","resumen","sintesis","síntesis",
]);

/** Top-2 términos de los títulos por comunidad (peso por degree) → "hermes · memoria". */
export function communityNames(nodes: NodeData[], communityOf: Record<string, number>): Map<number, string> {
  const tf = new Map<number, Map<string, number>>();
  for (const n of nodes) {
    const c = communityOf[n.id];
    if (c === undefined) continue;
    let bag = tf.get(c);
    if (!bag) tf.set(c, (bag = new Map()));
    const w = 1 + Math.log1p(n.degree || 0);
    const text = (n.title || n.label || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
    for (const tok of text.split(/[^a-z0-9]+/)) {
      if (tok.length < 3 || STOP.has(tok) || /^\d+$/.test(tok)) continue;
      bag.set(tok, (bag.get(tok) || 0) + w);
    }
  }
  const out = new Map<number, string>();
  for (const [c, bag] of tf) {
    const top = [...bag.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2).map(([t]) => t);
    if (top.length) out.set(c, top.join(" · "));
  }
  return out;
}
