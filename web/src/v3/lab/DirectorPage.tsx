import DOMPurify from "dompurify";
import { marked } from "marked";
import { Activity, Clock3, FileText, KeyRound, Play, RefreshCw, Shield, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { formatDate, request } from "../api";
import { useSession } from "../session";
import type { Campaign, DirectorStatus } from "../types";

function safeDigest(value: string) {
  const noImages = value.replace(/!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/g, "[$1 — imagen externa omitida]($2)");
  return DOMPurify.sanitize(marked.parse(noImages, { async: false }) as string, {
    FORBID_TAGS: ["img", "iframe", "object", "style", "form", "input"],
    USE_PROFILES: { html: true },
  });
}

function plainTitle(value: string) {
  return value.replace(/[*_`#]+/g, "").replace(/\s+/g, " ").trim();
}

export default function DirectorPage() {
  const { mutate } = useSession();
  const [status, setStatus] = useState<DirectorStatus | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [password, setPassword] = useState("");
  const [digest, setDigest] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const [director, history] = await Promise.all([
        request<DirectorStatus>("/pg/director"),
        request<{ campaigns: Campaign[] }>("/pg/campaigns"),
      ]);
      setStatus(director);
      setCampaigns(history.campaigns);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  useEffect(() => {
    refresh();
    const timer = window.setInterval(() => { if (!document.hidden) refresh(); }, 8000);
    return () => {
      window.clearInterval(timer);
      setPassword("");
    };
  }, []);

  async function fire(event: React.FormEvent) {
    event.preventDefault();
    if (!password) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await mutate("/pg/director", { password });
      setMessage("Solicitud encolada. El watcher iniciará el Director cuando corresponda.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }

  async function loadDigest() {
    setError("");
    try {
      const data = await request<{ markdown: string }>("/pg/digest");
      setDigest(data.markdown);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return <main className="v3-lab-page v3-director">
    <header className="v3-page-head compact split"><div><p className="v3-eyebrow">Control plane</p><h1>Director autónomo</h1><p>Orquesta campañas y juicio; la promoción continúa siendo una decisión editorial del dueño.</p></div><div className={`v3-director-state ${status?.running ? "running" : "idle"}`}><Activity /><span><small>Estado</small><b>{status?.running ? "Corriendo" : "Inactivo"}</b></span></div></header>
    <div className="v3-director-grid">
      <section className="v3-director-control"><p className="v3-eyebrow"><Shield /> Operación privilegiada</p><h2>Solicitar un ciclo completo</h2><p>Este botón no ejecuta comandos ni publica hallazgos. Valida la contraseña y deja una solicitud de parámetros fijos para el watcher root.</p><form onSubmit={fire}><label><KeyRound /> Contraseña del Director<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="off" /></label><button className="v3-primary large" disabled={!password || busy || status?.running}><Play /> {busy ? "Encolando…" : status?.running ? "Director en ejecución" : "Encolar Director"}</button></form><div className="v3-quota"><Clock3 /><span>Cinco intentos por hora y diez minutos de cooldown entre solicitudes. La contraseña nunca se persiste.</span></div>{message && <div className="v3-notice success">{message}</div>}{error && <div className="v3-notice error">{error}</div>}</section>
      <aside className="v3-digest-card"><p className="v3-eyebrow"><Sparkles /> Último digest</p>{status?.last_digest ? <><h2>{plainTitle(status.last_digest.veredicto || status.last_digest.title || "Digest disponible")}</h2><time>{formatDate(status.last_digest.mtime, true)}</time><button onClick={loadDigest}><FileText /> Leer digest</button></> : <div className="v3-empty">Todavía no se publicó un digest.</div>}<button className="quiet" onClick={refresh}><RefreshCw /> Actualizar estado</button></aside>
    </div>
    <section className="v3-campaign-history"><p className="v3-eyebrow">Historial de campañas</p><h2>Actividad del Director</h2>{campaigns.length === 0 ? <div className="v3-empty">Sin campañas registradas.</div> : <div>{campaigns.map((campaign) => <article key={campaign.id}><span className={`v3-status ${campaign.status}`}>{campaign.status}</span><b>#{campaign.id} · {campaign.name}</b><small>{campaign.owner} · {formatDate(campaign.created_at, true)}</small></article>)}</div>}</section>
    {digest !== null && <section className="v3-digest-reader"><header><div><p className="v3-eyebrow">Digest más reciente</p><h2>Lectura editorial</h2></div><button onClick={() => setDigest(null)}>Cerrar</button></header><div className="v3-prose" dangerouslySetInnerHTML={{ __html: safeDigest(digest) }} /></section>}
  </main>;
}
