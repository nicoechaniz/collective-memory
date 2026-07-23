import { useEffect, useState } from "react";
import { colorForProject } from "./colors";

type Stats = {
  generation: string;
  built_at: number;
  counts: { docs: number; projects: number; global_nodes: number; global_edges: number; by_kind: Record<string, number>; by_project: Record<string, number>; duplicates: number; excluded: number | null };
  warnings: string[];
  artifact_sizes: Record<string, { bytes: number; gzip_bytes: number }>;
  cross_edges?: { total: number; by_type: Record<string, number> };
};

type Health = Record<string, unknown>;

function rel(ts: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 90) return `hace ${s}s`;
  if (s < 5400) return `hace ${Math.round(s / 60)}min`;
  if (s < 129600) return `hace ${Math.round(s / 3600)}h`;
  return `hace ${Math.round(s / 86400)}d`;
}

function fmtBytes(b: number): string {
  if (b > 1048576) return `${(b / 1048576).toFixed(1)} MB`;
  if (b > 1024) return `${Math.round(b / 1024)} KB`;
  return `${b} B`;
}

export default function StatsPanel() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/ui/stats").then((r) => r.json()).then(setStats).catch((e) => setError(String(e)));
    fetch("/health").then((r) => r.json()).then(setHealth).catch(() => {});
  }, []);

  if (error) return <div className="error">{error}</div>;
  if (!stats) return <div className="loading">cargando estado…</div>;

  const stale = Boolean(health?.ui_stale) || Boolean(health?.stale);
  const topProjects = Object.entries(stats.counts.by_project || {}).slice(0, 12);
  const maxCount = Math.max(1, ...topProjects.map(([, n]) => n));

  return (
    <div className="stats-panel">
      <div className={`freshness ${stale ? "stale" : "fresh"}`}>
        <span className="dot" /> {stale ? "índice atrasado respecto a los últimos cambios" : "memoria al día"}
      </div>
      <dl>
        <dt>documentos</dt><dd>{stats.counts.docs?.toLocaleString()} en {stats.counts.projects} proyectos ({stats.counts.duplicates} duplicados)</dd>
        <dt>índice</dt><dd>{String(health?.n_chunks ?? "?")} chunks · modo {String(health?.serving_search_mode ?? "?")} · scope {String(health?.corpus_scope ?? "?")}</dd>
        <dt>atlas</dt><dd>gen {stats.generation} · construido {rel(stats.built_at)}</dd>
        {stats.cross_edges && <><dt>aristas cross</dt><dd>{stats.cross_edges.total.toLocaleString()}</dd></>}
        <dt>excluidos</dt><dd>{stats.counts.excluded?.toLocaleString() ?? "—"}</dd>
      </dl>
      <h3>Top proyectos</h3>
      <div className="bars">
        {topProjects.map(([p, n]) => (
          <div className="bar-row" key={p} title={`${p}: ${n} docs`}>
            <span className="bar-label">{p}</span>
            <span className="bar-track"><i style={{ width: `${(n / maxCount) * 100}%`, background: colorForProject(p) }} /></span>
            <span className="bar-n">{n}</span>
          </div>
        ))}
      </div>
      <h3>Por tipo</h3>
      <div className="kind-chips">
        {Object.entries(stats.counts.by_kind || {}).map(([k, n]) => (
          <span key={k} className="chip static">{k} <b>{n}</b></span>
        ))}
      </div>
      {stats.warnings?.length > 0 && (
        <details className="stats-warnings">
          <summary>warnings del build ({stats.warnings.length})</summary>
          <ul>{stats.warnings.slice(0, 20).map((w, i) => <li key={i}>{w}</li>)}</ul>
        </details>
      )}
      <details className="stats-artifacts">
        <summary>artefactos</summary>
        <ul>
          {Object.entries(stats.artifact_sizes || {}).map(([f, s]) => (
            <li key={f}>{f}: {fmtBytes(s.bytes)} (gz {fmtBytes(s.gzip_bytes)})</li>
          ))}
        </ul>
      </details>
    </div>
  );
}
