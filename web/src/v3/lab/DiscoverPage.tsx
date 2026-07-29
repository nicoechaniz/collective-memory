import { Activity, Bot, Check, ChevronDown, Clock3, FolderSearch, Play, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, formatDate, request, safeJson } from "../api";
import { useSession } from "../session";
import type { Job, Manifest, OperatorConfig } from "../types";
import { OPERATOR_HELP, STATUS_LABELS, STRUCTURAL_NOTICE, STRUCTURAL_ONLY } from "./constants";

type Mode = "agent" | "operators";

function storedSet(key: string, fallback: string[]) {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null");
    return new Set<string>(Array.isArray(value) ? value : fallback);
  } catch {
    return new Set<string>(fallback);
  }
}

export default function DiscoverPage({ manifest }: { manifest: Manifest | null }) {
  const { mutate } = useSession();
  const [mode, setMode] = useState<Mode>(() => !STRUCTURAL_ONLY && sessionStorage.getItem("v3.discovery.mode") === "agent" ? "agent" : "operators");
  const [task, setTask] = useState("");
  const [operators, setOperators] = useState<Set<string>>(() => storedSet("v3.discovery.operators", ["latent_bridge", "outlier"]));
  const [projects, setProjects] = useState<Set<string>>(() => storedSet("v3.discovery.projects", []));
  const [projectQuery, setProjectQuery] = useState("");
  const [limit, setLimit] = useState(() => Math.min(30, Math.max(1, Number(sessionStorage.getItem("v3.discovery.limit") || 12))));
  const [config, setConfig] = useState<OperatorConfig | null>(null);
  const [provider, setProvider] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refreshJobs() {
    try {
      const data = await request<{ jobs: Job[] }>("/pg/jobs");
      setJobs(data.jobs);
    } catch {
      // La cola conserva el último estado visible ante una falla transitoria.
    }
  }

  useEffect(() => {
    request<OperatorConfig>("/pg/operators").then((value) => {
      setConfig(value);
      setProvider(value.default_judge);
      if (STRUCTURAL_ONLY || value.mode === "structural-only") setMode("operators");
      setOperators((current) => new Set([...current].filter((item) => value.operators.includes(item))));
    }).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
    refreshJobs();
    const timer = window.setInterval(() => { if (!document.hidden) refreshJobs(); }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    sessionStorage.setItem("v3.discovery.mode", mode);
    sessionStorage.setItem("v3.discovery.operators", JSON.stringify([...operators]));
    sessionStorage.setItem("v3.discovery.projects", JSON.stringify([...projects]));
    sessionStorage.setItem("v3.discovery.limit", String(limit));
  }, [mode, operators, projects, limit]);

  function toggleOperator(id: string) {
    setOperators((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function toggleProject(id: string) {
    setProjects((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else if (next.size < 10) next.add(id);
      return next;
    });
  }

  async function run() {
    setBusy(true);
    setMessage("Encolando…");
    setError("");
    const body = mode === "agent"
      ? { task: task.trim(), ...(provider ? { provider } : {}) }
      : { operators: [...operators], limit, projects: [...projects], ...(provider ? { provider } : {}) };
    try {
      const result = await mutate<{ job_id: number; status: string }>("/pg/run", body);
      setMessage(`Campaña #${result.job_id} encolada.`);
      if (mode === "agent") setTask("");
      await refreshJobs();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : String(reason));
      setMessage("");
    } finally {
      setBusy(false);
    }
  }

  const structural = STRUCTURAL_ONLY || config?.mode === "structural-only";
  const valid = mode === "agent" ? Boolean(task.trim()) : operators.size > 0;
  const visibleProjects = (manifest?.projects || []).filter((item) => item.project.toLowerCase().includes(projectQuery.toLowerCase()));
  return <main className="v3-lab-page v3-discover">
    <header className="v3-page-head compact split"><div><p className="v3-eyebrow">Nueva campaña</p><h1>Convertí proximidad en hipótesis.</h1><p>Nada entra al mapa sin revisión y promoción editorial.</p></div><div className="v3-policy"><Activity /><span>{structural ? <><b>Modo estructural sin LLM.</b> {STRUCTURAL_NOTICE}</> : <><b>GPU con prioridad productiva.</b> La campaña puede esperar o ser preemptada por Psicopompo.</>}</span></div></header>

    <div className="v3-mode-switch" role="tablist" aria-label="Modo de descubrimiento">
      <button role="tab" aria-selected={mode === "operators"} className={mode === "operators" ? "active" : ""} onClick={() => setMode("operators")}><SlidersHorizontal /><span><b>Barrido por operadores</b><small>Seis lentes reproducibles sobre el corpus.</small></span></button>
      <button role="tab" aria-selected={mode === "agent"} className={mode === "agent" ? "active" : ""} disabled={structural} title={structural ? "No disponible en el modo estructural sin LLM" : undefined} onClick={() => setMode("agent")}><Bot /><span><b>Pedido dirigido</b><small>{structural ? "Desactivado: requiere inferencia LLM en vivo." : "Un agente explora a partir de tu pregunta."}</small></span></button>
    </div>

    <div className="v3-campaign-layout">
      <section className="v3-campaign-builder">
        {mode === "agent" ? <div className="v3-agent-task"><label>¿Qué querés descubrir?<textarea rows={7} maxLength={2000} value={task} onChange={(event) => setTask(event.target.value)} placeholder="Ej.: encontrá una relación entre dos investigaciones que no se citan y explicá dónde podría romperse." /></label><span>{task.length}/2000</span></div> : <>
          <div className="v3-section-title"><div><p className="v3-eyebrow">Lentes</p><h2>Operadores de descubrimiento</h2></div><span>{operators.size} seleccionados</span></div>
          <div className="v3-operator-grid">{(config?.operators || Object.keys(OPERATOR_HELP)).map((id) => {
            const help = OPERATOR_HELP[id] || { label: id, short: "Operador disponible en el servidor.", long: "Sin descripción local.", icon: Sparkles };
            const Icon = help.icon;
            return <label key={id} className={operators.has(id) ? "selected" : ""}><input type="checkbox" checked={operators.has(id)} onChange={() => toggleOperator(id)} /><Icon /><span><b>{help.label}</b><small>{help.short}</small><details><summary>Cómo piensa</summary><p>{help.long}</p></details></span>{operators.has(id) && <Check className="check" />}</label>;
          })}</div>
          <details className="v3-project-picker"><summary><FolderSearch /> Proyectos <b>{projects.size ? `${projects.size}/10` : "todos"}</b><ChevronDown /></summary><div className="v3-project-picker-body"><label className="v3-inline-search"><Search /><input value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder="Filtrar proyectos" /></label><div className="v3-project-actions"><button onClick={() => setProjects(new Set())}>Usar todos</button><span>Vacío significa corpus completo.</span></div><div className="v3-project-options">{visibleProjects.map((item) => <button key={item.slug} className={projects.has(item.project) ? "active" : ""} disabled={!projects.has(item.project) && projects.size >= 10} onClick={() => toggleProject(item.project)}><span>{item.project}</span><small>{item.count.toLocaleString("es-AR")} docs</small></button>)}</div></div></details>
          <label className="v3-limit">Máximo de candidatos <input type="range" min={1} max={config?.limits.limit_max || 30} value={limit} onChange={(event) => setLimit(Number(event.target.value))} /><output>{limit}</output></label>
        </>}
        {structural ? <div className="v3-judge v3-structural-judge"><p className="v3-eyebrow">Validación</p><b>Sin juez LLM</b><small>Los resultados quedan marcados como precandidatos no promovibles hasta una revisión posterior.</small></div> : <div className="v3-judge"><p className="v3-eyebrow">Modelo juez</p>{config?.judges.map((judge) => <label key={judge.id} className={provider === judge.id ? "selected" : ""}><input type="radio" name="judge" checked={provider === judge.id} onChange={() => setProvider(judge.id)} /><span><b>{judge.label}</b><small>{judge.desc}</small></span></label>)}</div>}
      </section>

      <aside className="v3-campaign-summary"><p className="v3-eyebrow">Antes de correr</p><h2>Resumen</h2><dl><div><dt>Modo</dt><dd>{mode === "agent" ? "Pedido dirigido" : structural ? "Estructural" : "Operadores"}</dd></div>{mode === "operators" && <><div><dt>Lentes</dt><dd>{operators.size}</dd></div><div><dt>Alcance</dt><dd>{projects.size ? `${projects.size} proyectos` : "todo el corpus"}</dd></div><div><dt>Tope</dt><dd>{limit} candidatos</dd></div></>}<div><dt>Juez</dt><dd>{structural ? "No se usa" : config?.judges.find((item) => item.id === provider)?.label || "cargando"}</dd></div></dl>{config && <div className="v3-quota"><Clock3 /><span>Máximo {config.limits.queued_per_user} jobs activos y {config.limits.jobs_per_day} por día.</span></div>}<button className="v3-primary large" disabled={!valid || (!structural && !provider) || busy} onClick={run}><Play /> {busy ? "Encolando…" : structural ? "Generar precandidatos" : "Iniciar descubrimiento"}</button>{message && <div className="v3-notice success">{message}</div>}{error && <div className="v3-notice error">{error}</div>}</aside>
    </div>

    <section className="v3-jobs"><div className="v3-section-title"><div><p className="v3-eyebrow">Actividad propia</p><h2>Jobs recientes</h2></div><button onClick={refreshJobs}><Activity /> Actualizar</button></div>{jobs.length === 0 ? <div className="v3-empty">Todavía no hay campañas en esta bandeja.</div> : <div className="v3-job-list">{jobs.map((job) => {
      const params = safeJson<Record<string, unknown>>(job.params_json, {});
      const jobMode = params.mode === "agent" || params.task ? "agente" : "operadores";
      const detail = jobMode === "agente" ? String(params.task || "pedido dirigido") : (Array.isArray(params.operators) ? params.operators.map((item) => OPERATOR_HELP[String(item)]?.label || item).join(", ") : "barrido");
      return <article key={job.id}><span className={`v3-status ${job.status}`}>{STATUS_LABELS[job.status] || job.status}</span><b>#{job.id} · {jobMode}</b><p>{detail}</p><dl><div><dt>Creado</dt><dd>{formatDate(job.created_at, true)}</dd></div><div><dt>Final</dt><dd>{formatDate(job.finished_at, true)}</dd></div></dl>{job.error && <small className="error">{job.error}</small>}</article>;
    })}</div>}</section>
  </main>;
}
