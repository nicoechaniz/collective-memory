import { useEffect, useMemo, useRef } from "react";
import ForceGraph3D, { type ForceGraph3DInstance } from "3d-force-graph";
import SpriteText from "three-spritetext";
import type { EdgeData, NodeData } from "./api";
import { sizeForKind } from "./colors";
import { tierMap, type LabelLevel } from "./labels";

const SPRITE_CAP = 600; // sprites de texto máximos (perf en grafos grandes)
const VIS_K = 0.012;    // umbral tamaño/distancia — equivalente 3D del zoom-threshold del 2D

type Props = {
  nodes: NodeData[];
  edges: EdgeData[];
  nodeColor: (n: { kind: string; project: string }) => string;
  edgeColor: (type: string) => string;
  labelLevel: LabelLevel;
  onSelect: (node: NodeData) => void;
};

export default function Graph3D({ nodes, edges, nodeColor, edgeColor, labelLevel, onSelect }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraph3DInstance | null>(null);
  const levelRef = useRef<LabelLevel>(labelLevel);
  levelRef.current = labelLevel;

  const tiers = useMemo(() => tierMap(nodes), [nodes]);

  // Nodos elegibles para sprite: priorizados por tier asc + degree desc, cap fijo.
  const eligible = useMemo(() => {
    const ranked = nodes
      .slice()
      .sort((a, b) => (tiers.get(a.id) ?? 3) - (tiers.get(b.id) ?? 3) || b.degree - a.degree)
      .slice(0, SPRITE_CAP);
    return new Set(ranked.map((n) => n.id));
  }, [nodes, tiers]);

  useEffect(() => {
    if (!ref.current) return;
    const ids = new Set(nodes.map((n) => n.id));
    const sprites = new Map<string, any>();  // SpriteText extiende THREE.Sprite; el .d.ts no expone la herencia
    const fg = new ForceGraph3D(ref.current)
      .backgroundColor("#111713")
      .graphData({
        nodes: nodes.map((n) => ({ ...n })),
        links: edges
          .filter((e) => ids.has(e.source) && ids.has(e.target))
          .map((e) => ({ source: e.source, target: e.target, type: e.type, weight: e.weight })),
      })
      .nodeLabel((n: any) => `${n.label} · ${n.kind}`)
      .nodeColor((n: any) => nodeColor(n))
      .nodeVal((n: any) => (n.kind === "project" ? 8 : 1 + Math.min(6, Math.log1p(n.degree || 1))))
      .nodeThreeObjectExtend(true)
      .nodeThreeObject((n: any) => {
        if (!eligible.has(n.id)) return false as unknown as object;
        const tier = tiers.get(n.id) ?? 3;
        const s: any = new SpriteText(n.label?.slice(0, 40) || "", tier === 1 ? 6 : tier === 2 ? 3.5 : 2.5, "#f4ead6");
        s.material.depthWrite = false;
        s.backgroundColor = "rgba(17,23,19,0.55)";
        s.padding = 1;
        s.borderRadius = 2;
        s.position.set(0, 6, 0);
        s.visible = tier === 1;
        sprites.set(n.id, s);
        return s;
      })
      .linkColor((l: any) => edgeColor(l.type))
      .linkOpacity(0.35)
      .linkWidth((l: any) => Math.max(0.2, Math.min(2, (l.weight || 1) * 0.4)))
      .onNodeClick((n: any) => onSelect(n as NodeData))
      .showNavInfo(false);
    fgRef.current = fg;

    // Visibilidad por distancia de cámara: al acercarte aparecen más etiquetas (paridad con el 2D).
    const visTimer = setInterval(() => {
      const cam = fg.cameraPosition();
      const level = levelRef.current;
      const forceLevel = level === "auto" ? 0 : level;
      for (const n of fg.graphData().nodes as any[]) {
        const s = sprites.get(n.id);
        if (!s || n.x === undefined) continue;
        const tier = tiers.get(n.id) ?? 3;
        if (forceLevel > 0) {
          s.visible = tier <= forceLevel;
          continue;
        }
        const dx = cam.x - n.x, dy = cam.y - n.y, dz = cam.z - n.z;
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
        s.visible = sizeForKind(n.kind, n.degree) / dist > VIS_K || tier === 1;
      }
    }, 300);

    const el = ref.current;
    const resize = () => fg.width(el.clientWidth).height(el.clientHeight);
    resize();
    const ro = new ResizeObserver(resize); // reacciona al arrastre de los divisores, no solo a window
    ro.observe(el);
    window.addEventListener("resize", resize);
    return () => {
      clearInterval(visTimer);
      ro.disconnect();
      window.removeEventListener("resize", resize);
      fg._destructor?.();
      fgRef.current = null;
    };
  }, [nodes, edges, eligible, tiers, nodeColor, edgeColor, onSelect]);

  return <div ref={ref} className="canvas" />;
}
