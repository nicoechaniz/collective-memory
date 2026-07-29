import { t } from "./i18n";

// Colores deterministas: mismo proyecto → mismo color, siempre (hash del nombre, no orden de llegada).

function hashInt(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

export function colorForProject(project: string): string {
  const hue = hashInt(project || "root") % 360;
  return `hsl(${hue}, 62%, 62%)`;
}

// Paleta por índice de comunidad (golden angle → hues bien separados).
export function communityColor(i: number): string {
  return `hsl(${Math.round((i * 137.508) % 360)}, 72%, 58%)`;
}

export function edgeColor(type: string): string {
  if (type === "wikilink") return "#e8e1c6";
  if (type === "source_of") return "#f2a65a";
  if (type === "semantic_neighbor") return "#78a7ff";
  if (type === "duplicate_of") return "#ff8f5a";
  if (type === "inter_project") return "#d9c268";
  if (type === "contains") return "rgba(180,190,190,0.28)";
  if (type === "challenges") return "#e05c5c";      // hallazgo → contraevidencia
  if (type === "bridges") return "#5ccfa8";         // hallazgo → puente entre lados
  if (type === "flags_freshness") return "#c98ce0"; // hallazgo → claim afectado por evidencia nueva
  if (type === "analogizes") return "#5ccfa8";
  return "rgba(180,190,190,0.28)";
}

export const EDGE_TYPE_LABELS: Record<string, string> = {
  wikilink: "wikilink",
  source_of: "source",
  semantic_neighbor: t("semántico", "semantic"),
  inter_project: t("inter-proyecto", "cross-project"),
  contains: t("contiene", "contains"),
  duplicate_of: t("duplicado", "duplicate"),
  challenges: t("contraevidencia", "counter-evidence"),
  bridges: t("puente", "bridge"),
  flags_freshness: t("frescura", "freshness"),
  analogizes: t("analogía", "analogy"),
};

export function sizeForKind(kind: string, degree: number): number {
  const base = kind === "project" ? 12 : kind === "map" ? 9 : kind === "discovery" ? 8 : kind === "synthesis" ? 8 : kind === "biblioteca" ? 7 : kind === "source" ? 6 : kind === "fs_media" ? 5 : kind === "fs_code" || kind === "fs_config" ? 2.5 : 4;
  return base + Math.min(8, Math.log1p(degree || 1) * 1.2);
}
// fs_dataset usa el base default (4): documentación de datasets, visible como fs_doc.
