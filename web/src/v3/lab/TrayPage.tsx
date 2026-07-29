import { AlertTriangle, Check, ChevronLeft, ChevronRight, ExternalLink, FileSearch, Filter, History, Inbox, Lightbulb, Send, ShieldAlert, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, formatDate, request, safeJson } from "../api";
import { queryParams, updateQuery } from "../router";
import { useSession } from "../session";
import type { CandidateDetail, CandidateRow } from "../types";
import { OPERATOR_HELP, ROLE_LABELS, STATUS_LABELS } from "./constants";

type CandidateResponse = { user: string; total: number; shown: number; offset: number; candidates: CandidateRow[] };
type DialogAction = { kind: "review" | "propose" | "delete"; ids: string[]; status?: string } | null;

function score(value: number | null) {
  return value == null ? "—" : value.toFixed(2);
}

function actionTitle(action: DialogAction) {
  if (!action) return "";
  if (action.kind === "delete") return `Eliminar ${action.ids.length} candidato${action.ids.length === 1 ? "" : "s"}`;
  if (action.kind === "propose") return "Proponer al flujo editorial";
  return `Marcar como ${STATUS_LABELS[action.status || ""] || action.status}`;
}

export default function TrayPage({ onDoc }: { onDoc: (id: string) => void }) {
  const { identity, mutate } = useSession();
  const initial = queryParams();
  const [scope, setScope] = useState<"mine" | "all">(initial.get("scope") === "all" ? "all" : "mine");
  const [status, setStatus] = useState(initial.get("status") || "");
  const [type, setType] = useState(initial.get("type") || "");
  const [query, setQuery] = useState(initial.get("q") || "");
  const [surprise, setSurprise] = useState(initial.get("min_surprise") || "");
  const [offset, setOffset] = useState(Number(initial.get("offset") || 0));
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<CandidateDetail | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [dialogAction, setDialogAction] = useState<DialogAction>(null);
  const [note, setNote] = useState("");
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const pageSize = 50;

  function listParams(nextOffset = offset, nextScope = scope) {
    const params = new URLSearchParams({ scope: nextScope, limit: String(pageSize), offset: String(nextOffset) });
    if (status) params.set("status", status);
    if (type) params.set("type", type);
    if (query.trim()) params.set("q", query.trim());
    if (surprise) params.set("min_surprise", surprise);
    return params;
  }

  async function load(nextOffset = offset, syncUrl = false, nextScope = scope) {
    setBusy(true);
    setError("");
    try {
      const data = await request<CandidateResponse>(`/pg/candidates?${listParams(nextOffset, nextScope)}`);
      setRows(data.candidates);
      setTotal(data.total ?? data.candidates.length);
      setOffset(nextOffset);
      setSelected(new Set());
      if (syncUrl) {
        const params = listParams(nextOffset, nextScope);
        if (detail) params.set("candidate", detail.id);
        const url = new URL(window.location.href);
        window.history.replaceState({}, "", `${url.pathname}?${params}`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function open(id: string) {
    setError("");
    try {
      const value = await request<CandidateDetail>(`/pg/candidate?id=${encodeURIComponent(id)}`);
      setDetail(value);
      updateQuery("candidate", id, true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  useEffect(() => {
    load(0, true);
    const candidate = initial.get("candidate");
    if (candidate) open(candidate);
  }, []);

  useEffect(() => {
    if (!dialogAction) return;
    dialogRef.current?.showModal();
  }, [dialogAction]);

  function closeDialog() {
    dialogRef.current?.close();
    setDialogAction(null);
    setNote("");
  }

  function canReview(candidate: CandidateRow | CandidateDetail) {
    return candidate.owner === identity?.user && ["candidate", "interesting", "actionable"].includes(candidate.status);
  }

  function canDelete(candidate: CandidateRow | CandidateDetail) {
    return candidate.owner === identity?.user && !["proposed", "importing", "imported"].includes(candidate.status);
  }

  async function confirmAction() {
    if (!dialogAction) return;
    setBusy(true);
    setError("");
    try {
      if (dialogAction.kind === "delete") {
        const result = await mutate<{ borrados: number; rechazados: { id: string; razón: string }[] }>("/pg/delete", { ids: dialogAction.ids });
        setMessage(`${result.borrados} eliminado${result.borrados === 1 ? "" : "s"}${result.rechazados.length ? ` · ${result.rechazados.length} rechazados` : ""}`);
        if (detail && dialogAction.ids.includes(detail.id)) {
          setDetail(null);
          updateQuery("candidate", null, true);
        }
      } else if (dialogAction.kind === "propose") {
        await mutate("/pg/propose", { id: dialogAction.ids[0], note });
        setMessage("Candidato propuesto al flujo editorial.");
      } else {
        await mutate("/pg/review", { id: dialogAction.ids[0], status: dialogAction.status, note });
        setMessage(`Candidato marcado como ${STATUS_LABELS[dialogAction.status || ""] || dialogAction.status}.`);
      }
      const activeId = detail?.id;
      closeDialog();
      await load(offset);
      if (activeId) await open(activeId).catch(() => undefined);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409 && detail) {
        await open(detail.id);
        setError("El candidato cambió en otra sesión. Recargamos su estado antes de continuar.");
      } else setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  function applyFilters(event: React.FormEvent) {
    event.preventDefault();
    load(0, true);
  }

  const roles = detail ? safeJson<Record<string, any> | null>(detail.roles_json, null) : null;
  const falsification = detail ? safeJson<Record<string, any> | null>(detail.falsification_json, null) : null;
  const flags = detail ? safeJson<Record<string, any>>(detail.flags_json, {}) : {};
  const malformed = detail && ((!roles && Boolean(detail.roles_json)) || (!falsification && Boolean(detail.falsification_json)));
  const ownDiscarded = rows.filter((candidate) => candidate.status === "discarded" && canDelete(candidate)).map((candidate) => candidate.id);

  return <main className="v3-tray-page">
    <header className="v3-tray-head"><div><p className="v3-eyebrow">Banco de hipótesis</p><h1>Bandeja de candidatos</h1></div><div className="v3-scope" role="group" aria-label="Ámbito"><button className={scope === "mine" ? "active" : ""} onClick={() => { setScope("mine"); load(0, true, "mine"); }}>Míos</button><button className={scope === "all" ? "active" : ""} onClick={() => { setScope("all"); load(0, true, "all"); }}>De todos</button></div></header>
    <form className="v3-tray-filters" onSubmit={applyFilters}><label><FileSearch /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar claim o título" maxLength={160} /></label><select aria-label="Operador" value={type} onChange={(event) => setType(event.target.value)}><option value="">Todos los operadores</option>{Object.entries(OPERATOR_HELP).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}</select><select aria-label="Estado" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">Todos los estados</option>{["candidate", "interesting", "actionable", "discarded", "proposed", "importing", "imported"].map((item) => <option key={item} value={item}>{STATUS_LABELS[item]}</option>)}</select><select aria-label="Sorpresa mínima" value={surprise} onChange={(event) => setSurprise(event.target.value)}><option value="">Cualquier sorpresa</option><option value="0.25">≥ 0,25</option><option value="0.5">≥ 0,50</option><option value="0.75">≥ 0,75</option></select><button className="v3-primary"><Filter /> Aplicar</button></form>
    {(error || message) && <div className={`v3-notice ${error ? "error" : "success"}`}>{error || message}</div>}
    <div className="v3-tray-layout">
      <aside className="v3-candidate-list">
        <div className="v3-list-tools"><span><Inbox /> {total} candidatos</span><button disabled={!selected.size} onClick={() => setDialogAction({ kind: "delete", ids: [...selected] })}><Trash2 /> Eliminar {selected.size || ""}</button><button disabled={!ownDiscarded.length} onClick={() => setDialogAction({ kind: "delete", ids: ownDiscarded })}>Limpiar descartados</button></div>
        {busy && !rows.length ? <div className="v3-skeleton tall" /> : rows.map((candidate) => {
          const itemFlags = safeJson<Record<string, unknown>>(candidate.flags_json, {});
          return <article key={candidate.id} className={detail?.id === candidate.id ? "active" : ""}>
            <input type="checkbox" aria-label={`Seleccionar ${candidate.title || candidate.id}`} disabled={!canDelete(candidate)} checked={selected.has(candidate.id)} onChange={() => setSelected((current) => { const next = new Set(current); next.has(candidate.id) ? next.delete(candidate.id) : next.add(candidate.id); return next; })} />
            <button onClick={() => open(candidate.id)}><span className="v3-candidate-meta"><i>{OPERATOR_HELP[candidate.discovery_type]?.label || candidate.discovery_type}</i><i className={`v3-status ${candidate.status}`}>{STATUS_LABELS[candidate.status] || candidate.status}</i></span><b>{candidate.title || candidate.id}</b><span className="v3-scoreline">Sorpresa {score(candidate.unexpectedness_score)} · juez {itemFlags.screen_passed === true ? "aprobó" : itemFlags.unscreened ? "sin screen" : "rechazó"}</span></button>
          </article>;
        })}
        <nav className="v3-pagination compact"><button disabled={offset === 0 || busy} onClick={() => load(Math.max(0, offset - pageSize), true)}><ChevronLeft /></button><span>{offset + 1}–{Math.min(offset + rows.length, total)} de {total}</span><button disabled={offset + rows.length >= total || busy} onClick={() => load(offset + pageSize, true)}><ChevronRight /></button></nav>
      </aside>

      <section className="v3-candidate-detail">
        {!detail ? <div className="v3-empty major"><Lightbulb /><h2>Elegí una hipótesis.</h2><p>Acá vas a ver qué afirma, de dónde sale y cómo intentó refutarse.</p></div> : <>
          <div className="v3-detail-scroll">
            <header className="v3-detail-head"><div><p className="v3-eyebrow">{OPERATOR_HELP[detail.discovery_type]?.label || detail.discovery_type}</p><h1>{detail.title || detail.id}</h1></div><span className={`v3-status ${detail.status}`}>{STATUS_LABELS[detail.status] || detail.status}</span><dl><div><dt>Novedad</dt><dd>{score(detail.novelty_score)}</dd></div><div><dt>Soporte</dt><dd>{score(detail.support_score)}</dd></div><div><dt>Sorpresa</dt><dd>{score(detail.unexpectedness_score)}</dd></div><div><dt>Modelo</dt><dd>{detail.generated_by || "—"}</dd></div><div><dt>Dueño</dt><dd>{detail.owner}</dd></div></dl></header>
            <div className="v3-detail-columns"><div className="v3-argument">
              <section><p className="v3-eyebrow">Qué afirma</p><div className="v3-claim">{detail.claim || "Sin claim: el juez no produjo una afirmación promovible."}</div></section>
              {detail.why_interesting && <section><p className="v3-eyebrow">Por qué importa</p><p>{detail.why_interesting}</p></section>}
              {roles && <section className="v3-structure"><p className="v3-eyebrow">Estructura del hallazgo</p>{roles.side_a && <p><b>Un lado:</b> {String(roles.side_a)}</p>}{roles.side_b && <p><b>El otro:</b> {String(roles.side_b)}</p>}{roles.intensity && <p><b>Intensidad:</b> {String(roles.intensity)}</p>}{roles.mediators && <p><b>Mediadores:</b> {JSON.stringify(roles.mediators)}</p>}{roles.role_mapping && <p><b>Mapeo de roles:</b> {typeof roles.role_mapping === "string" ? roles.role_mapping : JSON.stringify(roles.role_mapping)}</p>}{roles.breaking_point && <p><b>Dónde se rompe:</b> {String(roles.breaking_point)}</p>}{roles.next_step && <p><b>Próximo paso:</b> {String(roles.next_step)}</p>}</section>}
              {flags && (flags.fragile || flags.contradicted || flags.low_diversity || flags.hub_sources || flags.screen_reason) && <section className="v3-warnings"><p className="v3-eyebrow"><ShieldAlert /> Advertencias</p><ul>{flags.screen_reason && <li>El juez lo rechazó: {String(flags.screen_reason)}</li>}{flags.fragile && <li>Depende de una sola fuente y no sobrevivió la ablación.</li>}{flags.contradicted && <li>La búsqueda encontró documentos que lo contradicen.</li>}{flags.low_diversity && <li>La evidencia tiene poca diversidad de proyectos o tipos.</li>}{Array.isArray(flags.hub_sources) && flags.hub_sources.length > 0 && <li>Se apoya en documentos-hub: {flags.hub_sources.map((item: string) => item.split("/").pop()).join(", ")}.</li>}</ul></section>}
              {malformed && <details className="v3-raw-json"><summary>Datos estructurados no interpretables</summary><pre>{JSON.stringify({ roles_json: detail.roles_json, falsification_json: detail.falsification_json }, null, 2)}</pre></details>}
            </div>
            <aside className="v3-evidence-rail">
              {falsification && <section className={`v3-falsification ${falsification.passed ? "passed" : "failed"}`}><p className="v3-eyebrow">{falsification.passed ? <Check /> : <AlertTriangle />} Falsación</p><h2>{falsification.passed ? "Superada" : "No superada"}</h2>{falsification.source_ablation && <div><b>Prueba de ablación</b><p>{falsification.source_ablation.survives ? "El claim sobrevivió al quitar la fuente más fuerte." : "El claim cayó al quitar la fuente más fuerte."}</p>{falsification.source_ablation.reason && <small>{falsification.source_ablation.reason}</small>}</div>}{falsification.negative_search && <div><b>Contraevidencia</b><p>{falsification.negative_search.contradicted ? "Se encontraron contradicciones." : "No se encontraron contradicciones."}</p>{falsification.negative_search.reason && <small>{falsification.negative_search.reason}</small>}</div>}{falsification.diversity_check && <div><b>Diversidad</b><p>{(falsification.diversity_check.projects || []).join(", ") || "Sin proyectos declarados"}</p></div>}{falsification.stale_check?.stale_sources?.length > 0 && <div className="error">Hay fuentes que cambiaron desde la generación.</div>}</section>}
              <section><p className="v3-eyebrow">Evidencia · {detail.sources.length} documentos</p><div className="v3-source-list">{detail.sources.map((source, index) => <button key={`${source.doc_id}-${index}`} className={`role-${source.role}`} onClick={() => onDoc(source.doc_id)}><span>{ROLE_LABELS[source.role] || source.role} · {source.project}/{source.kind}</span><b>{source.title || source.doc_id}</b>{source.snippet && <p>{source.snippet}</p>}<ExternalLink /></button>)}</div></section>
              {Array.isArray((roles as any)?._trace) && <details><summary>Cómo exploró el agente ({(roles as any)._trace.length} pasos)</summary><ol className="v3-trace">{(roles as any)._trace.map((step: any, index: number) => <li key={index}><code>{step.tool}</code> {Object.entries(step.args || {}).map(([key, value]) => `${key}=${value}`).join(" ")}</li>)}</ol></details>}
              {detail.reviews.length > 0 && <details open><summary><History /> Historial ({detail.reviews.length})</summary><ol className="v3-history">{detail.reviews.map((review, index) => <li key={index}><time>{formatDate(review.created_at, true)}</time><b>{review.reviewer}</b><span>{STATUS_LABELS[review.from_status] || review.from_status} → {STATUS_LABELS[review.to_status] || review.to_status}</span>{review.note && <p>{review.note}</p>}</li>)}</ol></details>}
            </aside></div>
          </div>
          <footer className="v3-decision-bar"><span>Decisión editorial preliminar</span><button disabled={!canReview(detail)} onClick={() => setDialogAction({ kind: "review", ids: [detail.id], status: "interesting" })}>Interesante</button><button disabled={!canReview(detail)} onClick={() => setDialogAction({ kind: "review", ids: [detail.id], status: "actionable" })}>Accionable</button><button disabled={!canReview(detail)} onClick={() => setDialogAction({ kind: "review", ids: [detail.id], status: "discarded" })}>Descartar</button><button className="v3-primary" disabled={!canReview(detail)} onClick={() => setDialogAction({ kind: "propose", ids: [detail.id] })}><Send /> Proponer</button><button className="danger" disabled={!canDelete(detail)} onClick={() => setDialogAction({ kind: "delete", ids: [detail.id] })} title="Eliminar"><Trash2 /></button></footer>
        </>}
      </section>
    </div>

    <dialog ref={dialogRef} className="v3-dialog" onClose={() => setDialogAction(null)}><button className="v3-dialog-close" onClick={closeDialog}><X /><span>Cerrar</span></button><p className="v3-eyebrow">Confirmación</p><h2>{actionTitle(dialogAction)}</h2>{dialogAction?.kind === "delete" ? <p>Esta operación es definitiva y no puede deshacerse. Los candidatos ya propuestos o importados están protegidos por el servidor.</p> : <><p>{dialogAction?.kind === "propose" ? "Esto no publica el hallazgo: lo entrega al flujo editorial para revisión del dueño." : "La marca queda registrada en el historial del candidato."}</p><label>Nota opcional<textarea value={note} maxLength={2000} rows={5} onChange={(event) => setNote(event.target.value)} /></label><small>{note.length}/2000</small></>}<div className="v3-dialog-actions"><button onClick={closeDialog}>Cancelar</button><button className={dialogAction?.kind === "delete" ? "danger" : "v3-primary"} disabled={busy} onClick={confirmAction}>{dialogAction?.kind === "delete" ? <><Trash2 /> Eliminar</> : <><Check /> Confirmar</>}</button></div></dialog>
  </main>;
}
