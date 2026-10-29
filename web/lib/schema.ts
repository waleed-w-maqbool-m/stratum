import type { AtlasData, AtlasEdge, AtlasNode } from "./atlas";

/**
 * Read a schema pasted as SQL and lay it out as an Atlas, entirely in the browser.
 *
 * This is deliberately a smaller thing than excavation. It sees what a schema file
 * shows: tables, columns, declared keys. From that it can say which tables look like
 * backups, archives or scratch copies (by their names), and guess at joins (by their
 * column names). It cannot see the data, so it cannot find a soft delete, a unit that
 * changed, a table holding three kinds of record, or what a code means. Those need the
 * engine, and the page says so. Nothing here is uploaded anywhere.
 */

export type Kind = "core" | "archive" | "staging" | "backup" | "dead";

export type ForeignKey = { columns: string[]; refTable: string; refColumns: string[] };

export type ParsedTable = {
  name: string;
  columns: string[];
  primaryKey: string[];
  foreignKeys: ForeignKey[];
};

export type TableJoin = { other: string; direction: "to" | "from"; on: string; declared: boolean };

export type TableDetail = {
  name: string;
  columns: string[];
  primaryKey: string[];
  kind: Kind;
  reason: string;
  joins: TableJoin[];
};

export type SchemaMap = {
  atlas: AtlasData;
  details: Map<string, TableDetail>;
  declared: number;
  guessed: number;
  /** Tables beyond the limit, left off the map. */
  omitted: number;
};

const MAX_TABLES = 400;

// ---------------------------------------------------------------------------- parsing

const IDENT = '(?:"[^"]+"|\\[[^\\]]+\\]|`[^`]+`|[A-Za-z_][\\w$]*)';
const QNAME = `${IDENT}(?:\\s*\\.\\s*${IDENT})*`;

const CREATE = new RegExp(
  `CREATE\\s+(?:(?:GLOBAL\\s+|LOCAL\\s+)?(?:TEMP|TEMPORARY)\\s+|UNLOGGED\\s+|OR\\s+REPLACE\\s+)?TABLE\\s+(?:IF\\s+NOT\\s+EXISTS\\s+)?(${QNAME})\\s*\\(`,
  "gi",
);
const ALTER_FK = new RegExp(
  `ALTER\\s+TABLE\\s+(?:ONLY\\s+)?(?:IF\\s+EXISTS\\s+)?(${QNAME})\\s+(?:WITH\\s+(?:NO)?CHECK\\s+)?ADD\\s+(?:CONSTRAINT\\s+${IDENT}\\s+)?FOREIGN\\s+KEY\\s*\\(([^)]*)\\)\\s*REFERENCES\\s+(${QNAME})\\s*(?:\\(([^)]*)\\))?`,
  "gi",
);
const TABLE_PK = /^(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]*)\)/i;
const TABLE_FK = new RegExp(
  `^(?:CONSTRAINT\\s+\\S+\\s+)?FOREIGN\\s+KEY\\s*\\(([^)]*)\\)\\s*REFERENCES\\s+(${QNAME})\\s*(?:\\(([^)]*)\\))?`,
  "i",
);
const NOT_A_COLUMN = /^(?:CONSTRAINT|UNIQUE|CHECK|KEY|INDEX|FULLTEXT|SPATIAL|EXCLUDE|PERIOD|LIKE)\b/i;
const INLINE_REF = new RegExp(`\\bREFERENCES\\s+(${QNAME})\\s*(?:\\(([^)]*)\\))?`, "i");

function unquote(raw: string): string {
  const parts = raw.match(new RegExp(IDENT, "g")) ?? [raw];
  return (parts[parts.length - 1] ?? raw).replace(/^["[`]|["\]`]$/g, "");
}

function identList(raw: string | undefined): string[] {
  return (raw ?? "")
    .split(",")
    .map((part) => unquote(part.trim().split(/\s+/)[0] ?? ""))
    .filter(Boolean);
}

/** The index of the parenthesis that closes the one at `open`, skipping quoted text. */
function closing(sql: string, open: number): number {
  let depth = 0;
  let quote = "";
  for (let i = open; i < sql.length; i++) {
    const ch = sql[i];
    if (quote) {
      if (ch === quote) quote = "";
    } else if (ch === "'" || ch === '"' || ch === "`") {
      quote = ch;
    } else if (ch === "(") {
      depth++;
    } else if (ch === ")" && --depth === 0) {
      return i;
    }
  }
  return -1;
}

function splitTopLevel(body: string): string[] {
  const items: string[] = [];
  let depth = 0;
  let quote = "";
  let start = 0;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (quote) {
      if (ch === quote) quote = "";
    } else if (ch === "'" || ch === '"' || ch === "`") {
      quote = ch;
    } else if (ch === "(") {
      depth++;
    } else if (ch === ")") {
      depth--;
    } else if (ch === "," && depth === 0) {
      items.push(body.slice(start, i));
      start = i + 1;
    }
  }
  items.push(body.slice(start));
  return items.map((item) => item.trim()).filter(Boolean);
}

export function parseTables(source: string): ParsedTable[] {
  const sql = source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/--[^\n]*/g, " ");
  const tables = new Map<string, ParsedTable>();

  CREATE.lastIndex = 0;
  for (let match = CREATE.exec(sql); match; match = CREATE.exec(sql)) {
    const open = match.index + match[0].length - 1;
    const end = closing(sql, open);
    if (end < 0) break;
    CREATE.lastIndex = end + 1;

    const table: ParsedTable = { name: unquote(match[1]), columns: [], primaryKey: [], foreignKeys: [] };
    for (const item of splitTopLevel(sql.slice(open + 1, end))) {
      const pk = TABLE_PK.exec(item);
      if (pk) {
        table.primaryKey.push(...identList(pk[1]));
        continue;
      }
      const fk = TABLE_FK.exec(item);
      if (fk) {
        table.foreignKeys.push({
          columns: identList(fk[1]),
          refTable: unquote(fk[2]),
          refColumns: identList(fk[3]),
        });
        continue;
      }
      if (NOT_A_COLUMN.test(item)) continue;

      const first = new RegExp(`^(${IDENT})\\s`).exec(item);
      if (!first) continue;
      const column = unquote(first[1]);
      table.columns.push(column);
      if (/\bPRIMARY\s+KEY\b/i.test(item)) table.primaryKey.push(column);
      const ref = INLINE_REF.exec(item);
      if (ref) {
        table.foreignKeys.push({
          columns: [column],
          refTable: unquote(ref[1]),
          refColumns: identList(ref[2]),
        });
      }
    }
    tables.set(table.name.toLowerCase(), table);
  }

  ALTER_FK.lastIndex = 0;
  for (let match = ALTER_FK.exec(sql); match; match = ALTER_FK.exec(sql)) {
    tables.get(unquote(match[1]).toLowerCase())?.foreignKeys.push({
      columns: identList(match[2]),
      refTable: unquote(match[3]),
      refColumns: identList(match[4]),
    });
  }
  return [...tables.values()];
}

// ----------------------------------------------------------------------- classification

const RULES: [Kind, RegExp, string][] = [
  [
    "backup",
    /(^|[^a-z])(bak|bkp|bkup|backup|copy|orig)([^a-z]|$)|(^|_)old(_|$)|_\d{8}$/i,
    "its name looks like a backup",
  ],
  [
    "archive",
    /archive|(^|[^a-z])(arch|hist|history|snapshot|snap|rpt)([^a-z]|$)|_(19|20)\d\d$/i,
    "its name looks like an archive or a report snapshot",
  ],
  [
    "staging",
    /(^|[^a-z])(stg|stage|staging|import|imp|feed|etl|load)([^a-z]|$)/i,
    "its name looks like a staging table",
  ],
  [
    "dead",
    /(^|[^a-z])(tmp|temp|scratch|junk|unused|test|zz|wrk|work|fix|dedupe|xx)([^a-z]|$)/i,
    "its name looks like a scratch table",
  ],
];

function classify(name: string): { kind: Kind; reason: string } {
  for (const [kind, pattern, reason] of RULES) {
    if (pattern.test(name)) return { kind, reason };
  }
  return { kind: "core", reason: "nothing in its name suggests a leftover" };
}

// --------------------------------------------------------------------------- the joins

/** `tblPrd`, `PRD_ID` and `Products` all reduce to roughly "prd" / "product". */
function tableStem(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z]/g, "")
    .replace(/^(tbl|tb|dim|fact|vw)/, "")
    .replace(/(mstr|master|hdr|header|tbl|table|tab|dtl|detail)$/, "")
    .replace(/s$/, "");
}

function columnStem(column: string): string {
  return column
    .toLowerCase()
    .replace(/_?(id|no|num|nbr|cd|key|ref)$/, "")
    .replace(/[^a-z]/g, "")
    .replace(/s$/, "");
}

type Link = { child: string; parent: string; on: string; declared: boolean; confidence: number };

function findLinks(tables: ParsedTable[], kinds: Map<string, Kind>): Link[] {
  const byName = new Map(tables.map((table) => [table.name.toLowerCase(), table]));
  const links: Link[] = [];
  const seen = new Set<string>();
  const add = (link: Link) => {
    const key = `${link.child}>${link.parent}`.toLowerCase();
    if (link.child === link.parent || seen.has(key)) return;
    seen.add(key);
    links.push(link);
  };

  for (const table of tables) {
    for (const fk of table.foreignKeys) {
      const parent = byName.get(fk.refTable.toLowerCase());
      if (!parent) continue;
      add({
        child: table.name,
        parent: parent.name,
        on: `${table.name}.${fk.columns.join(", ")} = ${parent.name}.${(fk.refColumns.length ? fk.refColumns : parent.primaryKey).join(", ")}`,
        declared: true,
        confidence: 1,
      });
    }
  }

  const live = tables.filter((table) => kinds.get(table.name) === "core");
  for (const table of tables) {
    if (kinds.get(table.name) !== "core") continue;
    for (const column of table.columns) {
      const lower = column.toLowerCase();
      // A column that is this table's own single-column key names itself, not a parent.
      if (table.primaryKey.length === 1 && table.primaryKey[0].toLowerCase() === lower) continue;

      // The strongest hint: another table's single-column key has exactly this name.
      const byKey = live.filter(
        (other) =>
          other.name !== table.name &&
          other.primaryKey.length === 1 &&
          other.primaryKey[0].toLowerCase() === lower,
      );
      if (byKey.length === 1) {
        add({
          child: table.name,
          parent: byKey[0].name,
          on: `${table.name}.${column} = ${byKey[0].name}.${byKey[0].primaryKey[0]}`,
          declared: false,
          confidence: 0.7,
        });
        continue;
      }

      // Otherwise, the column's name with its id suffix removed matches a table's name.
      const stem = columnStem(column);
      if (stem.length < 3 || stem === tableStem(table.name)) continue;
      const exact = live.filter((other) => other.name !== table.name && tableStem(other.name) === stem);
      const near = live.filter(
        (other) => other.name !== table.name && tableStem(other.name).startsWith(stem),
      );
      const match = exact.length === 1 ? exact[0] : exact.length === 0 && near.length === 1 ? near[0] : null;
      if (match) {
        add({
          child: table.name,
          parent: match.name,
          on: `${table.name}.${column} = ${match.name}.${match.primaryKey[0] ?? column}`,
          declared: false,
          confidence: 0.5,
        });
      }
    }
  }
  return links;
}

// --------------------------------------------------------------------------- the layout

/** A stable number in [0, 1) from a name, so the same schema always draws the same map. */
function unit(text: string, salt: number): number {
  let h = 2166136261 ^ salt;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

type Point = { x: number; z: number };

/** A small spring layout in the ground plane: tables repel, joins pull. */
function spread(names: string[], links: [string, string][]): Map<string, Point> {
  const points = new Map<string, Point>(
    names.map((name) => {
      const angle = unit(name, 1) * Math.PI * 2;
      const radius = 2 + unit(name, 2) * 5;
      return [name, { x: Math.cos(angle) * radius, z: Math.sin(angle) * radius }];
    }),
  );
  const steps = 180;
  for (let step = 0; step < steps; step++) {
    const cooling = 0.7 * (1 - step / steps) + 0.05;
    const push = new Map(names.map((name) => [name, { x: 0, z: 0 }]));
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        const a = points.get(names[i])!;
        const b = points.get(names[j])!;
        const dx = a.x - b.x;
        const dz = a.z - b.z;
        const d2 = dx * dx + dz * dz + 0.05;
        const force = 14 / d2;
        const d = Math.sqrt(d2);
        push.get(names[i])!.x += (dx / d) * force;
        push.get(names[i])!.z += (dz / d) * force;
        push.get(names[j])!.x -= (dx / d) * force;
        push.get(names[j])!.z -= (dz / d) * force;
      }
    }
    for (const [from, to] of links) {
      const a = points.get(from);
      const b = points.get(to);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dz = b.z - a.z;
      const d = Math.sqrt(dx * dx + dz * dz) + 0.01;
      const pull = 0.12 * (d - 3.5);
      push.get(from)!.x += (dx / d) * pull;
      push.get(from)!.z += (dz / d) * pull;
      push.get(to)!.x -= (dx / d) * pull;
      push.get(to)!.z -= (dz / d) * pull;
    }
    for (const name of names) {
      const p = points.get(name)!;
      const f = push.get(name)!;
      const length = Math.sqrt(f.x * f.x + f.z * f.z) + 1e-6;
      const move = Math.min(length, cooling);
      p.x += (f.x / length) * move - p.x * 0.01;
      p.z += (f.z / length) * move - p.z * 0.01;
    }
  }
  let widest = 0;
  for (const p of points.values()) widest = Math.max(widest, Math.hypot(p.x, p.z));
  const fit = widest > 9 ? 9 / widest : 1;
  for (const p of points.values()) {
    p.x *= fit;
    p.z *= fit;
  }
  return points;
}

// -------------------------------------------------------------------------- the result

export function readSchema(sql: string): SchemaMap | null {
  const parsed = parseTables(sql);
  if (!parsed.length) return null;
  const tables = parsed.slice(0, MAX_TABLES);

  const classes = new Map(tables.map((table) => [table.name, classify(table.name)]));
  const kinds = new Map([...classes].map(([name, c]) => [name, c.kind]));
  const links = findLinks(tables, kinds);

  const live = tables.filter((table) => kinds.get(table.name) === "core").map((table) => table.name);
  const placed = spread(
    live,
    links.map((link) => [link.child, link.parent] as [string, string]),
  );

  const nodes: AtlasNode[] = tables.map((table) => {
    const { kind } = classes.get(table.name)!;
    const columns = table.columns.length;
    let position: [number, number, number];
    if (kind === "core") {
      const p = placed.get(table.name)!;
      position = [p.x, -0.5 + Math.min(3.5, Math.log2(1 + columns) * 0.7), p.z];
    } else {
      const angle = unit(table.name, 3) * Math.PI * 2;
      const radius = 13 + unit(table.name, 4) * 7;
      position = [Math.cos(angle) * radius, -6 - unit(table.name, 5) * 3, Math.sin(angle) * radius];
    }
    return {
      id: table.name,
      entity: null,
      class: kind,
      rows: 0,
      columns,
      domain: 0,
      position,
      filters: 0,
      unreviewed: 0,
    };
  });

  const edges: AtlasEdge[] = links.map((link) => ({
    from: link.child,
    to: link.parent,
    confidence: link.confidence,
    on: link.on,
    conditional: false,
    inferred: !link.declared,
    origin: link.declared ? "declared" : "names",
  }));

  const details = new Map<string, TableDetail>();
  for (const table of tables) {
    const { kind, reason } = classes.get(table.name)!;
    details.set(table.name, {
      name: table.name,
      columns: table.columns,
      primaryKey: table.primaryKey,
      kind,
      reason,
      joins: [],
    });
  }
  for (const link of links) {
    details.get(link.child)?.joins.push({
      other: link.parent,
      direction: "to",
      on: link.on,
      declared: link.declared,
    });
    details.get(link.parent)?.joins.push({
      other: link.child,
      direction: "from",
      on: link.on,
      declared: link.declared,
    });
  }

  const byClass: Record<string, number> = {};
  for (const node of nodes) byClass[node.class] = (byClass[node.class] ?? 0) + 1;

  return {
    atlas: {
      database: "your schema",
      version: 0,
      nodes,
      edges,
      domains: 1,
      summary: {
        core: byClass.core ?? 0,
        excluded: nodes.length - (byClass.core ?? 0),
        by_class: byClass,
      },
    },
    details,
    declared: links.filter((link) => link.declared).length,
    guessed: links.filter((link) => !link.declared).length,
    omitted: parsed.length - tables.length,
  };
}

/** An invented legacy order system, for trying the map. It is not a benchmark database. */
export const EXAMPLE_SCHEMA = `-- An invented example of a legacy order system. Replace it with your own.
CREATE TABLE CUST_MSTR (CUST_ID INT PRIMARY KEY, CFNM1 CHAR(30), CLNM1 CHAR(40), EML VARCHAR(80), TERR_ID INT, DEL_FLG CHAR(1));
CREATE TABLE TERR (TERR_ID INT PRIMARY KEY, TERR_NM VARCHAR(50), CTRY_CD VARCHAR(3));
CREATE TABLE SO_HDR (SO_ID INT PRIMARY KEY, CUST_ID INT, SP_ID INT, ORD_DT CHAR(8), ORD_STS TINYINT, TOT_DUE FLOAT);
CREATE TABLE SO_LN (SO_ID INT, LN_SEQ SMALLINT, PRD_ID INT, QTY SMALLINT, LN_AMT FLOAT, PRIMARY KEY (SO_ID, LN_SEQ));
CREATE TABLE tblPrd (PRD_ID INT PRIMARY KEY, strNm VARCHAR(60), SCAT_ID INT, curLstPrc MONEY, dtSellEnd DATETIME);
CREATE TABLE PSCAT (SCAT_ID INT PRIMARY KEY, CAT_ID INT, SCAT_NM VARCHAR(50));
CREATE TABLE PCTGRY (CAT_ID INT PRIMARY KEY, CAT_NM VARCHAR(50));
CREATE TABLE SLSPRSN (SP_ID INT PRIMARY KEY, SP_FNM VARCHAR(50), SP_LNM VARCHAR(50), TERR_ID INT, QUOTA MONEY);
CREATE TABLE VNDR (VNDR_ID INT PRIMARY KEY, VNDR_NM VARCHAR(50), CR_RTG TINYINT);
CREATE TABLE PO_HDR (PO_ID INT PRIMARY KEY, VNDR_ID INT, PO_DT DATETIME, TOT_DUE FLOAT);
CREATE TABLE PO_LN (PO_ID INT, LN_SEQ SMALLINT, PRD_ID INT, QTY INT, PRIMARY KEY (PO_ID, LN_SEQ));
CREATE TABLE INV_BAL (PRD_ID INT, WHSE_CD VARCHAR(6), QTY_OH INT);
CREATE TABLE WHSE (WHSE_CD VARCHAR(6) PRIMARY KEY, WHSE_NM VARCHAR(40));
CREATE TABLE AUDIT_LOG (LOG_ID BIGINT PRIMARY KEY, TBL_NM VARCHAR(30), ACTN_CD CHAR(1), LOG_TS DATETIME);
CREATE TABLE CUST_MSTR_BAK_20140312 (CUST_ID INT, CFNM1 CHAR(30), CLNM1 CHAR(40));
CREATE TABLE CUST_MSTR_OLD (CUST_ID INT, CFNM1 CHAR(30));
CREATE TABLE SO_HDR_2011 (SO_ID INT, CUST_ID INT, ORD_DT CHAR(8));
CREATE TABLE SO_HDR_2012 (SO_ID INT, CUST_ID INT, ORD_DT CHAR(8));
CREATE TABLE STG_SO_IMPORT (ORD_NO INT, CUST_NO VARCHAR(10), LOAD_BATCH INT);
CREATE TABLE STG_CUST_LOAD (CUST_ID INT, CFNM1 CHAR(30));
CREATE TABLE RPT_SLS_MTHLY (MTH CHAR(6), REV FLOAT);
CREATE TABLE HIST_PRD_017 (PRD_ID INT, STRNM VARCHAR(60));
CREATE TABLE tmp_fix_0412 (ID INT, VAL VARCHAR(80));
CREATE TABLE tmp_dedupe_cust (ID INT, VAL VARCHAR(80));
CREATE TABLE zz_scratch (ID INT, NOTE VARCHAR(200));
ALTER TABLE PO_HDR ADD CONSTRAINT fk_po_vndr FOREIGN KEY (VNDR_ID) REFERENCES VNDR (VNDR_ID);
ALTER TABLE PO_LN ADD CONSTRAINT fk_poln_po FOREIGN KEY (PO_ID) REFERENCES PO_HDR (PO_ID);
`;
