import type { EdgeData, GraphPayload, NodeData } from "../api";

export type { EdgeData, GraphPayload, NodeData };

export type Meta = {
  generation: string | null;
  index_generation: string | null;
  semantic_generation: string | null;
  semantic_available: boolean;
  semantic_fresh: boolean;
  total: number | null;
  shown: number | null;
  truncated: boolean;
  selection_policy: string;
};

export type Envelope<T> = { meta: Meta; data: T };

export type Project = {
  project_id: string;
  title: string;
  category: string;
  description: string;
  status: string;
  map_doc_id: string;
  color: string;
  doc_count: number;
  unique_count: number;
  vector_count: number;
  sort_order: number;
};

export type ManifestProject = { project: string; slug: string; graph_file: string; count: number };
export type Manifest = { schema_version: number; generation: string; projects: ManifestProject[] };

export type Bootstrap = {
  system: {
    name: string;
    read_only: boolean;
    total_docs: number;
    unique_docs: number;
    vectorized_docs: number;
    curated_projects: number;
    raw_scopes: number;
  };
  projects: Project[];
  counts_by_kind: Record<string, number>;
  counts_by_abstraction: Record<string, number>;
  views: string[];
};

export type DocMeta = {
  doc_id: string;
  project_id: string | null;
  raw_project: string;
  title: string;
  kind: string;
  abstraction: "raw" | "synthesis" | "curated";
  path_rel: string;
  size_bytes: number;
  mtime: number;
  vectorized: number;
  duplicate_of: string | null;
};

export type DocPayload = { doc_id: string; kind: string; project: string; title: string; body: string };
export type SearchHit = {
  doc_id: string;
  kind: string;
  project: string;
  title: string;
  snippet: string;
  heading: string | null;
  score: number;
};

export type Identity = {
  user: string;
  auth_mode: string;
  csrf: string | null;
  absolute_expires?: number | null;
};

export type Judge = { id: string; model: string; label: string; desc: string; default?: boolean };
export type OperatorConfig = {
  operators: string[];
  judges: Judge[];
  default_judge: string;
  mode?: "full" | "structural-only";
  llm_screening?: boolean;
  limits: { limit_max: number; queued_per_user: number; jobs_per_day: number };
};

export type Job = {
  id: number;
  owner: string;
  status: string;
  params_json: string;
  created_at: string;
  started_at?: string | null;
  finished_at: string | null;
  error: string | null;
};

export type CandidateRow = {
  id: string;
  owner: string;
  discovery_type: string;
  status: string;
  title: string | null;
  novelty_score: number | null;
  support_score: number | null;
  unexpectedness_score: number | null;
  flags_json: string;
  created_at: string;
};

export type CandidateSource = {
  role: string;
  doc_id: string;
  project: string | null;
  kind: string | null;
  title: string | null;
  snippet: string | null;
  score?: number | null;
  rank?: number | null;
};

export type CandidateReview = {
  reviewer: string;
  from_status: string;
  to_status: string;
  note: string;
  created_at: string;
};

export type CandidateDetail = CandidateRow & {
  claim: string | null;
  why_interesting: string | null;
  roles_json: string;
  falsification_json: string;
  generated_by: string | null;
  sources: CandidateSource[];
  reviews: CandidateReview[];
};

export type DirectorStatus = {
  running: boolean;
  last_digest: { mtime: number; title: string; veredicto: string } | null;
};

export type Campaign = {
  id: number;
  name: string;
  owner: string;
  status: string;
  created_at: string;
  finished_at: string | null;
};
