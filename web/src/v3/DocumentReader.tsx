import DOMPurify from "dompurify";
import { marked } from "marked";
import { Braces, ExternalLink, Network, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { request } from "./api";
import type { DocPayload } from "./types";

type Heading = { id: string; label: string; level: number };

function isMarkdown(doc: DocPayload) {
  return /\.(md|markdown|mdx)$/i.test(doc.doc_id)
    || ["map", "biblioteca", "synthesis", "discovery"].includes(doc.kind);
}

function withoutFrontmatter(body: string) {
  return body.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
}

function markdown(body: string): { html: string; headings: Heading[] } {
  const noRemoteImages = body.replace(/!\[([^\]]*)\]\((https?:\/\/[^)]+)\)/g, "[$1 — imagen externa omitida]($2)");
  const parsed = marked.parse(noRemoteImages, { async: false }) as string;
  const linked = parsed.replace(/\[\[([^\]<>|#]{1,160})\]\]/g, (_match, target: string) => {
    const safe = target.replace(/"/g, "&quot;");
    return `<a class="wikilink" data-wl="${safe}">${target}</a>`;
  });
  const safe = DOMPurify.sanitize(linked, {
    FORBID_TAGS: ["img", "iframe", "object", "style", "form", "input"],
    USE_PROFILES: { html: true },
  });
  const container = document.createElement("div");
  container.innerHTML = safe;
  const headings: Heading[] = [];
  const used = new Set<string>();
  container.querySelectorAll("h1,h2,h3").forEach((element, index) => {
    const label = element.textContent?.trim() || `Sección ${index + 1}`;
    let id = label.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64) || `seccion-${index + 1}`;
    while (used.has(id)) id = `${id}-${index + 1}`;
    used.add(id);
    element.id = id;
    headings.push({ id, label, level: Number(element.tagName.slice(1)) });
  });
  return { html: container.innerHTML, headings };
}

export default function DocumentReader({
  docId,
  onClose,
  onOpenDoc,
  onNeighbors,
  onWikilink,
}: {
  docId: string;
  onClose: () => void;
  onOpenDoc: (id: string) => void;
  onNeighbors: (id: string) => void;
  onWikilink: (name: string) => void;
}) {
  const [doc, setDoc] = useState<DocPayload | null>(null);
  const [error, setError] = useState("");
  const [rendered, setRendered] = useState<{ html: string; headings: Heading[] }>({ html: "", headings: [] });
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    setDoc(null);
    setError("");
    request<DocPayload>(`/doc?id=${encodeURIComponent(docId)}`)
      .then((value) => {
        setDoc(value);
        setRendered(isMarkdown(value) ? markdown(withoutFrontmatter(value.body)) : { html: "", headings: [] });
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [docId]);

  useEffect(() => {
    closeRef.current?.focus();
    const key = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);

  function onBodyClick(event: React.MouseEvent) {
    const anchor = (event.target as HTMLElement).closest("a");
    if (!anchor) return;
    const exact = anchor.getAttribute("data-doc");
    const wikilink = anchor.getAttribute("data-wl");
    if (exact) {
      event.preventDefault();
      onOpenDoc(exact);
      return;
    }
    if (wikilink) {
      event.preventDefault();
      onWikilink(wikilink);
      return;
    }
    const href = anchor.getAttribute("href") || "";
    if (/^https?:/i.test(href)) {
      event.preventDefault();
      window.open(href, "_blank", "noopener,noreferrer");
    }
  }

  return <div className="v3-reader-backdrop" role="dialog" aria-modal="true" aria-label="Documento">
    <article className="v3-reader">
      <header className="v3-reader-head">
        <div>
          <p className="v3-eyebrow">{doc ? `${doc.kind} · ${doc.project}` : "Documento"}</p>
          <h1>{doc?.title || docId}</h1>
          <code>{docId}</code>
        </div>
        <div className="v3-icon-actions">
          <button onClick={() => onNeighbors(docId)} title="Ver vecindario en el Atlas"><Network /><span>Vecindario</span></button>
          <button onClick={() => window.open(`/doc?id=${encodeURIComponent(docId)}`, "_blank", "noopener,noreferrer")} title="Abrir JSON crudo"><Braces /><span>JSON</span></button>
          <button ref={closeRef} onClick={onClose} title="Cerrar"><X /><span>Cerrar</span></button>
        </div>
      </header>
      {error && <div className="v3-notice error">{error}</div>}
      {!doc && !error && <div className="v3-skeleton tall" />}
      {doc && <div className="v3-reader-layout">
        {rendered.headings.length > 1 && <nav className="v3-toc" aria-label="Índice del documento">
          <p className="v3-eyebrow">En esta página</p>
          {rendered.headings.map((heading) => <a key={heading.id} className={`level-${heading.level}`} href={`#${heading.id}`}>{heading.label}</a>)}
        </nav>}
        {isMarkdown(doc)
          ? <div className="v3-prose" onClick={onBodyClick} dangerouslySetInnerHTML={{ __html: rendered.html }} />
          : <pre className="v3-source-code"><code>{doc.body}</code></pre>}
      </div>}
      <footer className="v3-reader-foot"><ExternalLink size={14} /> Documento servido desde el índice; abrir una fuente no toca el filesystem.</footer>
    </article>
  </div>;
}
