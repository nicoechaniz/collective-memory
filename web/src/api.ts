// Tipos compartidos + fetch JSON del Atlas.

export type NodeData = {
  id: string;
  label: string;
  kind: string;
  project: string;
  title: string;
  doc_id: string | null;
  path_rel: string;
  degree: number;
  curated: boolean;
  x: number;
  y: number;
};

export type EdgeData = {
  id: string;
  source: string;
  target: string;
  type: string;
  weight: number;
  directed: boolean;
};

export type GraphPayload = {
  schema_version: number;
  generation: string;
  truncated: boolean;
  nodes: NodeData[];
  edges: EdgeData[];
  counts: { nodes: number; edges: number; by_kind: Record<string, number>; by_project: Record<string, number>; by_edge_type: Record<string, number> };
};

export type CrossPayload = {
  schema_version: number;
  truncated: boolean;
  edges: (EdgeData & { source_project: string; target_project: string })[];
};

export type Manifest = {
  schema_version: number;
  generation: string;
  projects: { project: string; slug: string; graph_file: string; count: number }[];
};

// F21: partición Leiden servida (barrios semánticos persistentes de la generación vigente).
export type CommunitiesArtifact = {
  index_generation: number | string;
  ui_generation: string;
  partition_hash: string;
  algorithm: string;
  n_communities: number;
  params: Record<string, unknown>;
  universe: { excluded_kinds: string[]; n_docs: number };
  communities: Record<string, number>; // doc_id → community id
  sizes: Record<string, number>;
  pair_density: Record<string, number>;
};

// Fetch no-lanzante: 404 (build fast / no emitido) o error → null y el Atlas cae al Louvain client-side.
export async function fetchCommunities(): Promise<CommunitiesArtifact | null> {
  try {
    const res = await fetch("/ui/communities");
    if (!res.ok) return null;
    return (await res.json()) as CommunitiesArtifact;
  } catch {
    return null;
  }
}

export type TreeNode = {
  id: string;
  label: string;
  path_rel: string;
  kind: string;
  project: string;
  parent: string | null;
  doc_id: string | null;
  child_count: number;
  doc_count: number;
};

export type TreePayload = {
  schema_version: number;
  nodes: TreeNode[];
  roots: string[];
  excluded_summary: Record<string, number>;
  excluded_summary_stale: boolean;
};

export type SearchResult = {
  doc_id: string;
  kind: string;
  project: string;
  title: string;
  heading: string | null;
  snippet: string | null;
  score: number;
};

export type DocPayload = { doc_id: string; kind: string; project: string; title: string; body: string };

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const data = await res.json();
  if (data.schema_version && data.schema_version !== 1) throw new Error(`schema_version incompatible: ${data.schema_version}`);
  return data;
}
