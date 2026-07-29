import { lazy, Suspense, useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import { Box, Filter, Focus, List, Maximize2, Network, Search } from "lucide-react";
import { colorForProject, edgeColor } from "../../colors";
import { request } from "../api";
import { navigate } from "../router";
import type { GraphPayload, NodeData } from "../types";
import { EDGE_ROLE, OPERATOR_HELP, ROLE_LABELS, STATUS_LABELS } from "./constants";
import { t, UI_LOCALE } from "../../i18n";

const GraphView = lazy(() => import("../../GraphView"));
const Graph3D = lazy(() => import("../../Graph3D"));

const ROLES = ["support", "counter", "bridge", "target", "context"];

export default function EvidencePage({ onDoc }: { onDoc: (id: string) => void }) {
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [mode, setMode] = useState<"2d" | "3d">(() => sessionStorage.getItem("v3.evidence.mode") === "3d" ? "3d" : "2d");
  const [roles, setRoles] = useState<Set<string>>(new Set(ROLES));
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.toLowerCase());
  const [listOpen, setListOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setBusy(true);
    setError("");
    const params = new URLSearchParams({ scope });
    if (type) params.set("type", type);
    if (status) params.set("status", status);
    try {
      setGraph(await request<GraphPayload>(`/pg/graph?${params}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setGraph(null);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); }, [scope, type, status]);
  useEffect(() => { sessionStorage.setItem("v3.evidence.mode", mode); }, [mode]);

  const visible = useMemo(() => {
    if (!graph) return { nodes: [], edges: [] };
    const edges = graph.edges.filter((edge) => roles.has(EDGE_ROLE[edge.type] || "context"));
    const endpoints = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
    const nodes = graph.nodes.filter((node) => node.id.startsWith("cand:") || endpoints.has(node.id));
    return { nodes, edges };
  }, [graph, roles]);

  const listNodes = visible.nodes.filter((node) => !deferredQuery || `${node.label} ${node.project} ${node.kind}`.toLowerCase().includes(deferredQuery));
  const can3d = visible.nodes.length <= 2000 && typeof WebGLRenderingContext !== "undefined";

  const selectNode = useCallback((node: NodeData) => {
    if (node.id.startsWith("cand:")) {
      const params = new URLSearchParams({ candidate: node.id.slice(5), scope });
      navigate("lab", "bandeja", params);
    } else if (node.doc_id) onDoc(node.doc_id);
  }, [onDoc, scope]);

  function toggleRole(role: string) {
    setRoles((current) => {
      const next = new Set(current);
      next.has(role) ? next.delete(role) : next.add(role);
      return next;
    });
  }

  return <main className="v3-evidence-page">
    <header className="v3-evidence-toolbar">
      <div className="v3-evidence-title"><Network /><span><b>{t("Grafo de evidencia", "Evidence graph")}</b><small>{visible.nodes.length.toLocaleString(UI_LOCALE)} {t("nodos", "nodes")} · {visible.edges.length.toLocaleString(UI_LOCALE)} {t("relaciones", "relationships")}</small></span></div>
      <div className="v3-scope"><button className={scope === "mine" ? "active" : ""} onClick={() => setScope("mine")}>{t("Míos", "Mine")}</button><button className={scope === "all" ? "active" : ""} onClick={() => setScope("all")}>{t("De todos", "Everyone's")}</button></div>
      <select aria-label={t("Operador", "Operator")} value={type} onChange={(event) => setType(event.target.value)}><option value="">{t("Todos los operadores", "All operators")}</option>{Object.entries(OPERATOR_HELP).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}</select>
      <select aria-label={t("Estado", "Status")} value={status} onChange={(event) => setStatus(event.target.value)}><option value="">{t("Todos los estados", "All statuses")}</option>{["candidate", "interesting", "actionable", "discarded", "proposed", "importing", "imported"].map((item) => <option key={item} value={item}>{STATUS_LABELS[item]}</option>)}</select>
      <div className="v3-view-toggle"><button className={mode === "2d" ? "active" : ""} onClick={() => setMode("2d")}><Focus /> 2D</button><button className={mode === "3d" ? "active" : ""} disabled={!can3d} title={!can3d ? t("3D disponible hasta 2.000 nodos y con WebGL", "3D is available for up to 2,000 nodes with WebGL") : t("Vista tridimensional", "Three-dimensional view")} onClick={() => setMode("3d")}><Box /> 3D</button></div>
      <button className={listOpen ? "active" : ""} onClick={() => setListOpen((value) => !value)}><List /> {t("Lista", "List")}</button>
    </header>
    <div className="v3-rolebar"><span><Filter /> {t("Relaciones", "Relationships")}</span>{ROLES.map((role) => <button key={role} className={`role-${role} ${roles.has(role) ? "active" : ""}`} onClick={() => toggleRole(role)}><i />{ROLE_LABELS[role]}</button>)}{!can3d && <small>{t("3D desactivado: este grafo supera 2.000 nodos o el navegador no ofrece WebGL.", "3D disabled: this graph exceeds 2,000 nodes or the browser does not provide WebGL.")}</small>}</div>
    {error && <div className="v3-notice error floating">{error}</div>}
    <section className="v3-evidence-canvas" aria-busy={busy}>
      {busy && <div className="v3-graph-loading"><div className="v3-compass" /><span>{t("Ordenando evidencia…", "Arranging evidence…")}</span></div>}
      {!busy && visible.nodes.length === 0 && <div className="v3-empty major"><Network /><h2>{t("No hay nodos para estos filtros.", "No nodes match these filters.")}</h2><p>{t("Corré una campaña o ampliá el ámbito.", "Run a campaign or broaden the scope.")}</p></div>}
      {!busy && visible.nodes.length > 0 && <Suspense fallback={<div className="v3-graph-loading">{t("Cargando motor gráfico…", "Loading graph engine…")}</div>}>
        {mode === "3d" && can3d
          ? <Graph3D nodes={visible.nodes} edges={visible.edges} nodeColor={(node) => node.kind === "discovery" ? "#e4b86e" : colorForProject(node.project)} edgeColor={edgeColor} labelLevel="auto" onSelect={selectNode} />
          : <GraphView nodes={visible.nodes} edges={visible.edges} needsLayout view="discovery" communities={false} labelLevel="auto" focus={null} onNodeClick={selectNode} />}
      </Suspense>}
      {listOpen && <aside className="v3-node-list"><header><label><Search /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("Buscar nodo", "Search nodes")} /></label><button onClick={() => setListOpen(false)}><Maximize2 /> {t("Cerrar lista", "Close list")}</button></header><p>{listNodes.length} {t("nodos visibles", "visible nodes")}</p><div>{listNodes.slice(0, 500).map((node) => <button key={node.id} onClick={() => selectNode(node)}><span>{node.kind} · {node.project}</span><b>{node.title || node.label}</b></button>)}</div>{listNodes.length > 500 && <small>{t("Mostrando los primeros 500. Afiná la búsqueda para acceder al resto.", "Showing the first 500. Refine the search to access the rest.")}</small>}</aside>}
    </section>
  </main>;
}
