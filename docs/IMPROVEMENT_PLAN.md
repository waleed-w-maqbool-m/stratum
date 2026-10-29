# Stratum improvement plan

Written 2026-10-29, after the Chinook failure attribution (`docs/ATTRIBUTION.md`). This
plan follows `docs/PLAN.md` and does not replace it; the non-negotiables in `CLAUDE.md`
still apply to every item below. Work the phases in order. Each one ends with acceptance
checks; do not start the next phase until they pass.

## Status (updated after the first implementation pass)

| Phase | State | What is left |
|---|---|---|
| A. Where each relationship came from | **Done** | `llm` as an origin of its own (a model's choice of parent is recorded as `chosen_by` on a data-origin edge instead) |
| B. Relationships that should exist | **Done** | `missing_lookup` suggestions are not scored against an answer key; the broken/dangling/archive kinds are tested only on a constructed fixture |
| C. Atlas for study | **Mostly done**: full screen, as submitted vs refined, tabbed inspector, search, origin colours, ghost edges | animated transition between views; search by column; path between two tables; exports; demo recordings do not include the new views |
| D. Ask accuracy | **Started**: D4 (compiler bug), tie-aware score | D1 entity naming/synonyms, D2 bridge-table penalty, D3 country canonicalisation, D5 fan-out and composite-key guards, D6 value binding, D7 determinism, D8 ablations, fixing the legacifier and a v2 benchmark, attributing Pagila |
| E. SQL Server / Express | **Mostly done**: ADO.NET strings, named instances, Windows auth, compat-level profiles, FK trust flags, run live | Connect page fields, `MS_Description`, least-privilege script, Docker Express test, HALCYON benchmark |
| F. Large databases | **Started**: candidate generation 5-45x faster, identical output, synthetic scale benchmark | probe budget and parallel probes, sampling from profiles, incremental re-excavation, clustered Atlas for 800+ tables |
| G. Re-measure and publish | **Not done** | re-run both suites with a model after phase D; re-record the demo; deploy |

## What the code does today (the starting point)

These facts were checked in the code, and several of them drive the plan.

1. **Declared foreign keys are read and then ignored.** `introspect()` fills
   `schema.foreign_keys` from the catalog (`excavate/introspect.py:132`), but nothing
   reads it. `joins.detect` only builds candidates from names and data. On a real
   database that *has* constraints, Stratum re-guesses joins the schema already states,
   and it can miss them. The benchmarks never exposed this, because the legacifier
   drops every FK.
2. **The "inferred" flag on Atlas edges is wrong.** `api/app.py:678` sets
   `inferred = detector != "procmine"`. But `joins.detect` re-emits mined joins under
   detector `"joins"` with `evidence.source = "shipped SQL"`, and `_dedupe` can keep
   either version. So a join read from a view can show as a guess.
3. **There are only two origins in practice.** The four real sources are: the catalog
   (declared), shipped SQL (`procmine`), data (inclusion test) and the LLM
   (`enrich` referents and `overloaded` targets). They are not modelled as one field.
4. **Rejected candidates are thrown away.** Anything under 85% inclusion is dropped at
   `joins.py:483` with no record. Those near-misses, and identifier-looking columns that
   found no parent, are exactly the "relationships that should exist" the user wants to
   see. Orphan values (child values with no parent) are counted as a ratio but never
   sampled or reported.
5. **Candidate generation is all-pairs.** `generate_candidates` compares every
   identifier column with every key candidate (`joins.py:203`), then runs one
   sequential probe per survivor. That is fine at 100 tables and does not scale to
   2,000. The inclusion probe's `SELECT DISTINCT ... LIMIT 400` can also scan a whole
   large table.
6. **SQL Server has never run live.** `connectors/mssql.py` uses pymssql with SQL
   authentication only. It has no named instances (`.\SQLEXPRESS`), no Windows
   authentication, and no ADO.NET connection strings.
7. **The raw catalog is not kept.** Only the semantic model is stored, so there is
   nothing to draw an "as submitted" view from.
8. **The Atlas draws one `THREE.Line` per edge.** Labels are capped at 40 and there is
   no fullscreen, search or filtering by origin.
9. **Excavation recall** (`gauntlet/reports/*_v1.recall.json`):
   - Chinook scores 1.0 on joins and keys.
   - Pagila joins: P 0.895, R 0.81. Composite keys: 1 of 3.
   - Enums decoded: 50% (Chinook), 67% (Pagila). Embedded formats recall on Pagila: 0.5.
10. **Ask failures** (Chinook, 18): retrieval 8, benchmark 4, excavation 3,
    generation 2, compilation 1. See `docs/ATTRIBUTION.md`.

## Phase order

| Phase | What | Why this position |
|---|---|---|
| A | Relationship provenance | Everything else (gaps, Atlas, SQL Server) reads it |
| B | Relationships that should exist | Built on A's rejected-candidate record |
| C | Atlas for study: fullscreen, submitted vs refined, inspector | Needs A and B in the API |
| D | Ask accuracy (metrics) | Independent of A to C; fixes the attributed failures |
| E | SQL Server and SQL Express | Live declared FKs exercise phase A for real |
| F | Large databases | Last, because it changes how the A and B probes are scheduled |
| G | Benchmarks, docs and demo | Re-measure everything once |

---

## Phase A: every relationship says where it came from

**Goal:** each relationship carries one `origin` and a plain-language reason. The UI
can then always say "declared in the schema", "written in your views and procedures",
"inferred from the data" or "guessed by the language model".

### A1. A single `origin` field

- Add `class Origin(str, Enum)` to `excavate/findings.py`, with values `declared`,
  `shipped_sql`, `data`, `llm` and `conditional`. Precedence, strongest first:
  declared > shipped_sql > data > llm.
- Every `Kind.JOIN` finding sets `evidence["origin"]`. `Relationship` in
  `semantic/model.py` gets:
  - `origin: str`
  - `corroborated_by: list[str]`, the other origins that found the same edge.
- `_dedupe` in `joins.py` merges same-edge findings instead of picking one. It keeps
  the strongest origin and records the others in `corroborated_by`. "Declared, and the
  data agrees (99.8% of values match)" is the most convincing thing the UI can say.
- Derive the Atlas `inferred` flag from `origin`, never from the detector name. That
  fixes item 2 above.
- **Back-compat:** a model YAML without `origin` derives it on load: `evidence.source`
  "shipped SQL" means shipped_sql; detector `enrich` means llm; `overloaded` means
  conditional; anything else means data.

### A2. Use declared foreign keys

- New detector module `excavate/declared.py`, a pure function of `Schema`. It turns
  each `ForeignKeyInfo` into a `Kind.JOIN` finding with `origin=declared`,
  `status=ACCEPTED` and `confidence=1.0`.
- Feed these into `joins.detect` the same way mined joins are fed in (`joins.py:452`).
  They are taken as given and are not re-derived.
- **Still verify them with one inclusion probe** when the budget allows. A declared FK
  with orphans is a real legacy finding. In SQL Server, a constraint marked
  `is_disabled` or `is_not_trusted` (created `WITH NOCHECK`) commonly has them.
  - Record the result as `evidence.verified_inclusion`.
  - If inclusion is below 1, also emit a `Kind.QUALITY` finding: "declared but
    violated".
- Connectors must return trusted/disabled flags for FKs: `ForeignKeyInfo.enforced:
  bool | None`.
  - mssql: `sys.foreign_keys.is_disabled` and `is_not_trusted`.
  - Postgres: `pg_constraint.convalidated`.
  - SQLite: `PRAGMA foreign_keys` state.
- Pipeline: add a `declared` stage after `introspect`, and emit a trace event (stage,
  ms, count).

### A3. Evidence that explains itself

- Add `excavate/explain_relationship.py` with
  `def reason(rel_or_finding) -> str`. It is deterministic, uses no LLM, and is unit
  tested per origin. Examples:
  - declared: "Declared as foreign key FK_Order_Customer. 99.8% of 400 sampled
    CustomerID values exist in Customer; 1 orphan."
  - data: "Not declared. 97.5% of 400 distinct ORD_CUST_NO values exist in
    CUSTOMER.CUST_ID, which is unique. The names match (0.82). The values are stored
    as text, so the join casts them."
  - shipped_sql: "Not declared. Written as a join in view vw_OrderSummary and in
    procedure usp_GetOrders."
  - llm: "Guessed by the language model from column names; not tested against data."
- Procmine must keep **which** routine each join came from, so `MinedJoin` gets
  `sources: list[str]`. The UI can then name the view.

### A4. Orphans and cardinality, measured

- Change `test_inclusion` so that, in the same round trip or one more probe, it
  returns `orphan_count` and up to 3 `orphan_samples`. Rule 2 of CLAUDE.md allows a
  handful of sample values. These are for the UI and are never sent to the LLM.
- Work out cardinality from the profiles, with no new queries:
  - many-to-one or one-to-one, from whether the child column is unique;
  - optional or required, from the child column's null rate.
- Store it in `Relationship.cardinality`, which already exists and is mostly unset.

### A5. The raw catalog snapshot

- Excavation writes `catalog.json` next to the model in the store. It contains tables,
  columns, types, PKs, declared FKs, indexes and row estimates, and **no data**.
- This snapshot is what phase C's "as submitted" view draws from.

### Tests and acceptance

- New fixture `engine/tests/fixtures/declared.sqlite` with:
  - 3 declared FKs, one of them violated (orphans);
  - 1 join that exists only in a view;
  - 1 join that is only inferable from data;
  - 1 LLM-only referent (mock the LLM).
- Assert the origin, `corroborated_by`, orphan count and reason text for each.
- Re-run `make eval DB=chinook SUITE=v1` and pagila. Join precision and recall must not
  drop. Every Atlas edge must have an origin.

---

## Phase B: relationships that should exist

**Goal:** a separate, clearly labelled section: "Stratum thinks these relationships are
missing from your database". Each item has its evidence and, where it applies, the DDL
that would add it.

**Hard rule:** gaps are suggestions. They are never used by the join planner or the
generator unless a reviewer accepts them, and an accepted gap becomes a normal
relationship with `origin` set to whatever evidence it had.

### B1. Gap kinds

Add `Kind.GAP` to findings, with `value.gap_type` set to one of the following.

| gap_type | Meaning | Source |
|---|---|---|
| `undeclared_fk` | Proven by data or shipped SQL, but there is no constraint | A's accepted edges minus declared ones |
| `dangling_reference` | An identifier-looking column (`*_ID`, `*_NO`, `*_CD`) that matched no parent above 50% | Candidates that `joins.detect` now records instead of dropping |
| `broken_relationship` | Best parent matches 50–85% of values: likely a real FK eroded by deletes | The same record, inclusion band |
| `points_outside_core` | Parent exists only in an archive, backup or dead table | Re-test dangling references against non-core tables, within budget |
| `missing_lookup` | A coded column (enum found, or low cardinality) with no lookup table | `enums` findings with no matching join |
| `isolated_table` | A core table with no relationships at all | Graph degree is 0 |
| `missing_unique` | An FK is blocked because the parent column is not unique | `parent_key_is_unique = false` on a high-inclusion candidate |

### B2. Implementation

- `joins.detect` returns `(findings, rejected)`. `rejected` holds candidates with their
  scores and why they failed: inclusion, name or uniqueness. The finding list is
  unchanged.
- New module `excavate/gaps.py` as a pure function of findings, rejected candidates,
  schema and table classes, returning `Kind.GAP` findings. Each one carries:
  - `evidence`;
  - `confidence`;
  - `reason` (from A3);
  - `fix`: the DDL text in the connection's dialect. For mssql, use `WITH NOCHECK`
    when there are orphans, and explain that choice.
  - `blockers`, for example "types differ (VARCHAR vs INT), so a constraint needs a
    computed column first", "parent not unique", or "212 orphan rows".
- DDL is only **shown**. Nothing ever executes it (rule 1). Generate it with sqlglot
  for the connection dialect and add a unit test that it parses.
- Model: `model.gaps: list[Gap]`, persisted and versioned like everything else.
  Reviewers can accept or reject gaps in the existing review queue.
- API: `GET /databases/{id}/gaps`. Gaps are also included in the atlas payload as
  `ghost_edges`.

### B3. A metric for it

The legacifier manifest knows which FKs were dropped, so the *scorer* (never the
engine) can measure the recall of `undeclared_fk` and `dangling_reference` against
the manifest.

To also test the declared path, add a legacifier option `keep_fk_fraction` (for
example 0.5). The legacy database then keeps some constraints, and the scorer checks
two things:

- every kept FK is labelled declared;
- the dropped ones come back as `undeclared_fk`.

Use new suite names (for example `chinook_partial`) so the v1 numbers stay
comparable.

### Acceptance

- The fixture tests cover each gap type.
- On chinook_partial: kept FKs are 100% labelled declared, and recall of dropped FKs
  as `undeclared_fk` is at least 0.9.

---

## Phase C: an Atlas for studying a database

**Goal:**

- Open any table and see what it is, what it references, what references it, why each
  link is believed, and what is missing.
- Switch between the database as submitted and as Stratum understands it.
- Go fullscreen.

Rule 6 applies: every animation here explains a change.

### C1. Fullscreen

- Add a button in the Atlas toolbar, plus the `F` key. `Esc` exits.
- Use `element.requestFullscreen()` on the Atlas container, not the canvas, so the
  inspector and legend come along.
- iPhone Safari has no element Fullscreen API. There, fall back to a
  `fixed inset-0 z-50` overlay with the same layout.
- In fullscreen the inspector becomes a right-hand drawer over the canvas, and the
  canvas resizes (r3f handles it once the container size changes).
- Restore focus to the button on exit.
- Files: `web/app/db/[id]/atlas/page.tsx`, plus a new `web/components/atlas/Fullscreen.tsx`.

### C2. As submitted vs refined

- A segmented control in the toolbar: **As submitted** | **Refined**. A third
  position, **What changed**, highlights only the differences.
- Data comes from `GET /databases/{id}/atlas?view=submitted|refined`.
  - **As submitted** is built from `catalog.json` (A5):
    - physical table names;
    - only declared FKs;
    - every table on one level, with no sinking of dead tables;
    - no decoded names.
  - **Refined** is what exists today plus origins and gaps.
- The transition is the one animation that explains the whole product. It runs in
  this order:
  1. Leftover tables sink below the grid.
  2. Edges appear in order of origin: shipped SQL, then data, then LLM.
  3. Labels cross-fade from `TRK_MSTR` to `track`.
  4. Ghost edges for gaps fade in last.
  - Under reduced motion the switch is instant.
- **What changed** is a summary strip, with each count clickable as a filter:
  - "+14 relationships (9 from data, 5 from views)";
  - "117 tables set aside";
  - "212 columns renamed";
  - "8 soft-delete rules";
  - "11 coded columns decoded";
  - "6 relationships that should exist".

### C3. Edges by origin, with legend toggles

| Origin | Line | Colour token |
|---|---|---|
| declared | solid, full weight | `--edge-declared` |
| shipped_sql | solid, thinner | `--edge-code` |
| data | dashed, opacity follows confidence | `--edge-data` |
| llm | dotted | `--edge-llm` |
| conditional | dashed with a midpoint marker | existing |
| gap (should exist) | faint dashed ghost, off by default | `--edge-gap` |

- Never rely on colour alone: dash pattern and legend text carry the meaning too.
- Each legend row is a toggle with a count.
- Show direction (child to parent) with a small arrowhead at the parent end. On a
  selected node, a slow pulse runs along its edges toward the parent. It explains
  direction, and it is off under reduced motion.
- Rendering: one `LineSegments` buffer per origin (dashes through
  `LineDashedMaterial` with `computeLineDistances`) instead of one `Line` per edge.
  This is required for phase F's scale target.

### C4. The inspector (click a sphere)

A right-hand panel with tabs. Keep the current contents and extend them.

- **Overview:**
  - physical and logical name, and kind (live, archive and so on);
  - **why it was classified that way** (the artifacts finding's evidence);
  - rows, domain, and how many items are still to review.
- **Columns:**
  - a table of physical name, logical name, type, PK/FK marker, decoded meaning, and
    status (hypothesis/accepted);
  - for enum columns, the decoded values;
  - for drifted columns, the change point;
  - each renamed column has a "why" that expands to the naming evidence.
- **References** (outgoing) and **Referenced by** (incoming), each relationship
  showing:
  - an origin badge, plus corroboration ("declared, and the data agrees");
  - the join in physical and logical form;
  - cardinality ("many orders to one customer, required");
  - the A3 reason sentence;
  - numbers: inclusion, values tested, orphans with up to 3 samples, name similarity,
    and whether the parent is unique;
  - **Go to** (fly to the other table);
  - **Review** (accept or reject, through the existing review API).
- **Rules:** soft-delete filters, discriminators, drift, units and quality issues.
- **Missing:** this table's gaps from phase B, with the DDL and a **Copy DDL** button.

### C5. Study tools

- **Focus mode:** selecting a node dims everything outside 1 hop (a toggle extends this
  to 2 hops). The camera eases to frame the neighbourhood. A breadcrumb lists visited
  tables.
- **Search** (`/` or Ctrl+K): matches table, column or logical name, and flies to the
  node.
- **Path between two tables:** pick A and B. The Atlas highlights the path the join
  planner (`ask/joinplan.py`) would use and shows the SQL join chain. This needs a new
  endpoint, `GET /databases/{id}/joinpath?from=&to=`, which is deterministic and uses
  no LLM.
- **Export:**
  - relationships as CSV or JSON, with origin and evidence;
  - the selected neighbourhood as a Mermaid ER diagram;
  - all `undeclared_fk` DDL as one `.sql` script;
  - a PNG of the view.
- **Table view parity:** "Show as a table" lists relationships with filterable origin,
  confidence and cardinality columns. This is the accessible equivalent of the map, so
  every fact in the 3D view must also be reachable here and by keyboard.

### C6. The paste-your-schema page

`web/lib/schema.ts` already separates declared from guessed. Use the same inspector,
legend and fullscreen there, and add a "Should exist (guess from names only)" layer.
Keep the honest limits section, because a schema file has no data and so has no
inclusion evidence.

### Acceptance

Check in a headless browser:

- fullscreen enters and exits, and the inspector stays usable in fullscreen;
- switching views animates, or is instant under reduced motion;
- every edge type renders with its legend;
- the inspector shows the evidence for each origin;
- search and path work;
- no console errors;
- usable at phone width.

Also re-record the static demo (`web/scripts/record_demo.py`) so the Cloudflare site
shows the new atlas payload.

---

## Phase D: ask accuracy (the metrics)

**Order:** expected gain on the attributed Chinook failures first. Rule 3 still holds:
fix the general mechanism, and never write model content for a benchmark database by
hand.

| Item | Failures it targets | Change |
|---|---|---|
| D1. Entity naming and synonyms | 5 (c-011, c-023, c-030, c-038, c-040) | See below |
| D2. Bridge-table penalty in linking | Same 5, plus c-045 noise | See below |
| D3. Value canonicalisation | 3 (c-005, c-013, c-036) | See below |
| D4. Compiler LEFT JOIN pushdown bug | 1 (c-021) | See below |
| D5. Fan-out and composite-key guards | 2 (c-026, c-028) | See below |
| D6. Value binding misses | 2 (c-010, c-019) | See below |
| D7. Determinism | Run-to-run noise | See below |
| D8. Wire the ablations | Credibility | See below |

**D1. Entity naming and synonyms.** Name entities from evidence ranked by strength:

1. views that alias the table (`FROM TRK_MSTR t ... AS track_name`);
2. column-name stems (`TRK_*`, `TRACK_ID`);
3. the LLM.

Store `synonyms` on the entity (for example the expanded table name, view aliases and
the LLM's alternatives), and let `ask/linking.py` match on them.

**D2. Bridge-table penalty in linking.** Detect bridges deterministically: two or more
outgoing FKs, few non-key columns, and a composite PK made of those FKs. In linking, a
bridge only ranks when both ends are also linked. It never roots the join plan by
itself.

**D3. Value canonicalisation.** Cluster text values with these steps:

1. normalise (case, accents, punctuation, spacing);
2. use built-in abbreviation tables (ISO 3166 country names and codes, US states);
3. as a fallback, fuzzy clustering above a high threshold.

Emit the clusters as the existing canonical CASE (`compile_model._canonical_case_over`)
and bind query values to the canonical form.

**D4. Compiler LEFT JOIN pushdown bug.** In `_collect_pushdown` (`ask/compiler.py:288`),
never push a predicate on the nullable side of a LEFT/RIGHT/FULL join into that side's
derived table. Above all, never push `IS NULL`. Add a regression test built from c-021.

**D5. Fan-out and composite-key guards.** `joinplan` already computes `fan_out_risk`.
Make validation enforce it:

- A SUM/AVG of a header-grain measure, when the query also joins to a line-grain
  entity, triggers a repair with a precise message, or a deterministic rewrite to
  pre-aggregate.
- GROUP BY on part of a known composite key triggers a repair message naming the
  missing column.

Both checks are deterministic. Each one costs one repair call only when it fires.

**D6. Value binding misses.** Trace c-010 and c-019 through `ask/values.py`, and add a
test for each cause found.

**D7. Determinism.**

- Enrichment uses temperature 0 and a cache keyed on prompt hash.
- Answers served from the semantic cache must still pass the cost guard.

**D8. Wire the ablations.** `semantic_layer`, `single_candidate` and `no_repair`
currently do nothing, and `value_index` toggles the cache. Make each one change what
its name says, and add a test asserting that each ablation changes the trace.

### Benchmark hygiene

Do this in the same phase, but publish it separately.

- Fix `_null_marker` in `gauntlet/legacify/chinook.py` so it never destroys a real
  value. Extend the reversibility check so it would have caught the bug.
- Rebuild the data as **v2**. Keep the v1 reports and numbers, since they are what the
  README and SOP cite. Report v2 beside v1 and never overwrite it silently.
- In the scorer, report a second, **tie-aware** score: ties under ORDER BY are treated
  as a set, and extra columns are allowed when every gold column is present. It
  appears next to the strict score, never instead of it.
- Run the same attribution on Pagila (`gauntlet/attribution/`).

### Targets

These are targets, not promises.

- Chinook v1, strict: up from 28 of 46. Fixing D1 to D5 addresses up to 13 of the 18
  failures, so 36 or more is a reasonable goal.
- Pagila, shared questions: up from 15 of 26.
- Default mode still under about 6K tokens per question (rule 4). Check
  `tokens` in the trace for every question of the run.

---

## Phase E: SQL Server, SSMS and SQL Express

**Goal:**

- Someone who uses SSMS can connect to their local SQL Express instance the way SSMS
  does, by server name, instance and Windows or SQL login.
- Excavation and asking both work.
- Every generated query passes the connection's profile.

### E1. Two drivers

- Keep pymssql for SQL logins against a host and port; it works in Docker and on
  Linux.
- Add **pyodbc** for Windows authentication and named instances. Add it to the
  `mssql` extra.
  - Driver detection order: "ODBC Driver 18 for SQL Server", then 17. This machine has
    only the old "SQL Server" driver. If neither modern driver is installed, the error
    says exactly which one to install.
  - Express installs use a self-signed certificate. Default to `Encrypt=yes;
    TrustServerCertificate=yes` for `localhost`/`.` only, and say so in the
    connect UI.
- `MssqlConnector` picks the driver from the URL or connection string; the rest of the
  connector (catalog queries, `tsql` profile) is shared.

### E2. Connection input that matches SSMS

Accept all of these:

- `mssql://user:pass@host:1433/db`
- `mssql://./SQLEXPRESS/db?auth=windows`
- an ADO.NET string pasted from SSMS or Visual Studio, for example
  `Server=.\SQLEXPRESS;Database=Northwind;Trusted_Connection=True;`

Parse ADO.NET strings in `connectors/base.parse_url` and add tests.

The `web/app/connect` form gets these fields:

- Server, with an instance placeholder `.\SQLEXPRESS`;
- Authentication: Windows or SQL Server;
- Database, as a dropdown filled after a test connect by listing `sys.databases` the
  login can open;
- a **Paste connection string** box.

Named instances need SQL Browser (UDP 1434) or an explicit port. When resolution fails,
the error must say that.

Credentials stay in the engine process and never reach the LLM (rule 2). Windows
authentication means there are none to leak.

### E3. Catalog coverage

- Multiple schemas (`dbo`, `Sales` and so on), with names qualified everywhere.
- FK trusted and disabled flags (A2).
- `sys.sql_modules` for views, procedures and functions (procmine); synonyms.
- **`MS_Description` extended properties.** These are free, human-written column and
  table descriptions in many SSMS-maintained databases. Feed them to naming (D1) as
  the strongest naming evidence, above views.
- Computed columns, and user-defined types resolved to their base type.
- Fast row counts from `sys.dm_db_partition_stats`, falling back to
  `sys.partitions`.

### E4. Profile selection

`tsql2008` stays the default when the version is unknown or below 11. Add `tsql2012`
(adds OFFSET/FETCH, IIF, CONCAT, TRY_CAST, FORMAT, LAG/LEAD) and `tsql2017` (adds
STRING_AGG). Choose from `SERVERPROPERTY('ProductMajorVersion')` and
`compatibility_level` of the database, whichever is lower, because a 2022 server can
host a database at level 100 (2008). Show the chosen profile and the reason in the UI.
The linter tests get one case per added feature.

### E5. Read-only, the SQL Server way

- Each probe runs in a transaction that is rolled back.
- Set `SET LOCK_TIMEOUT`, and use a statement timeout through the driver's query
  timeout.
- Never use `NOLOCK` hints. Use the default isolation level; if the database has it
  on, use `READ_COMMITTED_SNAPSHOT`.
- Document a least-privilege login (`db_datareader` + `VIEW DEFINITION`) in the README
  as a T-SQL script. Excavation must work with exactly those rights.

### E6. Live tests

- Docker image `mcr.microsoft.com/mssql/server:2022-latest` with `MSSQL_PID=Express`.
  This is the real Express edition, with its limits, and it needs no install on
  Windows. Update `infra/docker-compose.yml`.
- Restore `gauntlet/sources/AdventureWorks2019.bak`, which is already downloaded. It
  has 71 tables, about 90 declared FKs, views, procs and `MS_Description` on most
  columns. That makes it the ideal test for phases A, B and E3 together.
- Mark the tests `@pytest.mark.mssql`. They are skipped when no server is reachable,
  so `make test` stays green without Docker.
- Then merge branch `halcyon-wip` (the SQL Server legacifier) and run the HALCYON
  benchmark.
- Optional, manual: connect to a native SQL Express on Windows through `.\SQLEXPRESS`
  and Windows authentication, and record the result in `docs/PROGRESS.md`.

### Acceptance

On AdventureWorks under Express:

- 100% of declared FKs are labelled declared;
- every `MS_Description` reaches the model;
- 10 asked questions all pass the chosen profile's linter;
- a connection made through `.\SQLEXPRESS` with Windows authentication works on the
  Windows machine.

---

## Phase F: large legacy databases

**Target:** a schema of 2,000 tables and 40,000 columns.

- It excavates in under 20 minutes on a laptop against local Postgres or SQL Server.
- Peak memory stays under 2 GB.
- The Atlas stays interactive (30 fps or more).
- Every limit that was hit is reported, not hidden.

### F1. Join candidates by blocking, not all pairs

- Index parent key candidates by:
  - expanded name tokens (`naming.expand`);
  - the table-name stem;
  - a type bucket.
- A child column is compared only with parents that share a token or stem, plus
  same-type hierarchy cases. This cuts the work from O(columns × keys) to roughly
  O(columns × k).
- Test that the candidate sets on Chinook and Pagila are unchanged.

### F2. A probe budget and a schedule

- Order candidates by origin, then name score.
- Run probes in a small pool (default 4 connections; read-only executor per
  connection).
- Stop at `probe_budget` (count) or `join_budget_s` (time).
- Untested candidates go into the trace and the UI as "not tested (budget)", never
  silently dropped. They can still surface as low-confidence gaps.

### F3. Sampling that does not scan

- Inclusion samples come from what profiling already collected (distinct sample values
  per column), not from a fresh `SELECT DISTINCT ... LIMIT` over the full table.
- Where a fresh sample is needed, use the dialect's sampling clause: Postgres
  `TABLESAMPLE SYSTEM`, SQL Server `TABLESAMPLE (n ROWS)`, SQLite a rowid window.
- Add sampling to the capability profiles so the linter knows it.

### F4. Profiling at scale

- Use row estimates from the catalog (no `COUNT(*)` on large tables).
- Profile per table with a sampled scan.
- Make the time budget scale with table count but cap it; the user can raise it.
- Report skipped tables by name.
- Re-excavation hashes each table's DDL and skips unchanged tables (incremental).

### F5. Atlas at scale

- Server-side layout per domain: networkx `spring_layout` with a fixed seed, cached in
  the store, so it is stable across reloads.
- Above about 800 nodes, the default is **domain clusters**: one sphere per domain,
  sized by table count. Clicking a cluster expands it. The transition explains
  containment.
- Labels by level of detail: only the selection, its neighbours, and the top N by
  degree that are in view.
- The edge buffers come from C3. The payload is gzip and compact arrays, under about
  2 MB for 2,000 tables.

### F6. A benchmark for scale

- Write `gauntlet/scale.py`, which generates a large schema by replicating a benchmark
  database's tables into N prefixed copies with noise:
  - renamed columns;
  - extra dead tables;
  - a few cross-copy references.
- Measure excavation wall time per stage, probes run, memory, and join precision and
  recall. That recall should hold at the small-database level.
- Write the results to `gauntlet/reports/scale_*.json`.

### Acceptance

- The scale benchmark meets the target.
- The trace shows the budget use.
- The Atlas at 2,000 nodes holds 30 fps or more in headless Chrome
  (`performance.now` frame sampling).

---

## Phase G: re-measure and publish

- Run `make eval` for chinook and pagila (v1 strict, v1 tie-aware, v2), chinook_partial
  (phase B), AdventureWorks (phase E) and scale (phase F). Paste the tables into
  `docs/PROGRESS.md`.
- Update `docs/ATTRIBUTION.md` with the post-fix attribution.
- Update the README with only what was run, as before. New numbers go beside the old
  ones, with the change explained.
- Re-record the demo and deploy. Pushing redeploys the public site, so push only when
  asked.

## Risks

- **Verifying declared FKs costs probes.** On a big database, verify within budget and
  trust the rest at 1.0, labelled "declared, not verified".
- **Gap noise.** `dangling_reference` on a messy schema can produce hundreds of items.
  Rank them by confidence, group them by table, show the top 50 by default, and count
  the rest.
- **The LLM origin must look weaker.** It is the easiest to over-trust, so its badge
  and line style are the faintest, and it is never auto-accepted.
- **Benchmark drift.** Fixing the legacifier and adding a tie-aware score change the
  numbers. v1 strict stays the headline until both are reported side by side.
- **pyodbc on this machine** needs ODBC Driver 18 installed (a user action). Docker
  Express with pymssql does not, so test that first.
- **Token budget.** D5 repairs and E3 descriptions add tokens. Check rule 4 in the eval
  traces, and truncate descriptions to a fixed size per column.

## Working on Sonnet

- **One phase per session.** Start each session by reading this file, `CLAUDE.md` and
  the phase section. Finish it with the acceptance checks and `make test`.
- **Phase A first.** It is the smallest change with the widest effect, and B, C and E
  read its `origin` field.
- **Engine detectors stay pure functions.** Each new detector gets a fixture test
  (CLAUDE.md conventions), and every new stage emits a trace event.
