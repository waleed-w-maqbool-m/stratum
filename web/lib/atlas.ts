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

export type AtlasEdge = {
  from: string;
  to: string;
  confidence: number;
  on: string;
  conditional: boolean;
  /** False when the join was read out of shipped SQL rather than inferred. */
  inferred: boolean;
};

export type AtlasData = {
  database: string;
  version: number;
  nodes: AtlasNode[];
  edges: AtlasEdge[];
  domains: number;
  summary: {
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
