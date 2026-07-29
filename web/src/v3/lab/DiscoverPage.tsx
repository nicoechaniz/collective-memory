import { Activity, Bot, Check, ChevronDown, Clock3, FolderSearch, Play, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, formatDate, request, safeJson } from "../api";
import { useSession } from "../session";
import type { Job, Manifest, OperatorConfig } from "../types";
import { OPERATOR_HELP, STATUS_LABELS, STRUCTURAL_NOTICE, STRUCTURAL_ONLY } from "./constants";
import { t, UI_LOCALE } from "../../i18n";

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
    setMessage(t("Encolando…", "Queueing…"));
    setError("");
    const body = mode === "agent"
      ? { task: task.trim(), ...(provider ? { provider } : {}) }
      : { operators: [...operators], limit, projects: [...projects], ...(provider ? { provider } : {}) };
    try {
      const result = await mutate<{ job_id: number; status: string }>("/pg/run", body);
      setMessage(`${t("Campaña", "Campaign")} #${result.job_id} ${t("encolada", "queued")}.`);
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
    <header className="v3-page-head compact split"><div><p className="v3-eyebrow">{t("Nueva campaña", "New campaign")}</p><h1>{t("Convertí proximidad en hipótesis.", "Turn proximity into hypotheses.")}</h1><p>{t("Nada entra al mapa sin revisión y promoción editorial.", "Nothing enters the map without review and editorial promotion.")}</p></div><div className="v3-policy"><Activity /><span>{structural ? <><b>{t("Modo estructural sin LLM.", "Structural mode without an LLM.")}</b> {STRUCTURAL_NOTICE}</> : <><b>{t("GPU con prioridad productiva.", "GPU reserved for production.")}</b> {t("La campaña puede esperar o ser preemptada por Psicopompo.", "The campaign may wait or be preempted by Psicopompo.")}</>}</span></div></header>

    <div className="v3-mode-switch" role="tablist" aria-label={t("Modo de descubrimiento", "Discovery mode")}>
      <button role="tab" aria-selected={mode === "operators"} className={mode === "operators" ? "active" : ""} onClick={() => setMode("operators")}><SlidersHorizontal /><span><b>{t("Barrido por operadores", "Operator sweep")}</b><small>{t("Seis lentes reproducibles sobre el corpus.", "Six reproducible lenses over the corpus.")}</small></span></button>
      <button role="tab" aria-selected={mode === "agent"} className={mode === "agent" ? "active" : ""} disabled={structural} title={structural ? t("No disponible en el modo estructural sin LLM", "Unavailable in structural mode without an LLM") : undefined} onClick={() => setMode("agent")}><Bot /><span><b>{t("Pedido dirigido", "Directed request")}</b><small>{structural ? t("Desactivado: requiere inferencia LLM en vivo.", "Disabled: requires live LLM inference.") : t("Un agente explora a partir de tu pregunta.", "An agent explores from your question.")}</small></span></button>
    </div>

    <div className="v3-campaign-layout">
      <section className="v3-campaign-builder">
        {mode === "agent" ? <div className="v3-agent-task"><label>{t("¿Qué querés descubrir?", "What do you want to discover?")}<textarea rows={7} maxLength={2000} value={task} onChange={(event) => setTask(event.target.value)} placeholder={t("Ej.: encontrá una relación entre dos investigaciones que no se citan y explicá dónde podría romperse.", "Example: find a relationship between two studies that do not cite each other and explain where it might break down.")} /></label><span>{task.length}/2000</span></div> : <>
          <div className="v3-section-title"><div><p className="v3-eyebrow">{t("Lentes", "Lenses")}</p><h2>{t("Operadores de descubrimiento", "Discovery operators")}</h2></div><span>{operators.size} {t("seleccionados", "selected")}</span></div>
          <div className="v3-operator-grid">{(config?.operators || Object.keys(OPERATOR_HELP)).map((id) => {
            const help = OPERATOR_HELP[id] || { label: id, short: t("Operador disponible en el servidor.", "Operator available on the server."), long: t("Sin descripción local.", "No local description."), icon: Sparkles };
            const Icon = help.icon;
            return <label key={id} className={operators.has(id) ? "selected" : ""}><input type="checkbox" checked={operators.has(id)} onChange={() => toggleOperator(id)} /><Icon /><span><b>{help.label}</b><small>{help.short}</small><details><summary>{t("Cómo piensa", "How it reasons")}</summary><p>{help.long}</p></details></span>{operators.has(id) && <Check className="check" />}</label>;
          })}</div>
          <details className="v3-project-picker"><summary><FolderSearch /> {t("Proyectos", "Projects")} <b>{projects.size ? `${projects.size}/10` : t("todos", "all")}</b><ChevronDown /></summary><div className="v3-project-picker-body"><label className="v3-inline-search"><Search /><input value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder={t("Filtrar proyectos", "Filter projects")} /></label><div className="v3-project-actions"><button onClick={() => setProjects(new Set())}>{t("Usar todos", "Use all")}</button><span>{t("Vacío significa corpus completo.", "Empty means the complete corpus.")}</span></div><div className="v3-project-options">{visibleProjects.map((item) => <button key={item.slug} className={projects.has(item.project) ? "active" : ""} disabled={!projects.has(item.project) && projects.size >= 10} onClick={() => toggleProject(item.project)}><span>{item.project}</span><small>{item.count.toLocaleString(UI_LOCALE)} docs</small></button>)}</div></div></details>
          <label className="v3-limit">{t("Máximo de candidatos", "Maximum candidates")} <input type="range" min={1} max={config?.limits.limit_max || 30} value={limit} onChange={(event) => setLimit(Number(event.target.value))} /><output>{limit}</output></label>
        </>}
        {structural ? <div className="v3-judge v3-structural-judge"><p className="v3-eyebrow">{t("Validación", "Validation")}</p><b>{t("Sin juez LLM", "No LLM judge")}</b><small>{t("Los resultados quedan marcados como precandidatos no promovibles hasta una revisión posterior.", "Results are marked as non-promotable pre-candidates until a later review.")}</small></div> : <div className="v3-judge"><p className="v3-eyebrow">{t("Modelo juez", "Judge model")}</p>{config?.judges.map((judge) => <label key={judge.id} className={provider === judge.id ? "selected" : ""}><input type="radio" name="judge" checked={provider === judge.id} onChange={() => setProvider(judge.id)} /><span><b>{judge.label}</b><small>{judge.desc}</small></span></label>)}</div>}
      </section>

      <aside className="v3-campaign-summary"><p className="v3-eyebrow">{t("Antes de correr", "Before running")}</p><h2>{t("Resumen", "Summary")}</h2><dl><div><dt>{t("Modo", "Mode")}</dt><dd>{mode === "agent" ? t("Pedido dirigido", "Directed request") : structural ? t("Estructural", "Structural") : t("Operadores", "Operators")}</dd></div>{mode === "operators" && <><div><dt>{t("Lentes", "Lenses")}</dt><dd>{operators.size}</dd></div><div><dt>{t("Alcance", "Scope")}</dt><dd>{projects.size ? `${projects.size} ${t("proyectos", "projects")}` : t("todo el corpus", "entire corpus")}</dd></div><div><dt>{t("Tope", "Limit")}</dt><dd>{limit} {t("candidatos", "candidates")}</dd></div></>}<div><dt>{t("Juez", "Judge")}</dt><dd>{structural ? t("No se usa", "Not used") : config?.judges.find((item) => item.id === provider)?.label || t("cargando", "loading")}</dd></div></dl>{config && <div className="v3-quota"><Clock3 /><span>{t("Máximo", "Maximum")} {config.limits.queued_per_user} {t("jobs activos y", "active jobs and")} {config.limits.jobs_per_day} {t("por día.", "per day.")}</span></div>}<button className="v3-primary large" disabled={!valid || (!structural && !provider) || busy} onClick={run}><Play /> {busy ? t("Encolando…", "Queueing…") : structural ? t("Generar precandidatos", "Generate pre-candidates") : t("Iniciar descubrimiento", "Start discovery")}</button>{message && <div className="v3-notice success">{message}</div>}{error && <div className="v3-notice error">{error}</div>}</aside>
    </div>

    <section className="v3-jobs"><div className="v3-section-title"><div><p className="v3-eyebrow">{t("Actividad propia", "Your activity")}</p><h2>{t("Jobs recientes", "Recent jobs")}</h2></div><button onClick={refreshJobs}><Activity /> {t("Actualizar", "Refresh")}</button></div>{jobs.length === 0 ? <div className="v3-empty">{t("Todavía no hay campañas en esta bandeja.", "There are no campaigns in this tray yet.")}</div> : <div className="v3-job-list">{jobs.map((job) => {
      const params = safeJson<Record<string, unknown>>(job.params_json, {});
      const jobMode = params.mode === "agent" || params.task ? t("agente", "agent") : t("operadores", "operators");
      const detail = params.mode === "agent" || params.task ? String(params.task || t("pedido dirigido", "directed request")) : (Array.isArray(params.operators) ? params.operators.map((item) => OPERATOR_HELP[String(item)]?.label || item).join(", ") : t("barrido", "sweep"));
      return <article key={job.id}><span className={`v3-status ${job.status}`}>{STATUS_LABELS[job.status] || job.status}</span><b>#{job.id} · {jobMode}</b><p>{detail}</p><dl><div><dt>{t("Creado", "Created")}</dt><dd>{formatDate(job.created_at, true)}</dd></div><div><dt>{t("Final", "Finished")}</dt><dd>{formatDate(job.finished_at, true)}</dd></div></dl>{job.error && <small className="error">{job.error}</small>}</article>;
    })}</div>}</section>
  </main>;
}
