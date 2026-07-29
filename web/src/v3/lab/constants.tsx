import { Clock3, GitCompareArrows, Orbit, ScanSearch, Split, Waypoints } from "lucide-react";
import { t } from "../../i18n";

export const STRUCTURAL_ONLY = import.meta.env.VITE_LAB_STRUCTURAL_ONLY === "1";
export const STRUCTURAL_NOTICE = import.meta.env.VITE_LAB_STRUCTURAL_NOTICE
  || t(
    "Esta instancia ejecuta puentes, fronteras y outliers sobre vectores precomputados. No carga un LLM ni ocupa GPU; los resultados son precandidatos estructurales para revisión humana.",
    "This instance runs latent bridges, frontiers, and outliers over precomputed vectors. It does not load an LLM or use the GPU; results are structural pre-candidates for human review.",
  );

export const OPERATOR_HELP: Record<string, { label: string; short: string; long: string; icon: typeof Waypoints }> = {
  latent_bridge: {
    label: t("Puentes latentes", "Latent bridges"),
    short: t("Conexiones útiles todavía no enlazadas.", "Useful connections that have not been linked yet."),
    long: t("Conexiones entre proyectos que el sistema ve en el espacio semántico pero que todavía no están enlazadas editorialmente. Pregunta si lo que un proyecto resolvió le serviría concretamente a otro.", "Connections between projects that are close in semantic space but have not been linked editorially. It asks whether a solution from one project could be concretely useful to another."),
    icon: Waypoints,
  },
  cluster_frontier: {
    label: t("Fronteras", "Frontiers"),
    short: t("Zonas de contacto subexploradas.", "Underexplored contact zones."),
    long: t("Pares de proyectos con muchas cercanías semánticas y pocos links editoriales. Señala una zona de contacto que merece trabajo curatorial.", "Pairs of projects with strong semantic proximity and few editorial links. It highlights a contact zone that deserves curatorial work."),
    icon: Split,
  },
  outlier: {
    label: "Outliers",
    short: t("Material cuyo barrio conceptual está en otro lugar.", "Material whose conceptual neighborhood lies elsewhere."),
    long: t("Documentos cuyo barrio semántico real parece ser otro proyecto. Puede indicar material reusable, citable, fuera de lugar o reubicable.", "Documents whose actual semantic neighborhood appears to belong to another project. This may reveal reusable, citable, misplaced, or relocatable material."),
    icon: Orbit,
  },
  tension: {
    label: t("Tensiones", "Tensions"),
    short: t("Afirmaciones incompatibles que necesitan contraste.", "Incompatible claims that need to be contrasted."),
    long: t("Documentos que afirman cosas incompatibles. No es mera complementariedad: debe existir desacuerdo real, aunque pueda requerir mediadores o contexto.", "Documents making incompatible claims. This is not mere complementarity: there must be a genuine disagreement, even if mediators or additional context are needed."),
    icon: GitCompareArrows,
  },
  analogy: {
    label: t("Analogías", "Analogies"),
    short: t("La misma forma relacional en dominios distintos.", "The same relational form across different domains."),
    long: t("Isomorfismos estructurales entre dominios distintos: problema, restricción, solución y costo, junto con el punto donde la analogía deja de funcionar.", "Structural isomorphisms across domains: problem, constraint, solution, and cost, together with the point where the analogy stops working."),
    icon: ScanSearch,
  },
  freshness_negative: {
    label: t("Frescura", "Freshness"),
    short: t("Evidencia nueva que debilita lo publicado.", "New evidence that weakens published knowledge."),
    long: t("Evidencia reciente que actualiza, debilita o contradice un nodo publicado. Detecta conocimiento que necesita revisión.", "Recent evidence that updates, weakens, or contradicts a published node. It detects knowledge that needs review."),
    icon: Clock3,
  },
};

export const ROLE_LABELS: Record<string, string> = {
  support: t("evidencia", "evidence"),
  counter: t("contraevidencia", "counter-evidence"),
  bridge: t("puente", "bridge"),
  target: t("afectado", "affected"),
  context: t("contexto", "context"),
};

export const EDGE_ROLE: Record<string, string> = {
  source_of: "support",
  challenges: "counter",
  bridges: "bridge",
  flags_freshness: "target",
  contains: "context",
};

export const STATUS_LABELS: Record<string, string> = {
  candidate: t("candidato", "candidate"),
  interesting: t("interesante", "interesting"),
  actionable: t("accionable", "actionable"),
  discarded: t("descartado", "discarded"),
  proposed: t("propuesto", "proposed"),
  importing: t("importándose", "importing"),
  imported: t("publicado", "published"),
  queued: t("en cola", "queued"),
  running: t("corriendo", "running"),
  done: t("listo", "done"),
  error: "error",
};

export const LOCKED_STATUSES = new Set(["discarded", "proposed", "importing", "imported"]);
