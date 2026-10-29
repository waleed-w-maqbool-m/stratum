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

/** A link Stratum thinks is missing, drawn faintly between the two tables it names. */
export type GhostEdge = {
  from: string;
  to: string;
  gap_type: GapType;
  confidence: number;
  reason: string;
  id: string;
  status: string;
};

export type GapType =
  | "undeclared_fk"
  | "broken_relationship"
  | "dangling_reference"
  | "points_outside_core"
  | "missing_unique"
  | "missing_lookup"
  | "isolated_table";

export const GAP_LABEL: Record<GapType, { title: string; blurb: string }> = {
  undeclared_fk: {
    title: "Keys nothing enforces",
    blurb: "Stratum is confident these joins are real, but the database has no foreign key for them.",
  },
  broken_relationship: {
    title: "Relationships worn away",
    blurb: "The names match and most values do. The rest point at rows that were deleted.",
  },
  points_outside_core: {
    title: "Pointing into an archive",
    blurb: "The live table does not hold these values; a backup or archive table does.",
  },
  dangling_reference: {
    title: "References to nowhere",
    blurb: "Named like a reference, but most of the values match nothing.",
  },
  missing_unique: {
    title: "Keys that are not unique",
    blurb: "The values match, but the column they match holds duplicates, so it cannot be a key.",
  },
  missing_lookup: {
    title: "Codes with no lookup table",
    blurb: "A coded column whose meaning was found in code or inferred, but no table holds it.",
  },
  isolated_table: {
    title: "Tables with no relationships",
    blurb: "Nothing was found to join these to the rest of the database.",
  },
};

export type Gap = {
  id: string;
  gap_type: GapType;
  table: string;
  columns: string[];
  to_table: string;
  to_columns: string[];
  reason: string;
  fix: string;
  blockers: string[];
  confidence: number;
  status: string;
  evidence: Record<string, unknown>;
};

export type GapsResponse = {
  total: number;
  by_type: Record<string, number>;
  items: Gap[];
};

export type AtlasData = {
  database: string;
  version: number;
  nodes: AtlasNode[];
  edges: AtlasEdge[];
  /** Absent in demo recordings made before gaps existed. */
  ghost_edges?: GhostEdge[];
  domains: number;
  view?: "submitted" | "refined";
  summary: {
    relationships_by_origin?: Record<string, number>;
    gaps_by_type?: Record<string, number>;
    /** Present in the refined view only. */
    entities?: number;
    columns_renamed?: number;
    columns_decoded?: number;
    soft_delete_rules?: number;
    /** Present in the submitted view only. */
    views?: number;
    columns?: number;
    core: number;
    excluded: number;
    by_class: Record<string, number>;
  };
};

export type AtlasView = "submitted" | "refined";

export async function fetchAtlas(
  databaseId: string,
  view: AtlasView = "refined",
): Promise<AtlasData> {
  if (DEMO) {
    // Recordings were made before the catalog was kept, so only the refined view exists.
    if (view === "submitted") throw new Error("not recorded");
    return demoJson<AtlasData>(`/databases/${databaseId}/atlas`);
  }
  const response = await fetch(`/api/databases/${databaseId}/atlas?view=${view}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<AtlasData>;
}

export type TableColumn = {
  physical: string;
  logical: string;
  type: string;
  nullable: boolean | null;
  is_primary_key: boolean;
  references: string;
  reference_origin?: string;
  decoded: Record<string, string>;
  derived: boolean;
  expr: string;
  description: string;
  status: string;
  confidence: number | null;
};

export type TableDetail = {
  table: string;
  view: AtlasView;
  entity: string | null;
  kind: string;
  reason: string;
  description: string;
  rows: number;
  primary_key: string[];
  columns: TableColumn[];
  filters: { expr: string; reason: string; kind: string; rows_excluded: number; status: string }[];
  gaps: Pick<
    Gap,
    "id" | "gap_type" | "columns" | "to_table" | "table" | "reason" | "fix" | "blockers" | "confidence" | "status"
  >[];
};

/** The inspector's data. A recording or an older engine has none; callers show less. */
export async function fetchTable(
  databaseId: string,
  table: string,
  view: AtlasView,
): Promise<TableDetail | null> {
  if (DEMO) return null;
  try {
    const response = await fetch(
      `/api/databases/${databaseId}/tables/${encodeURIComponent(table)}?view=${view}`,
    );
    return response.ok ? ((await response.json()) as TableDetail) : null;
  } catch {
    return null;
  }
}

/** The suggestions list. A recording or an older engine without it just has none. */
export async function fetchGaps(databaseId: string): Promise<GapsResponse> {
  const empty: GapsResponse = { total: 0, by_type: {}, items: [] };
  try {
    if (DEMO) return await demoJson<GapsResponse>(`/databases/${databaseId}/gaps`);
    const response = await fetch(`/api/databases/${databaseId}/gaps?limit=200`);
    return response.ok ? ((await response.json()) as GapsResponse) : empty;
  } catch {
    return empty;
  }
}

/** Respect the OS setting, and react if it changes while the page is open. */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
