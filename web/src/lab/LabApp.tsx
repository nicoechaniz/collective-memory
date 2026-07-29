import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import type { GraphPayload, Manifest, NodeData } from "../api";

const GraphView = lazy(() => import("../GraphView"));
const DocReader = lazy(() => import("../DocReader"));

const BUILD_ENV = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env || {};
const STRUCTURAL_ONLY = BUILD_ENV.VITE_LAB_STRUCTURAL_ONLY === "1";
const STRUCTURAL_NOTICE = BUILD_ENV.VITE_LAB_STRUCTURAL_NOTICE ||
  "Podés ejecutar operadores sobre los vectores ya calculados. Los resultados son precandidatos estructurales sin validación de un juez LLM.";
const STRUCTURAL_OPERATORS = new Set(["latent_bridge", "cluster_frontier", "outlier"]);

function StructuralNotice() {
  if (!STRUCTURAL_ONLY) return null;
  return (
    <aside className="lab-mode-notice" role="status">
      <strong>Modo demostración sin inferencia en vivo</strong>
      <span>{STRUCTURAL_NOTICE}</span>
    </aside>
  );
}

// ---------- fetch con token ----------

function getToken() {
  return localStorage.getItem("lab.token") || "";
}

async function pgJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { Authorization: `Bearer ${getToken()}`, ...(init?.headers || {}) },
  });
  if (res.status === 401) {
    localStorage.removeItem("lab.token");
    window.location.reload();
    throw new Error("token inválido");
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

// ---------- tipos ----------

type Job = { id: number; owner: string; status: string; params_json: string; created_at: string; finished_at: string | null; error: string | null };
type CandRow = { id: string; owner: string; discovery_type: string; status: string; title: string | null; novelty_score: number | null; support_score: number | null; unexpectedness_score: number | null; flags_json: string; created_at: string };
type Source = { role: string; doc_id: string; project: string | null; kind: string | null; title: string | null; snippet: string | null };
type CandDetail = CandRow & { claim: string | null; why_interesting: string | null; roles_json: string; falsification_json: string; generated_by: string | null; sources: Source[]; reviews: { reviewer: string; from_status: string; to_status: string; note: string; created_at: string }[] };

const OPERATOR_LABELS: Record<string, string> = {
  latent_bridge: "puentes latentes",
  cluster_frontier: "fronteras",
  outlier: "outliers",
  tension: "tensiones",
  analogy: "analogías",
  freshness_negative: "frescura",
};

// Explicación humana de cada operador (pedido en BITACORA_ADMIN 2026-07-13):
// visible como tooltip en los checkboxes y como panel desplegable de ayuda.
const OPERATOR_HELP: Record<string, string> = {
  latent_bridge:
    "Conexiones entre proyectos que el sistema ve en el espacio semántico pero que todavía no están enlazadas editorialmente. La pregunta es si lo que un proyecto resolvió le serviría concretamente al otro.",
  cluster_frontier:
    "Pares de proyectos con muchas cercanías semánticas y pocos links editoriales. Indica una zona de contacto subexplorada que merecería trabajo curatorial.",
  outlier:
    "Documentos cuyo barrio semántico real parece ser otro proyecto. Señala material fuera de lugar, reusable, citable o quizá reubicable.",
  tension:
    "Documentos que afirman cosas incompatibles entre sí. No es mera complementariedad: tiene que haber desacuerdo real, aunque pueda requerir mediadores o contexto.",
  analogy:
    "Isomorfismos estructurales entre dominios distintos. No palabras compartidas, sino una forma relacional semejante: problema, restricción, solución y costo, con punto de ruptura declarado.",
  freshness_negative:
    "Evidencia más nueva que actualiza, debilita o contradice un nodo ya publicado. Es el operador que detecta caducidad o revisión necesaria.",
};

function flagsOf(c: { flags_json: string }): Record<string, unknown> {
  try {
    return JSON.parse(c.flags_json || "{}");
  } catch {
    return {};
  }
}

// ---------- login ----------

function TokenGate({ onReady }: { onReady: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div style={{ maxWidth: 480, margin: "15vh auto", padding: 24 }}>
      <h2>🔭 Laboratorio de la Memoria Colectiva</h2>
      <StructuralNotice />
      <p>Pegá tu token de acceso:</p>
      <input
        style={{ width: "100%", padding: 8 }}
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && value.trim()) {
            localStorage.setItem("lab.token", value.trim());
            onReady();
          }
        }}
        placeholder="token…"
      />
      <p style={{ opacity: 0.6, fontSize: 12 }}>
        Acá corrés operadores de discovery sobre la memoria colectiva, revisás candidatos y proponés
        hallazgos. Lo que propongas pasa por revisión editorial antes de publicarse en el{" "}
        <a href="/atlas/">atlas</a>.
      </p>
    </div>
  );
}

// ---------- vista Correr ----------

type Judge = { id: string; model: string; label: string; desc: string; default?: boolean };

function RunView({ manifest }: { manifest: Manifest | null }) {
  const [ops, setOps] = useState<Set<string>>(new Set(["latent_bridge", "outlier"]));
  const [limit, setLimit] = useState(12);
  const [projects, setProjects] = useState<Set<string>>(new Set());
  const [judges, setJudges] = useState<Judge[]>([]);
  const [judge, setJudge] = useState<string>("");
  const [task, setTask] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [msg, setMsg] = useState("");
  const agentMode = task.trim().length > 0;

  useEffect(() => {
    pgJson<{ judges: Judge[]; default_judge: string }>("/pg/operators")
      .then((d) => { setJudges(d.judges); setJudge((j) => j || d.default_judge || ""); })
      .catch(() => {});
  }, []);

  const refresh = useCallback(() => {
    pgJson<{ jobs: Job[] }>("/pg/jobs").then((d) => setJobs(d.jobs)).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function run() {
    setMsg("encolando…");
    // Body por rama (unión exclusiva task/operadores): nunca ambos.
    const body = agentMode
      ? { task: task.trim(), provider: judge }
      : { operators: [...ops], limit, projects: [...projects], provider: judge };
    try {
      const r = await pgJson<{ job_id: number }>("/pg/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setMsg(`job #${r.job_id} encolado (${agentMode ? "agente dirigido" : "barrido"})`);
      refresh();
    } catch (e) {
      setMsg(String(e));
    }
  }

  return (
    <div style={{ padding: 16, overflow: "auto" }}>
      <h3>Descubrir</h3>
      <div style={{ margin: "0 0 14px" }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>¿Qué querés descubrir?</div>
        <textarea
          disabled={STRUCTURAL_ONLY}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          maxLength={2000}
          rows={3}
          style={{ width: "100%", boxSizing: "border-box", resize: "vertical" }}
          placeholder={STRUCTURAL_ONLY ? "Agente dirigido desactivado en este modo" : "Ej: relación entre dos investigaciones que no se citan · qué proyectos tocan un mismo tema sin saberlo · una idea de un proyecto que le sirva a otro"}
        />
        <div style={{ fontSize: 12, opacity: 0.6 }}>
          {STRUCTURAL_ONLY
            ? "La demostración usa únicamente el barrido estructural sobre vectores precomputados."
            : "Con texto acá, un agente explora la memoria colectiva dirigido por tu pedido. Vacío = barrido automático por operadores ↓"}
        </div>
      </div>

      <fieldset disabled={agentMode} style={{ opacity: agentMode ? 0.4 : 1, border: "1px solid #333", borderRadius: 6, padding: 10 }}>
        <legend style={{ fontSize: 13 }}>Barrido automático (operadores)</legend>
        <div className="actionbar" style={{ padding: 0 }}>
          {Object.entries(OPERATOR_LABELS).filter(([k]) => !STRUCTURAL_ONLY || STRUCTURAL_OPERATORS.has(k)).map(([k, label]) => (
            <label key={k} title={OPERATOR_HELP[k]}>
              <input
                type="checkbox"
                checked={ops.has(k)}
                onChange={() => setOps((p) => { const n = new Set(p); n.has(k) ? n.delete(k) : n.add(k); return n; })}
              />{" "}
              {label}
            </label>
          ))}
        </div>
        <details style={{ margin: "6px 0 8px", fontSize: 13 }}>
          <summary style={{ cursor: "pointer", opacity: 0.75 }}>❓ ¿Qué significa cada operador?</summary>
          <dl style={{ margin: "8px 0 0", maxWidth: "72ch" }}>
            {Object.entries(OPERATOR_LABELS).filter(([k]) => !STRUCTURAL_ONLY || STRUCTURAL_OPERATORS.has(k)).map(([k, label]) => (
              <div key={k} style={{ marginBottom: 6 }}>
                <dt style={{ fontWeight: 600, display: "inline" }}>{label}</dt>
                <dd style={{ display: "inline", margin: "0 0 0 6px", opacity: 0.8 }}>{OPERATOR_HELP[k]}</dd>
              </div>
            ))}
          </dl>
        </details>
        <label>
          candidatos (máx 30):{" "}
          <input type="number" min={1} max={30} value={limit} onChange={(e) => setLimit(Math.min(30, Math.max(1, parseInt(e.target.value, 10) || 1)))} />
        </label>
      </fieldset>
      {!STRUCTURAL_ONLY && <div style={{ margin: "10px 0" }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Modelo juez</div>
        {judges.map((j) => (
          <label key={j.id} style={{ display: "block", marginBottom: 6, cursor: "pointer" }}>
            <input type="radio" name="judge" checked={judge === j.id} onChange={() => setJudge(j.id)} />{" "}
            <b>{j.label}</b>
            <div style={{ fontSize: 12, opacity: 0.65, marginLeft: 22 }}>{j.desc}</div>
          </label>
        ))}
      </div>}
      {manifest && (
        <details style={{ margin: "8px 0" }}>
          <summary>limitar a proyectos ({projects.size || "todos"})</summary>
          <div className="project-list">
            {manifest.projects.slice().sort((a, b) => b.count - a.count).map((p) => (
              <label key={p.slug} className="project-check">
                <input
                  type="checkbox"
                  checked={projects.has(p.project)}
                  onChange={() => setProjects((prev) => { const n = new Set(prev); n.has(p.project) ? n.delete(p.project) : n.add(p.project); return n; })}
                />{" "}
                {p.project} <span>({p.count})</span>
              </label>
            ))}
          </div>
        </details>
      )}
      <div className="actionbar" style={{ padding: "10px 0" }}>
        <button className="primary" disabled={!agentMode && !ops.size} onClick={run}>
          {STRUCTURAL_ONLY ? "generar precandidatos" : agentMode ? "🔭 descubrir" : "▶ barrer"}
        </button> <span className="hint">{msg}</span>
      </div>
      <h4>Mis jobs</h4>
      <table style={{ width: "100%", fontSize: 13 }}>
        <thead><tr><th>#</th><th>estado</th><th>operadores</th><th>creado</th><th>error</th></tr></thead>
        <tbody>
          {jobs.map((j) => {
            let opsTxt = "";
            try { opsTxt = (JSON.parse(j.params_json).operators || []).join(", "); } catch { /* noop */ }
            return (
              <tr key={j.id}>
                <td>{j.id}</td>
                <td>{j.status === "running" ? "⏳ corriendo" : j.status === "queued" ? "🕒 en cola" : j.status === "done" ? "✅ listo" : `⚠️ ${j.status}`}</td>
                <td>{opsTxt}</td>
                <td>{j.created_at}</td>
                <td style={{ opacity: 0.7 }}>{(j.error || "").slice(0, 80)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {!STRUCTURAL_ONLY && <DirectorPanel />}
    </div>
  );
}

// ---------- panel del Director (F20) ----------

type DirectorStatus = { running: boolean; last_digest: { mtime: number; title: string; veredicto: string } | null };

function DirectorPanel() {
  const [st, setSt] = useState<DirectorStatus | null>(null);
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState("");
  const [digest, setDigest] = useState<string | null>(null);

  const refresh = useCallback(() => {
    pgJson<DirectorStatus>("/pg/director").then(setSt).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  async function fire() {
    if (!pw) { setMsg("necesitás la contraseña"); return; }
    setMsg("disparando…");
    try {
      await pgJson("/pg/director", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      setMsg("✅ director encolado — corre en segundo plano (~2-15 min)");
      setPw("");
      setTimeout(refresh, 2000);
    } catch (e) {
      setMsg(String(e).includes("403") ? "contraseña incorrecta" : String(e));
    }
  }

  async function showDigest() {
    try {
      const d = await pgJson<{ markdown: string }>("/pg/digest");
      setDigest(d.markdown);
    } catch { setDigest("(sin digest todavía)"); }
  }

  return (
    <div style={{ marginTop: 24, border: "1px solid #444", borderRadius: 6, padding: 12 }}>
      <h4 style={{ margin: "0 0 8px" }}>🎬 Director autónomo</h4>
      <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 10, maxWidth: "72ch" }}>
        Corre el ciclo completo solo (campañas + tareas + juicio) con un modelo de frontera y deja
        un digest. Protegido por contraseña. El promote sigue siendo del dueño.
      </div>
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        Estado: {st?.running ? <b>⏳ corriendo</b> : "○ inactivo"}
        {st?.last_digest && (
          <span style={{ opacity: 0.7 }}> · último digest: {st.last_digest.veredicto || st.last_digest.title}</span>
        )}
      </div>
      <div className="actionbar" style={{ padding: 0, gap: 8 }}>
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="contraseña del director"
          style={{ padding: 6, minWidth: 220 }}
        />
        <button className="primary" disabled={st?.running} onClick={fire}>🎬 correr director</button>
        <button onClick={showDigest}>ver último digest</button>
        <span className="hint">{msg}</span>
      </div>
      {digest !== null && (
        <pre style={{ marginTop: 10, maxHeight: 360, overflow: "auto", background: "#1a1a1a",
                      padding: 10, borderRadius: 4, fontSize: 12, whiteSpace: "pre-wrap" }}>{digest}</pre>
      )}
    </div>
  );
}

// ---------- vista Bandeja ----------

const TAB_BTN: React.CSSProperties = { fontSize: 13, padding: "5px 14px", lineHeight: 1.5 };

const ROLE_LABELS: Record<string, string> = {
  support: "evidencia", counter: "contraevidencia", bridge: "puente",
  target: "afectado", context: "contexto",
};

function TrayView({ onOpenDoc }: { onOpenDoc: (docId: string) => void }) {
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [rows, setRows] = useState<CandRow[]>([]);
  const [detail, setDetail] = useState<CandDetail | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState("");

  const refresh = useCallback(() => {
    pgJson<{ candidates: CandRow[] }>(`/pg/candidates?scope=${scope}`)
      .then((d) => { setRows(d.candidates); setSel(new Set()); })
      .catch((e) => setMsg(String(e)));
  }, [scope]);
  useEffect(refresh, [refresh]);

  async function open(id: string) {
    setDetail(await pgJson<CandDetail>(`/pg/candidate?id=${encodeURIComponent(id)}`));
  }

  async function act(id: string, path: "review" | "propose", status?: string) {
    setMsg("…");
    try {
      const note = window.prompt("Nota (opcional):") || "";
      await pgJson(`/pg/${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(path === "review" ? { id, status, note } : { id, note }),
      });
      setMsg("ok"); refresh(); open(id).catch(() => setDetail(null));
    } catch (e) { setMsg(String(e)); }
  }

  async function borrar(ids: string[]) {
    if (!ids.length || !window.confirm(`¿Eliminar ${ids.length} candidato(s)? No se puede deshacer.`)) return;
    setMsg("eliminando…");
    try {
      const r = await pgJson<{ borrados: number; rechazados: { id: string; razón: string }[] }>("/pg/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
      setMsg(`${r.borrados} eliminado(s)` + (r.rechazados.length ? ` · ${r.rechazados.length} no (${r.rechazados[0]["razón"]})` : ""));
      if (detail && ids.includes(detail.id)) setDetail(null);
      refresh();
    } catch (e) { setMsg(String(e)); }
  }

  const fals = useMemo(() => {
    if (!detail) return null;
    try { return JSON.parse(detail.falsification_json || "{}"); } catch { return null; }
  }, [detail]);
  const roles = useMemo(() => {
    if (!detail) return null;
    try { return JSON.parse(detail.roles_json || "{}"); } catch { return null; }
  }, [detail]);
  const flags = detail ? flagsOf(detail) : {};

  return (
    <div style={{ display: "flex", minHeight: 0, flex: 1 }}>
      {/* Lista angosta: el grueso de la pantalla es para el contenido del hallazgo */}
      <div style={{ flex: "0 0 30%", minWidth: 260, overflowY: "auto", padding: 10, borderRight: "1px solid #333" }}>
        <div className="actionbar" style={{ padding: "0 0 6px" }}>
          <button className={scope === "mine" ? "active" : ""} onClick={() => setScope("mine")}>míos</button>
          <button className={scope === "all" ? "active" : ""} onClick={() => setScope("all")}>de todos</button>
          <button disabled={!sel.size} onClick={() => borrar([...sel])}>🗑 eliminar ({sel.size})</button>
          <button disabled={!rows.some((c) => c.status === "discarded")}
                  onClick={() => borrar(rows.filter((c) => c.status === "discarded").map((c) => c.id))}>
            limpiar descartados
          </button>
        </div>
        <div style={{ fontSize: 12, opacity: 0.7, margin: "4px 0" }}>{msg}</div>
        <table style={{ width: "100%", fontSize: 11, tableLayout: "fixed" }}>
          <thead><tr><th style={{width:22}}></th><th style={{width:60}}>tipo</th><th>título</th><th style={{width:38}} title="inesperadez (cruce de barrios semánticos)">sorp</th><th style={{width:22}}>✔</th></tr></thead>
          <tbody>
            {rows.map((c) => {
              const f = flagsOf(c);
              return (
                <tr key={c.id} style={{ cursor: "pointer", background: detail?.id === c.id ? "#2a2a2a" : undefined }}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={sel.has(c.id)}
                           onChange={() => setSel((p) => { const n = new Set(p); n.has(c.id) ? n.delete(c.id) : n.add(c.id); return n; })} />
                  </td>
                  <td onClick={() => open(c.id)} style={{ opacity: 0.7 }} title={OPERATOR_HELP[c.discovery_type]}>{(OPERATOR_LABELS[c.discovery_type] || c.discovery_type).slice(0, 9)}</td>
                  <td onClick={() => open(c.id)} title={`${c.title || c.id} · ${c.status} · ${c.owner}`}
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                               fontWeight: c.status === "proposed" ? 700 : 400 }}>
                    {c.status === "proposed" ? "📤 " : ""}{c.title || c.id}
                  </td>
                  <td onClick={() => open(c.id)} style={{ textAlign: "right", opacity: 0.7 }}
                      title="inesperadez: 1.0 = cruza barrios casi aislados; 0 = mismo barrio; — sin prior">
                    {c.unexpectedness_score != null ? c.unexpectedness_score.toFixed(2) : "—"}
                  </td>
                  <td onClick={() => open(c.id)} title="screen del juez">
                    {f.screen_passed === true ? "✅" : f.unscreened ? "—" : "✗"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Panel de detalle: columna con el texto scrolleable y las acciones FIJAS al pie
          (fuera del área de scroll, así no tapan el contenido). */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }}>
        <div style={{ flex: 1, overflowY: "auto", padding: 16, lineHeight: 1.5, minHeight: 0 }}>
        {!detail && <p style={{ opacity: 0.6 }}>Elegí un candidato para ver su evidencia y el razonamiento completo.</p>}
        {detail && (
          /* maxWidth en ch: líneas de ~75 caracteres — a ancho completo (~1000px) el ojo
             pierde el renglón; el fondo del panel sigue ocupando todo. */
          <div style={{ maxWidth: "75ch" }}>
            <h3 style={{ marginTop: 0 }}>{detail.title || detail.id}</h3>
            <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 12 }}>
              <span title={OPERATOR_HELP[detail.discovery_type]}>{OPERATOR_LABELS[detail.discovery_type] || detail.discovery_type}</span> · modelo {detail.generated_by || "?"} ·
              novedad {detail.novelty_score ?? "?"} · sorpresa {detail.unexpectedness_score != null ? detail.unexpectedness_score.toFixed(2) : "—"} · dueño {detail.owner} · estado <b>{detail.status}</b>
            </div>

            <section style={{ marginBottom: 14 }}>
              <h4 style={{ margin: "0 0 4px" }}>Qué afirma</h4>
              <p style={{ margin: 0 }}>{detail.claim || "(sin claim — el juez lo rechazó)"}</p>
            </section>

            {detail.why_interesting && (
              <section style={{ marginBottom: 14 }}>
                <h4 style={{ margin: "0 0 4px" }}>Por qué importa</h4>
                <p style={{ margin: 0 }}>{detail.why_interesting}</p>
              </section>
            )}

            {/* Campos estructurales según el tipo (tensión, analogía…) */}
            {roles && (roles.side_a || roles.role_mapping || roles.breaking_point || roles.mediators) && (
              <section style={{ marginBottom: 14, background: "#232323", padding: 10, borderRadius: 6 }}>
                <h4 style={{ margin: "0 0 6px" }}>Estructura del hallazgo</h4>
                {roles.side_a && <p style={{ margin: "2px 0" }}><b>Un lado:</b> {String(roles.side_a)}</p>}
                {roles.side_b && <p style={{ margin: "2px 0" }}><b>El otro:</b> {String(roles.side_b)}</p>}
                {roles.intensity && <p style={{ margin: "2px 0" }}><b>Intensidad:</b> {String(roles.intensity)}</p>}
                {roles.mediators && <p style={{ margin: "2px 0" }}><b>Mediadores:</b> {JSON.stringify(roles.mediators)}</p>}
                {roles.role_mapping && <p style={{ margin: "2px 0" }}><b>Mapeo de roles:</b> {typeof roles.role_mapping === "string" ? roles.role_mapping : JSON.stringify(roles.role_mapping)}</p>}
                {roles.breaking_point && <p style={{ margin: "2px 0" }}><b>Dónde se rompe:</b> {String(roles.breaking_point)}</p>}
                {roles.next_step && <p style={{ margin: "2px 0" }}><b>Próximo paso:</b> {String(roles.next_step)}</p>}
              </section>
            )}

            {/* Falsación: el razonamiento COMPLETO, no un emoji */}
            {fals && Object.keys(fals).length > 0 && (
              <section style={{ marginBottom: 14, background: "#232323", padding: 10, borderRadius: 6 }}>
                <h4 style={{ margin: "0 0 6px" }}>
                  Falsación — {fals.passed ? "✅ superada" : "⚠️ NO superada (no promovible sin override)"}
                </h4>
                {fals.source_ablation && (
                  <div style={{ marginBottom: 6 }}>
                    <b>Prueba de ablación</b> (se quitó <code style={{ fontSize: 11 }}>{fals.source_ablation.removed?.split("/").pop()}</code>,
                    la fuente más fuerte): {fals.source_ablation.survives ? "el claim se sostuvo ✅" : "el claim se cayó ⚠️ (frágil)"}
                    {fals.source_ablation.reason && (
                      <div style={{ fontSize: 12, opacity: 0.75, marginTop: 2 }}>“{fals.source_ablation.reason}”</div>
                    )}
                  </div>
                )}
                {fals.negative_search && (
                  <div style={{ marginBottom: 6 }}>
                    <b>Búsqueda de contraevidencia:</b>{" "}
                    {fals.negative_search.contradicted ? "encontró documentos que lo contradicen ⚠️" : "no encontró contradicciones ✅"}
                    {fals.negative_search.reason && (
                      <div style={{ fontSize: 12, opacity: 0.75, marginTop: 2 }}>“{fals.negative_search.reason}”</div>
                    )}
                  </div>
                )}
                {fals.diversity_check && (
                  <div style={{ fontSize: 12, opacity: 0.8 }}>
                    <b>Diversidad:</b> proyectos [{(fals.diversity_check.projects || []).join(", ")}] · tipos [{(fals.diversity_check.kinds || []).join(", ")}]
                  </div>
                )}
                {fals.stale_check?.stale_sources?.length > 0 && (
                  <div style={{ fontSize: 12, color: "#e0a" }}>⚠️ fuentes que cambiaron desde que se generó</div>
                )}
              </section>
            )}

            {/* Flags explicados en castellano */}
            {(flags.fragile || flags.contradicted || flags.low_diversity || flags.hub_sources || flags.screen_reason) && (
              <section style={{ marginBottom: 14, fontSize: 13 }}>
                <h4 style={{ margin: "0 0 4px" }}>Advertencias</h4>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {flags.screen_reason ? <li>El juez lo rechazó: {String(flags.screen_reason)}</li> : null}
                  {flags.fragile ? <li>Frágil: depende de una sola fuente (no sobrevivió la ablación).</li> : null}
                  {flags.contradicted ? <li>Contradicho: hay documentos que lo desmienten.</li> : null}
                  {flags.low_diversity ? <li>Poca diversidad: la evidencia viene de un solo proyecto/tipo.</li> : null}
                  {Array.isArray(flags.hub_sources) && flags.hub_sources.length ? (
                    <li>Se apoya en documentos-hub (aparecen en todo, evidencia débil): {(flags.hub_sources as string[]).map((h) => h.split("/").pop()).join(", ")}</li>
                  ) : null}
                </ul>
              </section>
            )}

            <section style={{ marginBottom: 14 }}>
              <h4 style={{ margin: "0 0 6px" }}>Evidencia ({detail.sources.length} documentos)</h4>
              {detail.sources.map((s, i) => (
                <div key={i} style={{ marginBottom: 8, paddingLeft: 8, borderLeft: `3px solid ${s.role === "counter" ? "#e05c5c" : s.role === "support" ? "#f2a65a" : "#5ccfa8"}` }}>
                  <div style={{ fontSize: 11, opacity: 0.6 }}>
                    {ROLE_LABELS[s.role] || s.role} · {s.project}/{s.kind}
                  </div>
                  <a style={{ cursor: "pointer" }} onClick={() => onOpenDoc(s.doc_id)}>{s.title || s.doc_id}</a>
                  {s.snippet && <div style={{ fontSize: 12, opacity: 0.7, marginTop: 2 }}>{s.snippet}</div>}
                </div>
              ))}
            </section>

            {/* Cómo lo encontró el agente */}
            {roles?._trace && Array.isArray(roles._trace) && roles._trace.length > 0 && (
              <details style={{ marginBottom: 14, fontSize: 12 }}>
                <summary style={{ cursor: "pointer" }}>Cómo exploró el agente ({roles._trace.length} pasos)</summary>
                <ol style={{ margin: "6px 0", paddingLeft: 20, opacity: 0.8 }}>
                  {roles._trace.map((t: { tool: string; args: Record<string, string> }, i: number) => (
                    <li key={i}><code>{t.tool}</code> {Object.entries(t.args || {}).map(([k, v]) => `${k}=${v}`).join(" ")}</li>
                  ))}
                </ol>
              </details>
            )}

            {detail.reviews.length > 0 && (
              <details style={{ marginBottom: 14, fontSize: 12 }} open>
                <summary style={{ cursor: "pointer" }}>Historial ({detail.reviews.length})</summary>
                <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
                  {detail.reviews.map((r, i) => (
                    <li key={i}>{r.created_at} · {r.reviewer}: {r.from_status} → {r.to_status} {r.note && `— ${r.note}`}</li>
                  ))}
                </ul>
              </details>
            )}

          </div>
        )}
        </div>
        {/* Barra de acciones al pie: fuera del scroll, no tapa nada */}
        {detail && (
          <div className="actionbar" style={{ borderTop: "1px solid #333", background: "#1e1e1e" }}>
            <span className="hint">marcar:</span>
            <button onClick={() => act(detail.id, "review", "interesting")}>interesante</button>
            <button onClick={() => act(detail.id, "review", "actionable")}>accionable</button>
            <button onClick={() => act(detail.id, "review", "discarded")}>descartar</button>
            <button className="primary" onClick={() => act(detail.id, "propose")}>📤 proponer</button>
            <span className="sep" />
            <button className="danger" onClick={() => borrar([detail.id])}>🗑</button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- raíz ----------

export default function LabApp() {
  const [authed, setAuthed] = useState(!!getToken());
  const [tab, setTab] = useState<"correr" | "bandeja" | "grafo">("correr");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [readerDoc, setReaderDoc] = useState<string | null>(null);

  useEffect(() => {
    if (!authed) return;
    fetch("/ui/manifest").then((r) => r.json()).then(setManifest).catch(() => {});
  }, [authed]);
  // Grafo de MIS candidatos (el de hallazgos publicados vive en el atlas).
  const [graphScope, setGraphScope] = useState<"mine" | "all">("mine");
  useEffect(() => {
    if (tab !== "grafo") return;
    pgJson<GraphPayload>(`/pg/graph?scope=${graphScope}`).then(setGraph).catch(() => setGraph(null));
  }, [tab, graphScope]);

  if (!authed) return <TokenGate onReady={() => setAuthed(true)} />;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <header className="actionbar" style={{ borderBottom: "1px solid #333", padding: "4px 10px" }}>
        <b style={{ fontSize: 12 }}>🔭 Lab</b>
        <button style={TAB_BTN} className={tab === "correr" ? "active" : ""} onClick={() => setTab("correr")}>Correr</button>
        <button style={TAB_BTN} className={tab === "bandeja" ? "active" : ""} onClick={() => setTab("bandeja")}>Bandeja</button>
        <button style={TAB_BTN} className={tab === "grafo" ? "active" : ""} onClick={() => setTab("grafo")}>Grafo</button>
        <span style={{ flex: 1 }} />
        <a href="/atlas/">atlas ↗</a>
        <a style={{ cursor: "pointer", opacity: 0.6 }} onClick={() => { localStorage.removeItem("lab.token"); setAuthed(false); }}>salir</a>
      </header>
      <StructuralNotice />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
        {tab === "correr" && <RunView manifest={manifest} />}
        {tab === "bandeja" && <TrayView onOpenDoc={setReaderDoc} />}
        {tab === "grafo" && (
          <Suspense fallback={<div className="loading">cargando…</div>}>
            {/* className="center" es lo que neutraliza el min-height:100vh del canvas
                (si no, el grafo se descuadra y se ve en una franja). */}
            <div className="center" style={{ minHeight: 0 }}>
              <div className="actionbar" style={{ borderBottom: "1px solid #333" }}>
                <button style={TAB_BTN} className={graphScope === "mine" ? "active" : ""} onClick={() => setGraphScope("mine")}>míos</button>
                <button style={TAB_BTN} className={graphScope === "all" ? "active" : ""} onClick={() => setGraphScope("all")}>de todos</button>
                <span style={{ opacity: 0.75 }}>
                  {graph ? `${graph.counts.nodes} nodos · ${graph.counts.edges} aristas` : ""}
                </span>
                <span style={{ opacity: 0.55 }}>
                  🟠 evidencia · 🔴 contraevidencia · 🟢 puente — arrastrá para mover, rueda para zoom,
                  <b> doble clic en el fondo para reencuadrar</b>
                </span>
              </div>
              {graph && graph.nodes.length > 0 ? (
                <GraphView
                  nodes={graph.nodes}
                  edges={graph.edges}
                  needsLayout={true}
                  view="discovery"
                  communities={false}
                  labelLevel="auto"
                  focus={null}
                  onNodeClick={(n: NodeData) => n.doc_id && setReaderDoc(n.doc_id)}
                />
              ) : (
                <div className="loading">
                  todavía no hay candidatos — corré una campaña en «Correr» y aparecen acá con su evidencia
                </div>
              )}
            </div>
          </Suspense>
        )}
      </div>
      {readerDoc && (
        <Suspense fallback={null}>
          <DocReader
            docId={readerDoc}
            onClose={() => setReaderDoc(null)}
            onNeighbors={(id) => window.open(`/atlas/#view=neighbors&nb=${encodeURIComponent(id)}`, "_blank", "noopener")}
            onWikilink={() => {}}
            onOpenDoc={(id) => setReaderDoc(id)}
          />
        </Suspense>
      )}
    </div>
  );
}
