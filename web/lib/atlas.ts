import { DEMO, demoJson } from "./demo";

export type AtlasNode = {
  id: string;
  entity: string | null;
  class: "core" | "archive" | "staging" | "backup" | "dead";
  rows: number;
  columns: number;
  domain: number;
  position: [number, number, number];
  filters: number;
  unreviewed: number;
};

/**
 * Where a relationship came from. `names` only occurs on the paste-your-schema page,
 * which has no data to test a guess against.
 */
export type Origin = "declared" | "shipped_sql" | "data" | "llm" | "conditional" | "names";

export const ORIGIN_LABEL: Record<Origin, string> = {
  declared: "Declared in the schema",
  shipped_sql: "Written in your views and procedures",
  data: "Inferred from the data",
  llm: "Guessed by the language model",
  conditional: "Holds only for some rows",
  names: "Guessed from column names",
};

/** Edge colours by origin; the declared and code-written ones are the solid ones. */
export const ORIGIN_COLOR: Record<Origin, string> = {
  declared: "#ffb23f",
  shipped_sql: "#d9a441",
  data: "#9bc48a",
  names: "#9bc48a",
  llm: "#8aa4c8",
  conditional: "#d98080",
};

export type EdgeEvidence = {
  inclusion: number | null;
  values_tested: number | null;
  measured: boolean;
  orphan_count: number | null;
  orphan_samples: string[];
  name_similarity: number | null;
  parent_unique: boolean | null;
  needs_cast: boolean | null;
  sources: string[];
  fk_name: string;
  enforced: boolean | null;
  chosen_by: string;
};

export type AtlasEdge = {
  from: string;
  to: string;
  confidence: number;
  on: string;
  conditional: boolean;
  /** False when the catalog or shipped SQL states the join; true when it is a guess. */
  inferred: boolean;
  /** Absent in demo recordings made before origins existed; use `originOf`. */
  origin?: Origin;
  corroborated_by?: Origin[];
  reason?: string;
  cardinality?: "many_to_one" | "one_to_one" | "one_to_many";
  optional?: boolean;
  from_columns?: string[];
  to_columns?: string[];
  physical_on?: string;
  condition?: string;
  status?: string;
  evidence?: EdgeEvidence;
};

export function originOf(edge: AtlasEdge): Origin {
  if (edge.origin) return edge.origin;
  return edge.conditional ? "conditional" : edge.inferred ? "data" : "shipped_sql";
}

export function cardinalityText(edge: AtlasEdge): string {
  const base =
    edge.cardinality === "one_to_one"
      ? "one to one"
      : edge.cardinality === "one_to_many"
        ? "one to many"
        : "many to one";
  return edge.optional ? `${base}, optional` : base;
}

export type AtlasData = {
  database: string;
  version: number;
  nodes: AtlasNode[];
  edges: AtlasEdge[];
  domains: number;
  summary: {
    relationships_by_origin?: Record<string, number>;
    core: number;
    excluded: number;
    by_class: Record<string, number>;
  };
};

export async function fetchAtlas(databaseId: string): Promise<AtlasData> {
  if (DEMO) return demoJson<AtlasData>(`/databases/${databaseId}/atlas`);
  const response = await fetch(`/api/databases/${databaseId}/atlas`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<AtlasData>;
}

/** Respect the OS setting, and react if it changes while the page is open. */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
