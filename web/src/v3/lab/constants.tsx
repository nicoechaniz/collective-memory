import { Clock3, GitCompareArrows, Orbit, ScanSearch, Split, Waypoints } from "lucide-react";

export const STRUCTURAL_ONLY = import.meta.env.VITE_LAB_STRUCTURAL_ONLY === "1";
export const STRUCTURAL_NOTICE = import.meta.env.VITE_LAB_STRUCTURAL_NOTICE
  || "Esta instancia ejecuta puentes, fronteras y outliers sobre vectores precomputados. No carga un LLM ni ocupa GPU; los resultados son precandidatos estructurales para revisión humana.";

export const OPERATOR_HELP: Record<string, { label: string; short: string; long: string; icon: typeof Waypoints }> = {
  latent_bridge: {
    label: "Puentes latentes",
    short: "Conexiones útiles todavía no enlazadas.",
    long: "Conexiones entre proyectos que el sistema ve en el espacio semántico pero que todavía no están enlazadas editorialmente. Pregunta si lo que un proyecto resolvió le serviría concretamente a otro.",
    icon: Waypoints,
  },
  cluster_frontier: {
    label: "Fronteras",
    short: "Zonas de contacto subexploradas.",
    long: "Pares de proyectos con muchas cercanías semánticas y pocos links editoriales. Señala una zona de contacto que merece trabajo curatorial.",
    icon: Split,
  },
  outlier: {
    label: "Outliers",
    short: "Material cuyo barrio conceptual está en otro lugar.",
    long: "Documentos cuyo barrio semántico real parece ser otro proyecto. Puede indicar material reusable, citable, fuera de lugar o reubicable.",
    icon: Orbit,
  },
  tension: {
    label: "Tensiones",
    short: "Afirmaciones incompatibles que necesitan contraste.",
    long: "Documentos que afirman cosas incompatibles. No es mera complementariedad: debe existir desacuerdo real, aunque pueda requerir mediadores o contexto.",
    icon: GitCompareArrows,
  },
  analogy: {
    label: "Analogías",
    short: "La misma forma relacional en dominios distintos.",
    long: "Isomorfismos estructurales entre dominios distintos: problema, restricción, solución y costo, junto con el punto donde la analogía deja de funcionar.",
    icon: ScanSearch,
  },
  freshness_negative: {
    label: "Frescura",
    short: "Evidencia nueva que debilita lo publicado.",
    long: "Evidencia reciente que actualiza, debilita o contradice un nodo publicado. Detecta conocimiento que necesita revisión.",
    icon: Clock3,
  },
};

export const ROLE_LABELS: Record<string, string> = {
  support: "evidencia",
  counter: "contraevidencia",
  bridge: "puente",
  target: "afectado",
  context: "contexto",
};

export const EDGE_ROLE: Record<string, string> = {
  source_of: "support",
  challenges: "counter",
  bridges: "bridge",
  flags_freshness: "target",
  contains: "context",
};

export const STATUS_LABELS: Record<string, string> = {
  candidate: "candidato",
  interesting: "interesante",
  actionable: "accionable",
  discarded: "descartado",
  proposed: "propuesto",
  importing: "importándose",
  imported: "publicado",
  queued: "en cola",
  running: "corriendo",
  done: "listo",
  error: "error",
};

export const LOCKED_STATUSES = new Set(["discarded", "proposed", "importing", "imported"]);
