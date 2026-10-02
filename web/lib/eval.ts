/**
 * The benchmark's published results, typed, and the comparisons built from them.
 *
 * Everything here is read from files the gauntlet writes after each run. The engine
 * never computes a score or opens the answer key; it only serves these files.
 */

import { DEMO, demoJson } from "./demo";

export type Bucket = { correct: number; total: number; ex: number };

export type Reliability = {
  range: [number, number];
  count: number;
  predicted: number;
  observed: number;
  gap: number;
};

export type Report = {
  suite: string;
  database: string;
  tag: string;
  mode: string;
  partial?: boolean;
  questions_scored: number;
  questions_answered?: number;
  execution_accuracy: number;
  per_tag: Record<string, Bucket>;
  mean_tokens: number;
  mean_latency_ms: number;
  models?: Record<string, number>;
  behaviour?: Record<string, { rate?: number; total?: number; triggered?: number }>;
  clarification?: { precision: number; recall: number };
  calibration?: {
    observations: number;
    ece_before: number;
    ece_after: number;
    monotonic: boolean;
    reliability_before: Reliability[];
    reliability_after: Reliability[];
  };
  has_questions: boolean;
};

export type Preview = { columns: string[]; rows: unknown[][]; total: number };

export type QuestionRow = {
  id: string;
  question: string;
  tags: string[];
  difficulty: string;
  expect: string | null;
  answered: boolean;
  correct?: boolean;
  reason?: string;
  status?: string;
  tokens?: number;
  confidence?: number | null;
  logical_sql?: string;
  physical_sql?: string;
  message?: string;
  assumptions?: string[];
  gold?: Preview | { error: string };
  gold_sql?: string;
  ours?: Preview;
};

export type Recall = Record<
  string,
  { precision?: number; recall?: number; f1?: number; accuracy?: number;
    found?: number; total?: number; decoded?: number; agree?: number }
>;

async function get<T>(path: string): Promise<T> {
  if (DEMO) return demoJson<T>(path);
  const response = await fetch(`/api${path}`);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}

export const evalApi = {
  reports: () => get<Report[]>("/eval/reports"),
  questions: (suite: string, tag: string) => get<QuestionRow[]>(`/eval/questions/${suite}/${tag}`),
  recall: (suite: string) => get<Recall>(`/eval/recall/${suite}`),
};

/** Plain names for the anti-pattern tags, as a person would say them. */
export const TAG_NAMES: Record<string, string> = {
  inconsistent_types: "Mixed types",
  string_quality: "Messy strings",
  drift: "Schema drift",
  god_table: "A god table",
  ambiguous: "Vague questions",
  embedded_format: "Formats inside columns",
  soft_delete: "Rows marked deleted",
  missing_fk: "Missing foreign keys",
  cryptic_names: "Cryptic names",
  composite_key: "Keys spread over columns",
  magic_numbers: "Coded values",
  misleading_docs: "Wrong documentation",
  dialect_friction: "Dialect quirks",
  sprawl: "Table sprawl",
  perf_trap: "Expensive queries",
  abstention: "Unanswerable questions",
};

export const SUITE_NAMES: Record<string, { name: string; engine: string }> = {
  chinook_v1: { name: "Chinook", engine: "SQLite" },
  pagila_v1: { name: "Pagila", engine: "Postgres 16" },
};

export function modelOf(report?: Report): string {
  const key = Object.keys(report?.models ?? {})[0];
  return key ? key.split(":").slice(1).join(":").replace(/^openai\//, "").replace(/^qwen\//, "") : "";
}

export type Paired = {
  n: number;
  baseline: number;
  stratum: number;
  baselineTokens: number;
  stratumTokens: number;
  byTag: { tag: string; n: number; baseline: number; stratum: number }[];
};

/**
 * Baseline against Stratum on exactly the questions both answered.
 *
 * A baseline stopped by a daily limit covers fewer questions than the full run; scoring
 * each over its own set would compare different exams. Pairing keeps it one exam.
 */
export function pair(baseline: QuestionRow[], stratum: QuestionRow[]): Paired {
  const theirs = new Map(baseline.map((q) => [q.id, q]));
  const both = stratum.filter(
    (q) =>
      q.answered && q.correct !== undefined && !q.expect &&
      theirs.get(q.id)?.answered && theirs.get(q.id)?.correct !== undefined,
  );
  const rate = (rows: (QuestionRow | undefined)[]) =>
    rows.length ? rows.filter((r) => r?.correct).length / rows.length : 0;
  const mean = (values: number[]) =>
    values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;

  const tags = Array.from(new Set(both.flatMap((q) => q.tags)));
  const byTag = tags
    .map((tag) => {
      const rows = both.filter((q) => q.tags.includes(tag));
      return {
        tag,
        n: rows.length,
        baseline: rate(rows.map((q) => theirs.get(q.id))),
        stratum: rate(rows),
      };
    })
    .filter((row) => row.n >= 2)
    .sort((a, b) => a.baseline - b.baseline || b.n - a.n);

  return {
    n: both.length,
    baseline: rate(both.map((q) => theirs.get(q.id))),
    stratum: rate(both),
    baselineTokens: mean(both.map((q) => theirs.get(q.id)?.tokens ?? 0)),
    stratumTokens: mean(both.map((q) => q.tokens ?? 0)),
    byTag,
  };
}

export const pct = (value: number) => `${Math.round(value * 100)}%`;
