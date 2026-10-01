import type { ReviewItem } from "@/lib/api";

/**
 * Turning a detector's finding into something a person can decide on.
 *
 * Each finding arrives as a typed value plus raw evidence. The Workbench leads with one
 * sentence that states the claim in the schema's own names, because "accept" is only a
 * meaningful act when the thing being accepted is readable; the JSON stays one click
 * away for anyone who wants it.
 */

type Json = Record<string, unknown>;

/** Kinds in the order a reviewer should care about them, with the name a filter uses. */
export const KINDS: { kind: string; label: string }[] = [
  { kind: "soft_delete", label: "Deleted rows" },
  { kind: "god_table", label: "Mixed tables" },
  { kind: "overloaded", label: "Shared columns" },
  { kind: "drift", label: "Schema drift" },
  { kind: "enum", label: "Code values" },
  { kind: "join", label: "Relationships" },
  { kind: "key", label: "Keys" },
  { kind: "quality", label: "Messy values" },
  { kind: "type_mismatch", label: "Types" },
  { kind: "embedded_format", label: "Embedded formats" },
  { kind: "table_class", label: "Leftover tables" },
  { kind: "naming", label: "Names" },
];

const SINGULAR: Record<string, string> = {
  soft_delete: "Deleted rows",
  god_table: "Mixed table",
  overloaded: "Shared column",
  drift: "Schema drift",
  enum: "Code values",
  join: "Relationship",
  key: "Key",
  quality: "Messy values",
  type_mismatch: "Type",
  embedded_format: "Embedded format",
  table_class: "Leftover table",
  naming: "Name",
  perf: "Performance",
};

export function kindLabel(kind: string): string {
  return SINGULAR[kind] ?? kind.replace(/_/g, " ");
}

/** Only the kinds whose value a person can sensibly type a correction for. */
export function editable(item: ReviewItem): boolean {
  return item.kind === "naming" || item.kind === "enum";
}

/** `public.cstmr` reads as `cstmr`: every table on Postgres lives in a schema. */
export function short(name: unknown): string {
  return String(name ?? "").replace(/^public\./, "");
}

function list(values: unknown, limit = 3): string {
  const items = (Array.isArray(values) ? values : []).map(String);
  if (items.length <= limit) return items.join(", ");
  return `${items.slice(0, limit).join(", ")} and ${items.length - limit} more`;
}

const CLASS_MEANINGS: Record<string, string> = {
  dead: "a leftover work table",
  archive: "an archive of old rows",
  backup: "a backup copy of another table",
  staging: "a staging table for loads",
  audit: "an audit log",
};

/** The claim, in one sentence. */
export function headline(item: ReviewItem): string {
  const v = (item.value ?? {}) as Json;
  switch (item.kind) {
    case "soft_delete":
      return `Rows of ${short(v.table)} where ${v.deleted_predicate} count as deleted`;
    case "enum": {
      const pairs = Object.entries(v).map(([code, meaning]) => `${code} means ${meaning}`);
      if (!pairs.length) return `${short(item.target)} looks like a code column, but no meaning was found for its values`;
      const shown = pairs.slice(0, 3).join(", ");
      return `${short(item.target)} is a code: ${shown}${pairs.length > 3 ? ", …" : ""}`;
    }
    case "god_table": {
      const kinds = Object.keys((v.entities as Json) ?? {}).length;
      return `${short(v.table)} holds ${kinds} kinds of record, told apart by ${v.discriminator}`;
    }
    case "overloaded":
      return `${short(v.table)}.${v.column} points at different tables depending on ${v.discriminator}`;
    case "drift":
      if (v.kind === "column_relocation") return `${short(v.old)} moved to ${short(v.new)} partway through`;
      return reasons(item)[0] ?? `${short(item.target)} changed meaning over time`;
    case "join": {
      const from = `${short(v.from_table)}.${list(v.from_columns)}`;
      const to = `${short(v.to_table)}.${list(v.to_columns)}`;
      return `${from} refers to ${to}`;
    }
    case "key":
      return `${list(v.columns)} identifies each row of ${short(item.target)}`;
    case "quality":
      return `${short(v.table)}.${v.column} spells the same value more than one way`;
    case "type_mismatch":
      if (v.kind === "cross_table") {
        const forms = Object.keys((v.storage_forms as Json) ?? {}).length;
        return `"${v.meaning}" is stored in ${forms} different forms across the schema`;
      }
      return reasons(item)[0] ?? `${short(item.target)} is stored as the wrong type`;
    case "table_class":
      return `${short(item.target)} looks like ${CLASS_MEANINGS[String(item.value)] ?? String(item.value)}`;
    case "naming":
      return `${short(item.target)} means "${String(v.logical_name ?? "").replace(/_/g, " ")}"`;
    default:
      return reasons(item)[0] ?? short(item.target);
  }
}

/** What accepting it will do to answers. Stated, so nobody accepts blind. */
export function consequence(item: ReviewItem): string {
  const v = (item.value ?? {}) as Json;
  switch (item.kind) {
    case "soft_delete":
      return `Every answer about ${short(v.table)} will leave these rows out. Rejecting brings them back.`;
    case "enum":
      return "Answers will match these meanings to the stored codes, so a question can say “active” instead of “A”.";
    case "god_table":
      return "Each kind becomes its own entity, so a question about one kind never counts rows of another.";
    case "overloaded":
      return "Joins through this column will add the condition that says which table it points at.";
    case "drift":
      return "Queries will read the value from wherever it lived at the time, instead of only the old place.";
    case "join":
      return "Questions that span both tables will join them this way.";
    case "key":
      return "Counts and joins will treat this as the row's identity.";
    case "quality":
      return "Grouping on this column will merge the spellings into one value.";
    case "type_mismatch":
      return "Comparisons will convert the stored form first, so they compare like with like.";
    case "table_class":
      return "Answers will not draw from this table.";
    case "naming":
      return "Questions can use this name, and the SQL shown to you will too.";
    default:
      return "";
  }
}

export function reasons(item: ReviewItem): string[] {
  const evidence = (item.evidence ?? {}) as Json;
  const out: string[] = [];
  if (typeof evidence.reason === "string") out.push(evidence.reason);
  if (Array.isArray(evidence.reasons)) out.push(...evidence.reasons.map(String));
  return out;
}

/** The numbers behind the claim, each with a label a person would use. */
export function facts(item: ReviewItem): [string, string][] {
  const e = (item.evidence ?? {}) as Json;
  const out: [string, string][] = [];
  // 1.5% of rows is not 1%: small shares keep a decimal, since they are the ones that matter here.
  const pct = (n: unknown) => {
    const value = Number(n) * 100;
    return `${value > 0 && value < 10 ? value.toFixed(1).replace(/\.0$/, "") : Math.round(value)}%`;
  };
  const count = (n: unknown) => Number(n).toLocaleString();

  switch (item.kind) {
    case "soft_delete":
      if (e.rows_affected != null) out.push(["Rows it would hide", count(e.rows_affected)]);
      if (e.affected_fraction != null) out.push(["Share of the table", pct(e.affected_fraction)]);
      break;
    case "join":
      if (e.inclusion != null) out.push(["Values found in the other table", pct(e.inclusion)]);
      if (e.distinct_values_tested != null) out.push(["Distinct values tested", count(e.distinct_values_tested)]);
      if (e.parent_coverage != null) out.push(["Of the other table's rows, referenced", pct(e.parent_coverage)]);
      if (e.name_similarity != null) out.push(["How alike the column names are", pct(e.name_similarity)]);
      if ((item.value as Json)?.needs_cast) out.push(["Types differ", "joined with a cast"]);
      break;
    case "key":
      if (e.rows_checked != null) out.push(["Rows checked for duplicates", count(e.rows_checked)]);
      if ((item.value as Json)?.declared === false) out.push(["Declared in the schema", "no"]);
      break;
    case "god_table":
      if (e.column_count != null) out.push(["Columns", count(e.column_count)]);
      if (e.exclusivity != null) out.push(["Columns used by only one kind", pct(e.exclusivity)]);
      break;
    case "enum":
      if (e.distinct_values != null) out.push(["Distinct codes", count(e.distinct_values)]);
      break;
    case "drift":
      if (e.null_fraction != null) out.push(["Rows missing the old value", pct(e.null_fraction)]);
      break;
    case "table_class":
      if (e.rows != null) out.push(["Rows", count(e.rows)]);
      break;
  }
  return out;
}

/** A SQL expression the finding would put into queries, when it has one. */
export function expression(item: ReviewItem): string {
  const v = (item.value ?? {}) as Json;
  const e = (item.evidence ?? {}) as Json;
  if (item.kind === "soft_delete") return String(v.live_predicate ?? "");
  if (item.kind === "join") return String(v.expression ?? "");
  if (item.kind === "drift") return String(v.resolution ?? "");
  if (item.kind === "quality") return String(e.canonical_expression ?? "");
  if (typeof v.expression === "string") return v.expression;
  return "";
}

export function impactWord(impact: number): string {
  if (impact >= 0.25) return "many answers";
  if (impact >= 0.1) return "some answers";
  return "few answers";
}
