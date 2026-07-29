import DOMPurify from "dompurify";
import { Archive, ChevronLeft, ChevronRight, FileText, Filter, Search } from "lucide-react";
import { startTransition, useEffect, useState } from "react";
import { formatDate, formatNumber, v2 } from "./api";
import { navigate, queryParams } from "./router";
import type { Bootstrap, DocMeta, Manifest, SearchHit } from "./types";
import { t, UI_LOCALE } from "../i18n";

function plainSnippet(value: string) {
  const clean = String(DOMPurify.sanitize(value || "", { ALLOWED_TAGS: [], ALLOWED_ATTR: [] }));
  const decoder = document.createElement("textarea");
  decoder.innerHTML = clean;
  return decoder.value.replace(/\*\*|`/g, "").replace(/\s+/g, " ").trim();
}

function Highlight({ value }: { value: string }) {
  return <p className="v3-snippet">{plainSnippet(value).split(/(»[^«]*«)/g).filter(Boolean).map((part, index) =>
    part.startsWith("»") && part.endsWith("«")
      ? <mark key={`${index}-${part}`}>{part.slice(1, -1)}</mark>
      : part
  )}</p>;
}

export function SearchPage({ bootstrap, manifest, onDoc }: {
  bootstrap: Bootstrap;
  manifest: Manifest | null;
  onDoc: (id: string) => void;
}) {
  const params = queryParams();
  const [query, setQuery] = useState(params.get("q") || "");
  const [project, setProject] = useState(params.get("project") || "");
  const [kind, setKind] = useState(params.get("kind") || "");
  const [limit, setLimit] = useState(Number(params.get("limit") || 40));
  const [offset, setOffset] = useState(Number(params.get("offset") || 0));
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [mode, setMode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load(nextOffset = offset) {
    if (!query.trim()) return;
    const url = new URLSearchParams({ q: query.trim(), limit: String(limit), offset: String(nextOffset) });
    if (project) url.set("project", project);
    if (kind) url.set("kind", kind);
    setBusy(true);
    setError("");
    try {
      const result = await v2<{ mode: string; query: string; results: SearchHit[] }>(`/search?${url}`);
      startTransition(() => {
        setHits(result.data.results);
        setMode(result.data.mode || t("desconocido", "unknown"));
        setOffset(nextOffset);
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { if (params.get("q")) load(); }, []);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const url = new URLSearchParams({ q: query.trim(), limit: String(limit), offset: "0" });
    if (project) url.set("project", project);
    if (kind) url.set("kind", kind);
    navigate("atlas", "buscar", url, true);
  }

  const rawProjects = manifest?.projects.slice().sort((a, b) => a.project.localeCompare(b.project)) || [];
  return <section className="v3-content-page">
    <header className="v3-page-head compact"><p className="v3-eyebrow">{t("Consulta transversal", "Cross-cutting search")}</p><h1>{t("Buscá una idea, no una carpeta.", "Search for an idea, not a folder.")}</h1><p>{t("Texto completo y vecindad semántica, con procedencia visible.", "Full text and semantic neighborhoods, with visible provenance.")}</p></header>
    <form className="v3-searchbar" onSubmit={submit}>
      <Search aria-hidden="true" />
      <input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={512} placeholder={t("¿Qué relación, concepto o problema querés encontrar?", "What relationship, concept, or problem do you want to find?")} autoFocus />
      <button className="v3-primary" disabled={!query.trim() || busy}>{busy ? t("Buscando…", "Searching…") : t("Buscar", "Search")}</button>
    </form>
    <details className="v3-filter-disclosure" open={Boolean(project || kind)}><summary><Filter /> {t("Afinar consulta", "Refine search")}</summary>
      <div className="v3-filter-row">
        <label>{t("Proyecto", "Project")}<select value={project} onChange={(event) => setProject(event.target.value)}><option value="">{t("Todos", "All")}</option>{rawProjects.map((item) => <option key={item.slug} value={item.project}>{item.project}</option>)}</select></label>
        <label>{t("Clase", "Class")}<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="">{t("Todas", "All")}</option>{Object.keys(bootstrap.counts_by_kind).sort().map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>{t("Resultados", "Results")}<select value={limit} onChange={(event) => setLimit(Number(event.target.value))}><option>20</option><option>40</option><option>80</option></select></label>
      </div>
    </details>
    {mode && <div className="v3-query-status"><span>{t("Modo de esta consulta", "Search mode")}</span><b>{mode}</b><span>{bootstrap.system.vectorized_docs.toLocaleString(UI_LOCALE)} {t("documentos vectorizados en el índice", "vectorized documents in the index")}</span></div>}
    {error && <div className="v3-notice error">{error}</div>}
    <div className="v3-results" aria-busy={busy}>{busy ? <div className="v3-skeleton tall" /> : hits.map((hit, index) => <button key={`${hit.doc_id}-${index}`} onClick={() => onDoc(hit.doc_id)}>
      <span className="v3-result-rank">{String(offset + index + 1).padStart(2, "0")}</span>
      <div><p className="v3-eyebrow">{hit.kind} · {hit.project}</p><h2>{hit.title}</h2>{hit.heading && <small>{hit.heading}</small>}<Highlight value={hit.snippet} /><code>{hit.doc_id}</code></div>
    </button>)}</div>
  </section>;
}

export function ArchivePage({ bootstrap, onDoc }: { bootstrap: Bootstrap; onDoc: (id: string) => void }) {
  const params = queryParams();
  const [filters, setFilters] = useState({
    q: params.get("q") || "",
    project: params.get("project") || "",
    kind: params.get("kind") || "",
    abstraction: params.get("abstraction") || "",
    prefix: params.get("prefix") || "",
  });
  const [docs, setDocs] = useState<DocMeta[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(Number(params.get("offset") || 0));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pageSize = 100;

  async function load(nextFilters = filters, nextOffset = offset, syncUrl = false) {
    const url = new URLSearchParams({ limit: String(pageSize), offset: String(nextOffset) });
    Object.entries(nextFilters).forEach(([key, value]) => value && url.set(key, value));
    if (syncUrl) {
      navigate("atlas", "archivo", url, true);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await v2<DocMeta[]>(`/tree?${url}`);
      startTransition(() => {
        setDocs(result.data);
        setTotal(result.meta.total || 0);
        setOffset(nextOffset);
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); }, []);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    load(filters, 0, true);
  }

  return <section className="v3-content-page">
    <header className="v3-page-head compact"><p className="v3-eyebrow">{t("Recorrido documental", "Document exploration")}</p><h1>{t("El archivo, sin perder contexto.", "The archive, without losing context.")}</h1><p>{formatNumber(total || bootstrap.system.total_docs)} {t("documentos alcanzables desde una misma mesa de lectura.", "documents accessible from a single reading desk.")}</p></header>
    <form className="v3-archive-tools" onSubmit={submit}>
      <label className="wide">{t("Título o ruta", "Title or path")}<input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} maxLength={160} placeholder={t("paper, src/audio, informe…", "paper, src/audio, report…")} /></label>
      <label>{t("Proyecto", "Project")}<select value={filters.project} onChange={(event) => setFilters({ ...filters, project: event.target.value })}><option value="">{t("Todos", "All")}</option>{bootstrap.projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.title.replace(/ — .*/, "")}</option>)}</select></label>
      <label>{t("Abstracción", "Abstraction")}<select value={filters.abstraction} onChange={(event) => setFilters({ ...filters, abstraction: event.target.value })}><option value="">{t("Todas", "All")}</option>{Object.keys(bootstrap.counts_by_abstraction).sort().map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>{t("Clase", "Class")}<select value={filters.kind} onChange={(event) => setFilters({ ...filters, kind: event.target.value })}><option value="">{t("Todas", "All")}</option>{Object.keys(bootstrap.counts_by_kind).sort().map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>{t("Prefijo", "Prefix")}<input value={filters.prefix} onChange={(event) => setFilters({ ...filters, prefix: event.target.value })} placeholder="project/docs" /></label>
      <button className="v3-primary"><Filter /> {t("Aplicar", "Apply")}</button>
    </form>
    <div className="v3-table-summary"><Archive /> <b>{formatNumber(total)}</b> {t("coincidencias", "matches")} <span>· {offset + 1}–{Math.min(offset + docs.length, total)}</span></div>
    {error && <div className="v3-notice error">{error}</div>}
    <div className="v3-file-table" role="table" aria-busy={busy}>{busy ? <div className="v3-skeleton tall" /> : docs.map((doc) => <button role="row" key={doc.doc_id} onClick={() => onDoc(doc.doc_id)}>
      <FileText /><span className={`v3-level ${doc.abstraction}`}>{doc.abstraction}</span><b>{doc.title}</b><code>{doc.path_rel}</code><span>{doc.kind}</span><time>{formatDate(doc.mtime)}</time>
    </button>)}</div>
    <nav className="v3-pagination" aria-label={t("Paginación", "Pagination")}><button disabled={offset === 0 || busy} onClick={() => load(filters, Math.max(0, offset - pageSize), true)}><ChevronLeft /> {t("Anterior", "Previous")}</button><span>{t("Página", "Page")} {Math.floor(offset / pageSize) + 1} {t("de", "of")} {Math.max(1, Math.ceil(total / pageSize))}</span><button disabled={offset + docs.length >= total || busy} onClick={() => load(filters, offset + pageSize, true)}>{t("Siguiente", "Next")} <ChevronRight /></button></nav>
  </section>;
}
