import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchCommunities, getJson, type CommunitiesArtifact, type CrossPayload, type EdgeData, type GraphPayload, type Manifest, type NodeData, type SearchResult } from "./api";
import { colorForProject, EDGE_TYPE_LABELS, edgeColor } from "./colors";
import type { LabelLevel } from "./labels";
import GraphView from "./GraphView";
import SearchBox from "./SearchBox";
import TreePanel from "./TreePanel";
import StatsPanel from "./StatsPanel";
import { t } from "./i18n";

// --- Paneles redimensionables (persistidos en localStorage) ---
function loadLayout(): { left: number; right: number } {
  try {
    const s = JSON.parse(localStorage.getItem("atlas.layout") || "{}");
    return { left: Math.min(Math.max(s.left || 300, 200), 700), right: Math.min(Math.max(s.right || 320, 200), 700) };
  } catch {
    return { left: 300, right: 320 };
  }
}

const Graph3D = lazy(() => import("./Graph3D"));
const DocReader = lazy(() => import("./DocReader"));

type ViewKind = "macro" | "global" | "projects" | "neighbors" | "discovery";
type Tab = "vistas" | "arbol" | "estado";

const MERGE_NODE_CAP = 6000;
const MERGE_EDGE_CAP = 30000;
const FA2_SKIP_NODES = 3000;
const FA2_SKIP_EDGES = 15000;

const EMPTY_GRAPH: GraphPayload = { schema_version: 1, generation: "", truncated: false, nodes: [], edges: [], counts: { nodes: 0, edges: 0, by_kind: {}, by_project: {}, by_edge_type: {} } };

/** Une los grafos de los proyectos seleccionados + aristas cruzadas; siembra x/y por grilla de clusters. */
async function loadProjectsMerge(selected: string[]): Promise<GraphPayload & { needsLayout: boolean }> {
  const graphs = await Promise.all(
    selected.map((p) => getJson<GraphPayload>(`/ui/graph?view=project&project=${encodeURIComponent(p)}`)),
  );
  let cross: CrossPayload | null = null;
  try {
    cross = await getJson<CrossPayload>("/ui/graph?view=cross");
  } catch {
    cross = null;
  }
  const nodes = new Map<string, NodeData>();
  const edges = new Map<string, EdgeData>();
  const cols = Math.max(1, Math.ceil(Math.sqrt(graphs.length)));
  graphs.forEach((g, gi) => {
    const ox = (gi % cols) * 1200;
    const oy = Math.floor(gi / cols) * 1200;
    for (const n of g.nodes) if (!nodes.has(n.id)) nodes.set(n.id, { ...n, x: (n.x || 0) + ox, y: (n.y || 0) + oy });
    for (const e of g.edges) if (!edges.has(e.id)) edges.set(e.id, e);
  });
  if (cross) for (const e of cross.edges) if (nodes.has(e.source) && nodes.has(e.target) && !edges.has(e.id)) edges.set(e.id, e);
  if (nodes.size > MERGE_NODE_CAP || edges.size > MERGE_EDGE_CAP) {
    throw new Error(`${t("merge demasiado grande", "merge is too large")} (${nodes.size} ${t("nodos", "nodes")} / ${edges.size} ${t("aristas", "edges")}) — ${t("seleccioná menos proyectos", "select fewer projects")}`);
  }
  return {
    ...EMPTY_GRAPH,
    generation: graphs[0]?.generation || "",
    truncated: graphs.some((g) => g.truncated) || Boolean(cross?.truncated),
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    counts: { ...EMPTY_GRAPH.counts, nodes: nodes.size, edges: edges.size },
    needsLayout: selected.length > 1 && nodes.size <= FA2_SKIP_NODES && edges.size <= FA2_SKIP_EDGES,
  };
}

/** BFS client-side sobre el payload de vecinos: nodos a ≤depth saltos del centro. */
function filterByDepth(g: GraphPayload, centerId: string, depth: number): GraphPayload {
  if (depth >= 3 || !g.nodes.some((n) => n.id === centerId)) return g;
  const adj = new Map<string, string[]>();
  for (const e of g.edges) {
    (adj.get(e.source) || adj.set(e.source, []).get(e.source)!).push(e.target);
    (adj.get(e.target) || adj.set(e.target, []).get(e.target)!).push(e.source);
  }
  const dist = new Map<string, number>([[centerId, 0]]);
  const q = [centerId];
  while (q.length) {
    const cur = q.shift()!;
    const d = dist.get(cur)!;
    if (d >= depth) continue;
    for (const nb of adj.get(cur) || []) {
      if (!dist.has(nb)) {
        dist.set(nb, d + 1);
        q.push(nb);
      }
    }
  }
  const nodes = g.nodes.filter((n) => dist.has(n.id));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = g.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  return { ...g, nodes, edges, counts: { ...g.counts, nodes: nodes.length, edges: edges.length } };
}

// ---------- Estado en URL (deep-linking) ----------
type UrlState = { view: ViewKind; sel: string[]; nb: string | null; depth: number; doc: string | null; com: boolean; d3: boolean; lb: LabelLevel };

function readHash(): Partial<UrlState> {
  const h = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const out: Partial<UrlState> = {};
  const v = h.get("view");
  if (v === "macro" || v === "global" || v === "projects" || v === "neighbors" || v === "discovery") out.view = v;
  if (h.get("sel")) out.sel = h.get("sel")!.split(",").filter(Boolean);
  if (h.get("nb")) out.nb = h.get("nb");
  if (h.get("depth")) out.depth = Math.min(3, Math.max(1, parseInt(h.get("depth")!, 10) || 3));
  if (h.get("doc")) out.doc = h.get("doc");
  if (h.get("com")) out.com = h.get("com") === "1";
  if (h.get("d3")) out.d3 = h.get("d3") === "1";
  const lb = h.get("lb");
  if (lb === "1" || lb === "2" || lb === "3") out.lb = parseInt(lb, 10) as LabelLevel;
  return out;
}

function writeHash(s: UrlState): string {
  const h = new URLSearchParams();
  h.set("view", s.view);
  if (s.sel.length) h.set("sel", s.sel.join(","));
  if (s.nb) h.set("nb", s.nb);
  if (s.depth !== 3) h.set("depth", String(s.depth));
  if (s.doc) h.set("doc", s.doc);
  if (s.com) h.set("com", "1");
  if (s.d3) h.set("d3", "1");
  if (s.lb !== "auto") h.set("lb", String(s.lb));
  return "#" + h.toString();
}

const initial = readHash();

export default function App() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [servedCommunities, setServedCommunities] = useState<CommunitiesArtifact | null>(null);
  const [graphData, setGraphData] = useState<(GraphPayload & { needsLayout?: boolean }) | null>(null);
  const [neighborsData, setNeighborsData] = useState<GraphPayload | null>(null);
  const [neighborsId, setNeighborsId] = useState<string | null>(initial.nb ?? null);
  const [depth, setDepth] = useState(initial.depth ?? 3);
  const [selected, setSelected] = useState<NodeData | null>(null);
  const [view, setView] = useState<ViewKind>(initial.view ?? "macro");
  const [selectedProjects, setSelectedProjects] = useState<string[]>(initial.sel ?? []);
  const [readerDoc, setReaderDoc] = useState<string | null>(initial.doc ?? null);
  const [communities, setCommunities] = useState(initial.com ?? false);
  const [mode3d, setMode3d] = useState(initial.d3 ?? false);
  const [offEdgeTypes, setOffEdgeTypes] = useState<Set<string>>(new Set());
  const [offKinds, setOffKinds] = useState<Set<string>>(new Set(["fs_code", "fs_config"]));
  const [weightFrac, setWeightFrac] = useState(0);
  const [focus, setFocus] = useState<{ id: string; ts: number } | null>(null);
  const [pendingFocus, setPendingFocus] = useState<string | null>(null);
  const [labelLevel, setLabelLevel] = useState<LabelLevel>(initial.lb ?? "auto");
  const [layout, setLayout] = useState(loadLayout);
  const [tab, setTab] = useState<Tab>("vistas");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const applyingHash = useRef(false);

  // Arrastre de los divisores entre paneles.
  const dragging = useRef<"left" | "right" | null>(null);
  useEffect(() => {
    function onMove(e: PointerEvent) {
      if (!dragging.current) return;
      setLayout((prev) => {
        const max = Math.floor(window.innerWidth * 0.45);
        if (dragging.current === "left") return { ...prev, left: Math.min(Math.max(e.clientX, 200), max) };
        return { ...prev, right: Math.min(Math.max(window.innerWidth - e.clientX, 200), max) };
      });
    }
    function onUp() {
      if (!dragging.current) return;
      dragging.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setLayout((l) => {
        localStorage.setItem("atlas.layout", JSON.stringify(l));
        return l;
      });
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  function startDrag(which: "left" | "right") {
    dragging.current = which;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  useEffect(() => {
    getJson<Manifest>("/ui/manifest").then(setManifest).catch((e) => setError(String(e)));
    fetchCommunities().then(setServedCommunities); // null si no fue emitida → fallback Louvain
  }, []);

  // ---------- Carga por vista (state machine; "neighbors" carga imperativo) ----------
  const projectsKey = selectedProjects.join("|");
  useEffect(() => {
    if (view === "neighbors") return;
    let cancelled = false;
    setLoading(true);
    const load: Promise<GraphPayload & { needsLayout?: boolean }> =
      view === "macro"
        ? getJson<GraphPayload>("/ui/graph?view=macro")
        : view === "global"
          ? getJson<GraphPayload>("/ui/graph?view=global")
          : view === "discovery"
            ? getJson<GraphPayload>("/ui/graph?view=discovery")
            : selectedProjects.length
              ? loadProjectsMerge(selectedProjects)
              : Promise.resolve(EMPTY_GRAPH);
    load
      .then((g) => {
        if (cancelled) return;
        setGraphData(g);
        setSelected(null);
        setError("");
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [view, projectsKey]);

  const openNeighbors = useCallback(async (docId: string) => {
    try {
      setLoading(true);
      const g = await getJson<GraphPayload>(`/ui/graph?view=neighbors&id=${encodeURIComponent(docId)}`);
      setNeighborsData(g);
      setNeighborsId(docId);
      setView("neighbors");
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Restaurar vecinos desde el hash inicial.
  useEffect(() => {
    if (initial.nb && initial.view === "neighbors") void openNeighbors(initial.nb);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------- URL sync ----------
  useEffect(() => {
    if (applyingHash.current) return;
    const next = writeHash({ view, sel: selectedProjects, nb: view === "neighbors" ? neighborsId : null, depth, doc: readerDoc, com: communities, d3: mode3d, lb: labelLevel });
    if (window.location.hash !== next) window.history.replaceState(null, "", next);
  }, [view, selectedProjects, neighborsId, depth, readerDoc, communities, mode3d, labelLevel]);

  useEffect(() => {
    function onHash() {
      applyingHash.current = true;
      const s = readHash();
      if (s.view) setView(s.view);
      setSelectedProjects(s.sel ?? []);
      setDepth(s.depth ?? 3);
      setReaderDoc(s.doc ?? null);
      setCommunities(s.com ?? false);
      setMode3d(s.d3 ?? false);
      setLabelLevel(s.lb ?? "auto");
      if (s.view === "neighbors" && s.nb) void openNeighbors(s.nb);
      setTimeout(() => {
        applyingHash.current = false;
      }, 0);
    }
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [openNeighbors]);

  // ---------- Atajos ----------
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      const typing = t.tagName === "INPUT" || t.tagName === "TEXTAREA";
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape" && readerDoc) {
        setReaderDoc(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [readerDoc]);

  // ---------- Filtros ----------
  const base = useMemo<GraphPayload>(() => {
    if (view === "neighbors") {
      if (!neighborsData || !neighborsId) return EMPTY_GRAPH;
      return filterByDepth(neighborsData, "doc:" + neighborsId, depth);
    }
    return graphData ?? EMPTY_GRAPH;
  }, [view, graphData, neighborsData, neighborsId, depth]);

  // Partición Leiden servida: solo si el toggle está activo y su generación coincide con la
  // del GRAFO efectivamente renderizado (no la del manifest inicial: un flip atómico entre
  // requests podría colorear un grafo nuevo con la partición vieja). Mismatch → Louvain local.
  const servedPartition = useMemo(() => {
    if (!communities || !servedCommunities || !base.generation) return null;
    if (view === "macro") return null; // nodos = proyectos, sin doc_id: la partición doc-level no aplica
    if (servedCommunities.ui_generation !== base.generation) return null;
    return { map: servedCommunities.communities, algorithm: servedCommunities.algorithm };
  }, [communities, servedCommunities, base, view]);

  const kindsPresent = useMemo(() => [...new Set(base.nodes.map((n) => n.kind))].filter((k) => k !== "project").sort(), [base]);
  const edgeTypesPresent = useMemo(() => [...new Set(base.edges.map((e) => e.type))].sort(), [base]);

  const weightThreshold = useMemo(() => {
    if (weightFrac <= 0 || !base.edges.length) return -Infinity;
    const ws = base.edges.map((e) => e.weight).sort((a, b) => a - b);
    return ws[Math.min(ws.length - 1, Math.floor(weightFrac * (ws.length - 1)))];
  }, [base, weightFrac]);

  const visibleNodes = useMemo(
    () => base.nodes.filter((n) => n.kind === "project" || !offKinds.has(n.kind)),
    [base, offKinds],
  );
  const visibleEdges = useMemo(() => {
    const ids = new Set(visibleNodes.map((n) => n.id));
    return base.edges.filter((e) => !offEdgeTypes.has(e.type) && e.weight >= weightThreshold && ids.has(e.source) && ids.has(e.target));
  }, [base, visibleNodes, offEdgeTypes, weightThreshold]);

  // ---------- Interacciones ----------
  const openProject = useCallback((project: string) => {
    setSelectedProjects([project]);
    setView("projects");
  }, []);

  const handleNodeClick = useCallback(
    (node: NodeData) => {
      if (node.kind === "project" && view === "macro") {
        openProject(node.project);
        return;
      }
      setSelected(node);
    },
    [view, openProject],
  );

  // Búsqueda→mapa: elegir un resultado SIEMPRE lleva el mapa al doc.
  const onSearchPick = useCallback(
    (r: SearchResult, action: "focus" | "doc" | "neighbors") => {
      if (action === "doc") return setReaderDoc(r.doc_id);
      if (action === "neighbors") return void openNeighbors(r.doc_id);
      const nid = "doc:" + r.doc_id;
      // Asegurar que el kind del doc esté visible (si no, el focus apuntaría a un nodo filtrado).
      setOffKinds((prev) => {
        if (!prev.has(r.kind)) return prev;
        const next = new Set(prev);
        next.delete(r.kind);
        return next;
      });
      const node = visibleNodes.find((n) => n.id === nid);
      if (node) {
        setSelected(node);
        setFocus({ id: nid, ts: Date.now() });
      } else {
        // No está en el grafo actual → cargar el proyecto del doc y enfocar al aterrizar.
        setPendingFocus(nid);
        setSelectedProjects([r.project]);
        setView("projects");
      }
    },
    [visibleNodes, openNeighbors],
  );

  // Resolución del pendingFocus: cuando el grafo del proyecto terminó de cargar.
  useEffect(() => {
    if (!pendingFocus || loading) return;
    const node = visibleNodes.find((n) => n.id === pendingFocus);
    if (node) {
      setSelected(node);
      setFocus({ id: pendingFocus, ts: Date.now() });
      setPendingFocus(null);
    } else if (view === "projects" && graphData && base.nodes.length) {
      // El grafo cargó y el nodo no está (p.ej. duplicado) → fallback: vecinos centrados en el doc.
      setPendingFocus(null);
      void openNeighbors(pendingFocus.slice(4));
    }
  }, [pendingFocus, loading, visibleNodes, view, graphData, base, openNeighbors]);

  const onWikilink = useCallback(async (name: string) => {
    try {
      const d = await getJson<{ results: SearchResult[] }>(`/search?q=${encodeURIComponent(name)}&k=3`);
      const hit = d.results?.find((r) => r.title === name || r.doc_id.includes(name)) || d.results?.[0];
      if (hit) setReaderDoc(hit.doc_id);
    } catch {
      /* wikilink sin resolución: no-op */
    }
  }, []);

  function toggleSet(setter: (fn: (prev: Set<string>) => Set<string>) => void, key: string) {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const nodeColor3d = useCallback((n: { kind: string; project: string }) => (n.kind === "project" ? "#ffffff" : colorForProject(n.project || "root")), []);

  return (
    <main className="shell" style={{ gridTemplateColumns: `${layout.left}px 6px minmax(0, 1fr) 6px ${layout.right}px` }}>
      <aside className="panel">
        <p className="eyebrow">{t("memoria colectiva", "collective memory")}</p>
        <h1>{t("Atlas relacional", "Relational Atlas")}</h1>
        <div className="tabs">
          <button className={tab === "vistas" ? "active" : ""} onClick={() => setTab("vistas")}>{t("Vistas", "Views")}</button>
          <button className={tab === "arbol" ? "active" : ""} onClick={() => setTab("arbol")}>{t("Árbol", "Tree")}</button>
          <button className={tab === "estado" ? "active" : ""} onClick={() => setTab("estado")}>{t("Estado", "Status")}</button>
        </div>
        {error && <div className="error">{error}</div>}
        {loading && <div className="loading">{t("cargando…", "loading…")}</div>}

        {tab === "vistas" && (
          <>
            <div className="controls">
              <button className={view === "macro" ? "active" : ""} onClick={() => setView("macro")}>Macro</button>
              <button className={view === "global" ? "active" : ""} onClick={() => setView("global")}>Global</button>
              <button className={view === "projects" ? "active" : ""} onClick={() => setView("projects")}>{t("Proyectos", "Projects")}</button>
              <button className={view === "discovery" ? "active" : ""} onClick={() => setView("discovery")}>{t("Hallazgos", "Findings")}</button>
              {view === "projects" && manifest && (
                <div className="project-list">
                  {manifest.projects
                    .slice()
                    .sort((a, b) => b.count - a.count)
                    .map((p) => (
                      <label key={p.slug} className="project-check">
                        <input
                          type="checkbox"
                          checked={selectedProjects.includes(p.project)}
                          onChange={() => setSelectedProjects((prev) => (prev.includes(p.project) ? prev.filter((x) => x !== p.project) : [...prev, p.project]))}
                        />
                        <i style={{ background: colorForProject(p.project) }} /> {p.project} <span>({p.count})</span>
                      </label>
                    ))}
                </div>
              )}
              {view === "neighbors" && (
                <label className="slider">
                  {t("profundidad", "depth")} {depth}
                  <input type="range" min={1} max={3} step={1} value={depth} onChange={(e) => setDepth(parseInt(e.target.value, 10))} />
                </label>
              )}
              <label><input type="checkbox" checked={communities} onChange={(e) => setCommunities(e.target.checked)} /> {t("comunidades", "communities")}{mode3d ? t(" (solo 2D)", " (2D only)") : ""}</label>
              {communities && !mode3d && (
                <span className="community-source">
                  {servedPartition
                    ? `${t("barrios: servidos", "communities: served")} (${servedPartition.algorithm === "louvain" ? "Louvain" : "Leiden"})`
                    : t("barrios: locales (Louvain)", "communities: local (Louvain)")}
                </span>
              )}
              <label><input type="checkbox" checked={mode3d} onChange={(e) => setMode3d(e.target.checked)} /> 3D</label>
              <label className="label-level">
                {t("etiquetas", "labels")}
                <select value={String(labelLevel)} onChange={(e) => setLabelLevel(e.target.value === "auto" ? "auto" : (parseInt(e.target.value, 10) as LabelLevel))}>
                  <option value="auto">auto (zoom)</option>
                  <option value="1">1 · {t("proyectos", "projects")}</option>
                  <option value="2">2 · + {t("núcleos", "hubs")}</option>
                  <option value="3">3 · {t("todas", "all")}</option>
                </select>
              </label>
            </div>

            {kindsPresent.length > 0 && (
              <div className="kind-chips">
                {kindsPresent.map((k) => (
                  <button key={k} className={`chip ${offKinds.has(k) ? "off" : ""}`} onClick={() => toggleSet(setOffKinds, k)}>{k}</button>
                ))}
              </div>
            )}

            {edgeTypesPresent.length > 0 && (
              <div className="legend interactive">
                {edgeTypesPresent.map((edgeType) => (
                  <button key={edgeType} className={offEdgeTypes.has(edgeType) ? "off" : ""} onClick={() => toggleSet(setOffEdgeTypes, edgeType)} title={t("click para ocultar/mostrar", "click to hide/show")}>
                    <i style={{ background: edgeColor(edgeType) }} /> {EDGE_TYPE_LABELS[edgeType] || edgeType}
                  </button>
                ))}
              </div>
            )}

            {base.edges.length > 5 && (
              <label className="slider">
                {t("peso mín.", "min. weight")} {weightThreshold === -Infinity ? "—" : weightThreshold.toFixed(2)}
                <input type="range" min={0} max={0.95} step={0.05} value={weightFrac} onChange={(e) => setWeightFrac(parseFloat(e.target.value))} />
              </label>
            )}

            <div className="stats">
              <strong>{visibleNodes.length}</strong> {t("nodos", "nodes")} · <strong>{visibleEdges.length}</strong> {t("relaciones", "relationships")}
              {base.truncated && <span className="warn"> · {t("truncado", "truncated")}</span>}
            </div>
          </>
        )}

        {tab === "arbol" && <TreePanel onOpenDoc={(id) => setReaderDoc(id)} />}
        {tab === "estado" && <StatsPanel />}
      </aside>

      <div className="divider" onPointerDown={() => startDrag("left")} title={t("arrastrá para redimensionar", "drag to resize")} />

      <div className="center">
        <SearchBox inputRef={searchRef} onPick={onSearchPick} />
        {mode3d ? (
          <Suspense fallback={<section className="canvas loading3d">{t("cargando 3D…", "loading 3D…")}</section>}>
            <Graph3D nodes={visibleNodes} edges={visibleEdges} nodeColor={nodeColor3d} edgeColor={edgeColor} labelLevel={labelLevel} onSelect={handleNodeClick} />
          </Suspense>
        ) : (
          <GraphView
            nodes={visibleNodes}
            edges={visibleEdges}
            needsLayout={graphData?.needsLayout && view === "projects"}
            view={view}
            communities={communities}
            servedPartition={servedPartition}
            labelLevel={labelLevel}
            focus={focus}
            onNodeClick={handleNodeClick}
          />
        )}
      </div>

      <div className="divider" onPointerDown={() => startDrag("right")} title={t("arrastrá para redimensionar", "drag to resize")} />

      <aside className="inspector">
        {selected ? (
          <>
            <p className="eyebrow">{selected.kind} · {selected.project}</p>
            <h2>{selected.title || selected.label}</h2>
            <code>{selected.doc_id || selected.id}</code>
            <p>{selected.path_rel}</p>
            <div className="actions">
              {selected.doc_id && <button onClick={() => setReaderDoc(selected.doc_id)}>{t("Leer doc", "Read document")}</button>}
              {selected.doc_id && <button onClick={() => void openNeighbors(selected.doc_id!)}>{t("Ver vecinos", "View neighbors")}</button>}
              {selected.kind === "project" && <button onClick={() => openProject(selected.project)}>{t("Ver documentos", "View documents")}</button>}
            </div>
          </>
        ) : (
          <p className="empty">{t("Click en un nodo para inspeccionarlo.", "Click a node to inspect it.")} <kbd>/</kbd> {t("busca.", "searches.")}</p>
        )}
      </aside>

      {readerDoc && (
        <Suspense fallback={<aside className="reader"><div className="loading">{t("cargando lector…", "loading reader…")}</div></aside>}>
          <DocReader docId={readerDoc} onClose={() => setReaderDoc(null)} onNeighbors={(id) => { setReaderDoc(null); void openNeighbors(id); }} onWikilink={onWikilink} onOpenDoc={(id) => setReaderDoc(id)} />
        </Suspense>
      )}
    </main>
  );
}
