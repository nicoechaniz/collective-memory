import { useEffect, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { getJson, type DocPayload } from "./api";
import { t } from "./i18n";

const MD_KINDS = new Set(["map", "discovery", "synthesis", "biblioteca", "source", "fs_doc"]);

type Props = {
  docId: string;
  onClose: () => void;
  onNeighbors: (docId: string) => void;
  onWikilink: (name: string) => void;
  onOpenDoc: (docId: string) => void; // navegación determinista: anchors data-doc de las síntesis
};

function renderMarkdown(body: string): string {
  const html = marked.parse(body, { async: false }) as string;
  // Wikilinks [[x]] → anchors clickeables (se inyecta ANTES de sanitizar; data-* pasa DOMPurify).
  const linked = html.replace(/\[\[([^\]<>|#]{1,160})\]\]/g, (_m, t: string) => {
    const safe = t.replace(/"/g, "&quot;");
    return `<a class="wikilink" data-wl="${safe}">${t}</a>`;
  });
  // Sanitización: barrera primaria contra XSS almacenado (el corpus contiene HTML de terceros).
  return DOMPurify.sanitize(linked, { FORBID_TAGS: ["style", "form", "input", "iframe"], USE_PROFILES: { html: true } });
}

function loadReaderWidth(): number {
  const w = parseInt(localStorage.getItem("atlas.reader") || "0", 10);
  return w >= 320 && w <= window.innerWidth * 0.8 ? w : Math.min(680, window.innerWidth * 0.55);
}

export default function DocReader({ docId, onClose, onNeighbors, onWikilink, onOpenDoc }: Props) {
  const [doc, setDoc] = useState<DocPayload | null>(null);
  const [error, setError] = useState("");
  const [width, setWidth] = useState(loadReaderWidth);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const dragging = useRef(false);

  useEffect(() => {
    function onMove(e: PointerEvent) {
      if (!dragging.current) return;
      setWidth(Math.min(Math.max(window.innerWidth - e.clientX, 320), window.innerWidth * 0.8));
    }
    function onUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setWidth((w) => {
        localStorage.setItem("atlas.reader", String(Math.round(w)));
        return w;
      });
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  useEffect(() => {
    setDoc(null);
    setError("");
    getJson<DocPayload>(`/doc?id=${encodeURIComponent(docId)}`)
      .then(setDoc)
      .catch((e) => setError(String(e)));
  }, [docId]);

  const isMd = doc ? MD_KINDS.has(doc.kind) || /\.(md|markdown)$/i.test(doc.doc_id) : false;

  useEffect(() => {
    if (!doc || !isMd || !bodyRef.current) return;
    bodyRef.current.innerHTML = renderMarkdown(doc.body);
  }, [doc, isMd]);

  function onBodyClick(e: React.MouseEvent) {
    const a = (e.target as HTMLElement).closest("a");
    if (!a) return;
    // data-doc ANTES que data-wl: es el doc_id exacto (Fuentes de síntesis) — resolución sin ambigüedad.
    const dd = a.getAttribute("data-doc");
    if (dd) {
      e.preventDefault();
      onOpenDoc(dd);
      return;
    }
    const wl = a.getAttribute("data-wl");
    if (wl) {
      e.preventDefault();
      onWikilink(wl);
      return;
    }
    const href = a.getAttribute("href") || "";
    if (/^https?:/i.test(href)) {
      e.preventDefault();
      window.open(href, "_blank", "noopener");
    }
  }

  return (
    <aside className="reader" style={{ width }}>
      <div
        className="reader-handle"
        title={t("arrastrá para redimensionar", "drag to resize")}
        onPointerDown={() => {
          dragging.current = true;
          document.body.style.cursor = "col-resize";
          document.body.style.userSelect = "none";
        }}
      />
      <header>
        <div>
          <p className="eyebrow">{doc ? `${doc.kind} · ${doc.project}` : t("cargando…", "loading…")}</p>
          <h2>{doc?.title || docId}</h2>
          <code>{docId}</code>
        </div>
        <div className="reader-actions">
          <button onClick={() => onNeighbors(docId)} title={t("ver vecinos en el grafo", "view neighbors in graph")}>◉ {t("vecinos", "neighbors")}</button>
          <button onClick={() => window.open(`/doc?id=${encodeURIComponent(docId)}`, "_blank", "noopener")} title={t("JSON crudo", "raw JSON")}>{t("crudo", "raw")}</button>
          <button onClick={onClose} title={t("cerrar (Esc)", "close (Esc)")}>✕</button>
        </div>
      </header>
      {error && <div className="error">{error}</div>}
      {doc && isMd && <div className="reader-body md" ref={bodyRef} onClick={onBodyClick} />}
      {doc && !isMd && (
        <div className="reader-body">
          <pre>{doc.body}</pre>
        </div>
      )}
    </aside>
  );
}
