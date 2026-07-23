import { useEffect, useRef, useState, type RefObject } from "react";
import { getJson, type SearchResult } from "./api";
import { colorForProject } from "./colors";

type Props = {
  inputRef: RefObject<HTMLInputElement | null>;
  onPick: (r: SearchResult, action: "focus" | "doc" | "neighbors") => void;
};

type SearchPayload = { mode: string; results: SearchResult[] };

export default function SearchBox({ inputRef, onPick }: Props) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState(-1);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const query = q.trim();
    if (query.length < 2) {
      setResults([]);
      setOpen(false);
      return;
    }
    timer.current = setTimeout(async () => {
      try {
        setBusy(true);
        const d = await getJson<SearchPayload>(`/search?q=${encodeURIComponent(query)}&k=10`);
        setResults(d.results || []);
        setOpen(true);
        setCursor(-1);
      } catch {
        setResults([]);
      } finally {
        setBusy(false);
      }
    }, 300);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [q]);

  // Cerrar al click afuera.
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, []);

  function pick(r: SearchResult, action: "focus" | "doc" | "neighbors") {
    setOpen(false);
    onPick(r, action);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      (e.target as HTMLInputElement).blur();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(results.length - 1, c + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(-1, c - 1));
    } else if (e.key === "Enter" && cursor >= 0 && results[cursor]) {
      pick(results[cursor], "focus");
    }
  }

  return (
    <div className="searchbox" ref={boxRef}>
      <input
        ref={inputRef}
        type="search"
        placeholder="Buscar en la memoria…  ( / )"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onKeyDown={onKey}
      />
      {busy && <span className="search-busy">…</span>}
      {open && results.length > 0 && (
        <ul className="search-results">
          {results.map((r, i) => (
            <li key={r.doc_id + i} className={i === cursor ? "cursor" : ""}>
              <button className="hit" onClick={() => pick(r, "focus")} title="enfocar en el grafo / abrir">
                <i style={{ background: colorForProject(r.project) }} />
                <span className="hit-title">{r.title}</span>
                <span className="hit-meta">{r.kind} · {r.project}</span>
                {r.snippet && <span className="hit-snippet">{r.snippet.replace(/»|«/g, "")}</span>}
              </button>
              <span className="hit-actions">
                <button title="leer documento" onClick={() => pick(r, "doc")}>📄</button>
                <button title="ver vecinos" onClick={() => pick(r, "neighbors")}>◉</button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
