import { useEffect, useMemo, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import louvain from "graphology-communities-louvain";
import { createNodeBorderProgram } from "@sigma/node-border";
import type { EdgeData, NodeData } from "./api";
import { colorForProject, communityColor, edgeColor, sizeForKind } from "./colors";
import { t } from "./i18n";
import { communityNames, tierMap, type LabelLevel } from "./labels";

const CURATED_BORDER = "#f4ead6";
const NEUTRAL_COMMUNITY = "#6b7270"; // nodo sin barrio (fuera de la partición servida o comunidad -1)

function communityFill(c: number | undefined): string {
  return c === undefined || c < 0 ? NEUTRAL_COMMUNITY : communityColor(c);
}

// Nodo con borde (curados): borde fino del color de borderColor + relleno del color del nodo.
const BorderProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.12 }, color: { attribute: "borderColor" } },
    { size: { fill: true }, color: { attribute: "color" } },
  ],
});

type Props = {
  nodes: NodeData[];
  edges: EdgeData[];
  needsLayout?: boolean;
  view: string;
  communities: boolean;
  servedPartition?: { map: Record<string, number>; algorithm: string } | null;
  labelLevel: LabelLevel;
  focus: { id: string; ts: number } | null;
  onNodeClick: (node: NodeData) => void;
};

export default function GraphView({ nodes, edges, needsLayout, view, communities, servedPartition = null, labelLevel, focus, onNodeClick }: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const cloudsRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hoverRef = useRef<string | null>(null);
  const pulseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Comunidades: partición Leiden servida (persistente, F21) si está disponible y coherente;
  // si no, fallback al Louvain client-side sobre la vista (louvain no acepta grafos mixtos/multi).
  const communityOf = useMemo(() => {
    if (!communities || !nodes.length) return null;
    if (servedPartition) {
      // Adaptar keys: el artefacto está keyed por doc_id; los nodos usan node.id="doc:<doc_id>".
      // Nodos sin doc_id / sin entrada / comunidad -1 quedan fuera del map → color neutro.
      const map: Record<string, number> = {};
      let hits = 0;
      for (const n of nodes) {
        if (!n.doc_id) continue;
        const c = servedPartition.map[n.doc_id];
        if (c === undefined || c < 0) continue;
        map[n.id] = c;
        hits++;
      }
      // Vista sin docs en la partición (p.ej. macro, nodos = proyectos): la partición servida
      // no aplica — caer al Louvain local en vez de pintar todo neutro.
      if (hits > 0) return map;
    }
    try {
      const simple = new Graph({ type: "undirected" });
      for (const n of nodes) simple.mergeNode(n.id);
      for (const e of edges) {
        if (e.source === e.target || !simple.hasNode(e.source) || !simple.hasNode(e.target)) continue;
        simple.mergeUndirectedEdge(e.source, e.target, { weight: e.weight || 1 });
      }
      return louvain(simple, { getEdgeWeight: "weight" }) as Record<string, number>;
    } catch {
      return null;
    }
  }, [communities, nodes, edges, servedPartition]);

  const tiers = useMemo(() => tierMap(nodes), [nodes]);

  useEffect(() => {
    if (!container.current) return;
    sigmaRef.current?.kill();
    hoverRef.current = null;

    const forceLevel = labelLevel === "auto" ? 0 : labelLevel;
    const graph = new Graph({ multi: true, type: "mixed" });
    for (const node of nodes) {
      const color = communityOf
        ? communityFill(communityOf[node.id])
        : node.kind === "project"
          ? view === "macro"
            ? colorForProject(node.project)
            : "#ffffff"
          : colorForProject(node.project || "root");
      graph.addNode(node.id, {
        ...node,
        label: node.label,
        x: node.x || 0,
        y: node.y || 0,
        size: sizeForKind(node.kind, node.degree) * (nodes.length < 120 ? 1.7 : 1),
        color,
        forceLabel: forceLevel > 0 && (tiers.get(node.id) ?? 3) <= forceLevel,
        ...(node.curated || node.kind === "project" ? { type: "border", borderColor: CURATED_BORDER } : {}),
      });
    }
    for (const edge of edges) {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
      graph.addEdgeWithKey(edge.id, edge.source, edge.target, {
        type: "line",
        size: edge.type === "inter_project" ? Math.max(0.6, Math.min(6, Math.log1p(edge.weight))) : Math.max(0.4, Math.min(2.5, edge.weight || 1)),
        color: edgeColor(edge.type),
        etype: edge.type,
      });
    }

    // Niveles de etiqueta: auto = por zoom (threshold); 1/2 = solo forzadas; 3 = todas.
    const threshold = labelLevel === "auto" ? (view === "macro" ? 0 : 5) : labelLevel === 3 ? 0 : Infinity;
    const renderer = new Sigma(graph, container.current, {
      allowInvalidContainer: true,
      renderEdgeLabels: false,
      defaultEdgeColor: "rgba(160,170,170,0.3)",
      labelColor: { color: "#f4ead6" },
      labelSize: 12,
      labelRenderedSizeThreshold: threshold,
      nodeProgramClasses: { border: BorderProgram },
    });

    // Hover-highlight: nodo + vecinos directos nítidos, el resto atenuado.
    const highlightEnabled = graph.order <= 3000;
    renderer.setSetting("nodeReducer", (node, data) => {
      const h = hoverRef.current;
      if (!h || !highlightEnabled) return data;
      if (node === h || graph.areNeighbors(node, h)) return { ...data, zIndex: 1, forceLabel: node === h };
      return { ...data, color: "rgba(95,102,100,0.18)", label: "", type: "circle", zIndex: 0 };
    });
    renderer.setSetting("edgeReducer", (edge, data) => {
      const h = hoverRef.current;
      if (!h || !highlightEnabled) return data;
      return graph.hasExtremity(edge, h) ? { ...data, zIndex: 1 } : { ...data, hidden: true };
    });
    renderer.on("enterNode", ({ node }) => {
      hoverRef.current = node;
      renderer.refresh({ skipIndexation: true });
      if (cloudsRef.current) cloudsRef.current.style.opacity = "0.15";
    });
    renderer.on("leaveNode", () => {
      hoverRef.current = null;
      renderer.refresh({ skipIndexation: true });
      if (cloudsRef.current) cloudsRef.current.style.opacity = "1";
    });
    renderer.on("clickNode", ({ node }) => onNodeClick(graph.getNodeAttributes(node) as NodeData));
    // Doble clic en el fondo = reencuadrar (gesto estándar; evita quedar perdido tras el zoom).
    renderer.on("doubleClickStage", (e: { preventSigmaDefault: () => void }) => {
      e.preventSigmaDefault();
      renderer.getCamera().animatedReset({ duration: 300 });
    });
    sigmaRef.current = renderer;

    // Nubes semánticas nombradas: overlay DOM en el centroide de cada comunidad.
    let cleanClouds: (() => void) | null = null;
    if (communityOf && cloudsRef.current) {
      const names = communityNames(nodes, communityOf);
      const centroids = new Map<number, { x: number; y: number; n: number }>();
      for (const nd of nodes) {
        const c = communityOf[nd.id];
        if (c === undefined || !names.has(c)) continue;
        const acc = centroids.get(c) || { x: 0, y: 0, n: 0 };
        acc.x += nd.x || 0;
        acc.y += nd.y || 0;
        acc.n += 1;
        centroids.set(c, acc);
      }
      const overlay = cloudsRef.current;
      overlay.innerHTML = "";
      const divs: { el: HTMLDivElement; gx: number; gy: number }[] = [];
      for (const [c, acc] of centroids) {
        if (acc.n < 3) continue; // comunidades minúsculas sin nombre
        const el = document.createElement("div");
        el.className = "cloud-label";
        el.textContent = names.get(c)!;
        el.style.borderColor = communityColor(c);
        overlay.appendChild(el);
        divs.push({ el, gx: acc.x / acc.n, gy: acc.y / acc.n });
      }
      const sync = () => {
        for (const d of divs) {
          const p = renderer.graphToViewport({ x: d.gx, y: d.gy });
          d.el.style.transform = `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px) translate(-50%, -50%)`;
        }
      };
      renderer.on("afterRender", sync);
      sync();
      cleanClouds = () => {
        renderer.off("afterRender", sync);
        overlay.innerHTML = "";
      };
    } else if (cloudsRef.current) {
      cloudsRef.current.innerHTML = "";
    }

    let layout: InstanceType<typeof FA2Layout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    if (needsLayout && graph.order > 1) {
      // Grafos chicos (el Lab): más gravedad y menos dispersión, si no los clusters
      // se van a las esquinas y el centro queda vacío.
      const small = graph.order < 120;
      layout = new FA2Layout(graph, {
        settings: small
          ? { gravity: 8, scalingRatio: 3, slowDown: 6, barnesHutOptimize: false }
          : { gravity: 1, scalingRatio: 8, slowDown: 10, barnesHutOptimize: graph.order > 500 },
      });
      layout.start();
      timer = setTimeout(() => {
        layout?.stop();
        // Auto-encuadre al terminar el layout: sin esto el grafo aparece descentrado
        // y el usuario no sabe cómo volver.
        renderer.getCamera().animatedReset({ duration: 400 });
      }, small ? 1400 : 2500);
    }
    return () => {
      if (timer) clearTimeout(timer);
      layout?.kill();
      cleanClouds?.();
      renderer.kill();
      sigmaRef.current = null;
    };
  }, [nodes, edges, communityOf, needsLayout, view, labelLevel, tiers, onNodeClick]);

  // Focus (búsqueda→mapa): esperar al primer render (RAF), animar cámara y pulsar el vecindario.
  useEffect(() => {
    if (!focus) return;
    const raf = requestAnimationFrame(() => {
      const renderer = sigmaRef.current;
      if (!renderer) return;
      const graph = renderer.getGraph();
      if (!graph.hasNode(focus.id)) return;
      const pos = renderer.getNodeDisplayData(focus.id);
      if (!pos) return;
      renderer.getCamera().animate({ x: pos.x, y: pos.y, ratio: 0.08 }, { duration: 550 });
      // Pulso: reusar el hover-highlight ~2s para mostrar a dónde te llevó.
      hoverRef.current = focus.id;
      renderer.refresh({ skipIndexation: true });
      if (pulseTimer.current) clearTimeout(pulseTimer.current);
      pulseTimer.current = setTimeout(() => {
        if (hoverRef.current === focus.id) {
          hoverRef.current = null;
          sigmaRef.current?.refresh({ skipIndexation: true });
        }
      }, 2000);
    });
    return () => cancelAnimationFrame(raf);
  }, [focus]);

  function zoom(delta: number) {
    const cam = sigmaRef.current?.getCamera();
    if (!cam) return;
    if (delta > 0) cam.animatedZoom({ duration: 200 });
    else cam.animatedUnzoom({ duration: 200 });
  }

  return (
    <section className="canvas-wrap">
      <div className="canvas" ref={container} />
      <div className="clouds" ref={cloudsRef} />
      <div className="zoomctl">
        <button title={t("acercar", "zoom in")} onClick={() => zoom(1)}>+</button>
        <button title={t("alejar", "zoom out")} onClick={() => zoom(-1)}>−</button>
        <button title={t("reencuadrar todo (o doble clic en el fondo)", "fit all (or double-click the background)")} className="fitbtn"
                onClick={() => sigmaRef.current?.getCamera().animatedReset({ duration: 300 })}>
          ⤢ {t("ajustar", "fit")}
        </button>
      </div>
    </section>
  );
}
