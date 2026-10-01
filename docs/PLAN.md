# Stratum — Build Plan

> Natural language → correct SQL → correct records, on databases nobody documented.

This is the plan Claude Code executes. Work top to bottom. Each phase ends with acceptance criteria; do not start the next phase until they pass.

---

## 0. Decisions already made

| Topic | Decision | Why |
|---|---|---|
| Goal | Ship fast. MVP in ~3–4 weeks of focused work (phases below). | User priority. |
| Dialects | SQL Server (legacy T-SQL profile `tsql2008`), Postgres 16, SQLite. | User choice. |
| Runtime LLM | No paid key. Groq free tier as primary, Gemini free tier / OpenRouter free models as fallback, optional local Ollama. One OpenAI-compatible adapter. | User has no API key. |
| Claude without an API key | **MCP mode**: the engine is exposed as an MCP server so Claude Desktop / Claude Code can drive it using the user's Claude plan. The web app itself uses Groq. | A website can't call Claude through a subscription; MCP is the sanctioned way for Claude to use local tools. |
| Human in the loop | Confidence-gated: auto-run when confident, clarify/approve when not. | User choice. |
| Ground truth | Build "The Gauntlet": clean, well-known databases → deterministically **legacified** copies. Gold answers are computed on the clean copy, so correctness is known without hand-writing SQL for the messy one. | Makes cross-matching exact and cheap. |
| Working name | **Stratum** (legacy data as sediment layers to be excavated). Rename freely. | Gives the design a concept. |

---

## 1. What we're copying from the best systems (and what we do differently)

| Prior art | Idea we take | Where it lands |
|---|---|---|
| Uber QueryGPT | Narrow the search space (workspaces → intent), show proposed tables for the user to ACK/edit, prune columns before generation. | §4.4 steps 3–4; table chips in the UI. |
| LinkedIn SQL Bot | Knowledge graph from metadata + query logs + example queries; retrieve wide then LLM-rank narrow; a fixer agent that catches hallucinated tables/columns. Example queries were the single biggest accuracy lever. | Semantic model + verified library; repair loop. |
| Snowflake Cortex Analyst | Semantic model with **logical** names over physical columns; Verified Query Repository written against logical names; suggest new verified queries from usage. | The whole logical-layer design (§4.3). |
| Databricks Genie | Trusted assets marked "verified" in answers; benchmark questions with gold SQL to measure a space before users trust it. | "Verified" badge; the Lab. |
| CHASE-SQL / Agentar-style systems (top of BIRD) | Multiple diverse candidates + a selector. | §4.4 step 7–9 (budget-limited version). |
| BIRD-Interact, AmbiSQL | Ambiguity taxonomy (intent-level vs implementation-level vs missing knowledge); ask a targeted clarification instead of guessing. | Clarification gate; simulated user in eval. |
| SNAILS | Cryptic identifiers measurably hurt LLMs; naturalizing names helps. | The LLM never sees `CFNM1`; it sees `customer.first_name`. |

**Our distinctive bet:** the LLM writes SQL against a clean, *virtual* schema. A deterministic compiler (sqlglot-based) then rewrites it into the ugly physical reality: renames, casts on mismatched join keys, soft-delete filters, discriminator filters for god tables, XML/JSON extraction, drift coalescing, and dialect downgrades. Legacy mess is handled once, in reviewed code and YAML — not re-guessed by the LLM on every question.

---

## 2. Architecture

```
                 ┌───────────────────────────── web (Next.js) ─────────────────────────────┐
                 │ Landing · Connect · Excavate · Atlas (3D) · Workbench · Ask · Lab       │
                 └───────────────▲──────────────────────────────▲──────────────────────────┘
                                 │ REST                          │ SSE (trace events)
┌────────────────────────────────┴──────────────── engine (FastAPI) ─────────────────────────────────┐
│  connectors/      capability profiles, read-only executor, cost guard                              │
│  excavate/        introspect → profile → detectors → LLM enrichment → semantic model (YAML)        │
│  semantic/        model store, versioning, HITL review queue, verified query library              │
│  ask/             route → understand → clarify? → link → plan joins → generate → compile →         │
│                   validate → execute → repair → select → confidence → explain                      │
│  llm/             OpenAI-compatible adapter: Groq → Gemini → OpenRouter → Ollama; token budgeter   │
│  mcp/             MCP server exposing engine tools (Claude Desktop / Claude Code as the brain)     │
│  store/           app DB (SQLite): connections (encrypted), models, runs, feedback, cache          │
└──────────────┬──────────────────────────────┬──────────────────────────────┬──────────────────────┘
               │                              │                              │
         SQL Server 2022                 Postgres 16                      SQLite files
     (HALCYON, AW_CLEAN)          (pagila_legacy, pagila_clean)   (chinook_legacy, chinook_clean, BIRD)
```

### Repo layout

```
stratum/
  CLAUDE.md
  Makefile
  infra/docker-compose.yml
  engine/
    pyproject.toml
    stratum/
      api/            # FastAPI routers
      connectors/     # mssql.py, postgres.py, sqlite.py, profiles.py, executor.py, costguard.py
      excavate/       # introspect.py, profile.py, artifacts.py, joins.py, keys.py, enums.py,
                      # softdelete.py, formats.py, overloaded.py, drift.py, types.py, quality.py,
                      # procmine.py, enrich.py, compile_model.py
      semantic/       # model.py (pydantic), store.py, review.py, verified.py
      ask/            # router.py, understand.py, ambiguity.py, linking.py, values.py,
                      # joinplan.py, generate.py, compiler.py, dialect.py, validate.py,
                      # repair.py, select.py, confidence.py, explain.py, fallback.py
      llm/            # client.py, budget.py, prompts/
      mcp/server.py
      store/
    tests/
  gauntlet/
    sources/          # download scripts for AdventureWorks, Pagila, Chinook, BIRD mini-dev
    legacify/         # halcyon.py, pagila.py, chinook.py, common.py
    suites/           # halcyon_v1.yaml, ...
    scorer.py
    reports/
  web/
  docs/PLAN.md, docs/PROGRESS.md
```

---

## 3. The Gauntlet (build this FIRST — everything is measured against it)

### 3.1 Databases

| Name | Engine | Clean source | Legacy treatment |
|---|---|---|---|
| **HALCYON** (flagship) | SQL Server, profile `tsql2008` | AdventureWorks OLTP (Microsoft sample) restored as `AW_CLEAN` | Full treatment: all 16 anti-patterns. |
| **pagila_legacy** | Postgres 16 | Pagila (Postgres port of Sakila) | Moderate: cryptic names, missing FKs, JSON blobs, drift, soft deletes, string mess. Also run under an artificial `legacy_strict` profile (no CTEs, no window functions) to exercise the rewriter. |
| **chinook_legacy** | SQLite | Chinook | Light: text dates in 3 formats, magic numbers, dead tables. |
| **BIRD mini-dev** (external) | SQLite / Postgres | as published | Untouched. A sanity check against a public benchmark; confirm the current release on the BIRD GitHub before downloading. |

### 3.2 The legacifier

`gauntlet/legacify/halcyon.py` reads `AW_CLEAN` and writes `HALCYON` deterministically (fixed seed). It emits `manifest.json` describing every transformation (the answer key — engine code never reads it).

Transformations, by anti-pattern (HALCYON):

| # | Anti-pattern | Concrete injection |
|---|---|---|
| 1 | Cryptic naming | `Sales.Customer` → `dbo.CUST_MSTR`; `SalesOrderHeader` → `SO_HDR`; `OrderDate` → `ORD_DT`; `FirstName` → `CFNM1`. Mix styles: vowel-dropped (`CSTMR`), Hungarian (`tblPrd`, `strNm`), numbered (`ADDR1..3`), 6–8 char mainframe style. |
| 2 | Missing FKs | Drop all FK constraints. Keep PKs on only ~half the tables. |
| 3 | No documentation | No column comments; add a few **stale, wrong** extended properties (misleading docs are realistic). |
| 4 | Table sprawl | Generate ~300 extra tables: yearly archives (`SO_HDR_2011`), staging (`STG_*`), report snapshots (`RPT_*`), `tmp_*`, vendor add-on schemas. |
| 5 | Dead / redundant artifacts | `CUST_MSTR_BAK_20140312` (stale copy, slightly different), `CUST_MSTR_OLD`, empty tables, views referencing dropped columns (broken). |
| 6 | Schema drift | `PHONE` deprecated in 2015 → `PHN_NBR` added; older rows only have `PHONE`. Money stored as dollars before a cutover date and **cents** after (unit drift). Email moved from `CUST_MSTR` to `CUST_CNTCT` mid-history. |
| 7 | God table | `TRANS_MSTR` (~90 columns, mostly NULL) holding orders, returns and payments, discriminated by `TRANS_TYP_CD` ('O','R','P'). |
| 8 | Embedded formats | `ADDR_XML` (XML address), `PREFS` (JSON string in NVARCHAR), `ATTRS` as `color=Red|size=M`. |
| 9 | Overloaded fields | `REF_ID` = customer id when `REF_TYP='C'`, vendor id when `'V'`. `NOTES` carries discount codes prefixed `DSC:`. |
| 10 | Implicit composite keys | Order numbers restart per store → true key is `(STORE_CD, ORD_NO)`; order lines keyed by `(STORE_CD, ORD_NO, LN_SEQ)` with no declared PK. |
| 11 | Magic numbers | `ORD_STS` 1–6 plus 9 = cancelled; flag columns mixing `'Y'/'N'/'1'/'0'/NULL`. |
| 12 | Implicit soft deletes | Three styles in different tables: `DEL_FLG='Y'`, `STS=9`, `DLT_DT IS NOT NULL`, plus products renamed with a `ZZ_` prefix when discontinued. Soft-deleted rows are *extra* rows that don't exist in `AW_CLEAN`. |
| 13 | Inconsistent types | `CUST_ID INT` in `CUST_MSTR`, `CUST_NO VARCHAR(10)` zero-padded (`'0000012345'`) in `SO_HDR`; dates as `DATETIME`, `CHAR(8) 'YYYYMMDD'`, and `VARCHAR 'DD/MM/YYYY'`; money as `FLOAT`, `DECIMAL`, and cents `INT`. |
| 14 | Bad string quality | CHAR padding, mixed-case cities, null markers `'N/A'`, `''`, `'-'`, `'UNKNOWN'`; country as `'US'`/`'USA'`/`'United States'`. |
| 15 | Dialect friction | Enforced by profile `tsql2008`: no OFFSET/FETCH, IIF, CONCAT, TRY_CAST/TRY_CONVERT, FORMAT, LAG/LEAD, running aggregates with ORDER BY in OVER, JSON functions, STRING_AGG. (The container runs SQL Server 2022; the profile linter is what enforces 2008-era SQL. Also set DB compatibility level 100.) |
| 16 | Performance traps | `AUDIT_LOG` with ~8M synthetic rows and no index on its date column (the clean copy gets an identical table *with* an index). Questions over it must trip the cost guard. |

### 3.3 Question suites

`gauntlet/suites/halcyon_v1.yaml`, ~60 questions to start:

```yaml
- id: h-017
  question: "How many active customers placed an order in March 2013?"
  tags: [soft_delete, inconsistent_types, cryptic_names, missing_fk]
  difficulty: medium
  gold_sql: |            # written against AW_CLEAN
    SELECT COUNT(DISTINCT c.CustomerID) ...
  gold_sql_alt: |        # independent second formulation; results must match gold_sql
    ...
  order_sensitive: false
  ambiguity: null
- id: h-041
  question: "Who are our top customers?"
  tags: [ambiguous]
  ambiguity:
    type: intent            # intent | implementation | knowledge
    clarification: "Top 10 by total order value, all time"
  gold_sql: ...
```

Rules:
- Every question gets **two independent gold formulations**; build fails if their results differ (public benchmarks have been shown to carry annotation errors — catch ours).
- Prefer answers that are IDs, counts, sums, dates. When strings are returned, the scorer trims and casefolds.
- Tag each question with the anti-patterns it exercises so accuracy can be broken down per anti-pattern.
- ~15% of questions are ambiguous with an annotated clarification. The eval's **simulated user** answers only with that clarification and refuses anything else (BIRD-Interact style).
- Claude Code drafts questions + gold SQL; the human spot-checks at least 20%.

### 3.4 Scoring (`gauntlet/scorer.py`)

- Execution accuracy: run gold on the clean DB, the system's SQL on the legacy DB, compare result **multisets**.
- Column matching Spider 2.0-style: each gold column must match some system column by value vector; extra system columns are allowed.
- Numbers rounded to 2 dp; dates to ISO; strings trimmed + casefolded; nothing else normalized.
- Order compared only when `order_sensitive: true`.

Report per run: overall EX, EX per anti-pattern tag, EX per difficulty, clarification precision/recall (asked when needed / didn't ask when not), abstention rate and abstention correctness, mean tokens and latency per question, verified-hit rate.

**Knowledge-recall score** (does excavation find the truth?), computed by comparing the engine's semantic model to `manifest.json` — the only code allowed to read the manifest: join precision/recall, composite-key recovery, enum decode accuracy, soft-delete rule recall, dead-table classification F1, drift detection recall.

Baselines, reported in every eval: (a) naive "full schema dump + one LLM call", (b) full pipeline, (c) ablations: no semantic layer / no join inference / no value index / single candidate.

---

## 4. The Engine

### 4.1 Connectors and capability profiles

- Connection input: a URL (`postgresql://…`, `mssql://…`, `sqlite:///…`) or a form; SQLite also via file upload. Parse the URL into form fields live so users see what was understood.
- Drivers: `psycopg` 3, `pymssql` (simplest install; `pyodbc` + ODBC Driver 18 as an alternative), `sqlite3`.
- On connect: detect server version, pick a capability profile (`tsql2008`, `tsql2016`, `pg16`, `sqlite3`, `legacy_strict`), check the login's privileges and **warn** if it can write. Execution stays read-only regardless.
- Friendly error mapping (e.g. "Login failed for user" → "SQL Server rejected the username/password — is SQL auth enabled?").
- Credentials encrypted at rest (Fernet key from env).

Capability profile = feature flags (`cte`, `window_basic`, `window_frame`, `lag_lead`, `offset_fetch`, `limit`, `top`, `json_fns`, `xml_value`, `try_cast`, `concat_fn`, `string_agg`, `iif`, `ilike`, `regex`) plus a sqlglot write dialect. `dialect.py` lints and rewrites: `LIMIT`→`TOP`, CTE→derived table when `cte=false`, `TRY_CAST` → safe `CASE` patterns, `CONCAT` → `+` with `ISNULL`, window functions → correlated subqueries where possible, otherwise escalate to the Python fallback (§4.5, R3).

### 4.2 Excavation (runs once per database, streams progress)

Each step writes findings `{kind, target, value, evidence, confidence}`.

1. **Introspect**: tables, columns, types, nullability, PKs, unique constraints, indexes, row counts (catalog estimates, not `COUNT(*)`), views and **stored procedure / view source text**. Sources: `sys.*` for SQL Server, `pg_catalog` / `information_schema` for Postgres, `sqlite_master` + `PRAGMA` for SQLite.
2. **Profile** (sampled, time-boxed per table): null %, distinct count, top-K values, min/max, length stats, and **pattern signatures** — regex classes per string column (`\d{8}`, `\d{2}/\d{2}/\d{4}`, zero-padded ints, emails, phone shapes, JSON-looking, XML-looking, `k=v|k=v`).
3. **Detectors**, one module per anti-pattern:

| Detector | Method | Output used at query time |
|---|---|---|
| `artifacts.py` (sprawl, dead) | Name patterns (`_bak`, `_old`, `tmp_`, date suffixes), zero/near-zero rows, schema near-duplicate of another table (column Jaccard + sampled row overlap), broken views. | Tables classified `core / archive / staging / backup / dead`; non-core excluded from retrieval unless asked for. |
| `procmine.py` | Parse view and stored-procedure SQL with sqlglot; harvest join predicates, filters, CASE mappings (legacy code often documents enums and joins nobody else did). | High-confidence join edges, enum labels, soft-delete filters. |
| `joins.py` (missing FKs) | Candidate pairs by expanded-name similarity + type compatibility **including cast-compatibility** (INT vs zero-padded VARCHAR) + **inclusion test** on samples (child values ⊆ parent values) + parent uniqueness. | Join graph with edge confidence and the exact join expression (with casts). |
| `keys.py` (implicit composite keys) | Search minimal unique column combos on samples (up to 3 columns), verify with a `GROUP BY … HAVING COUNT(*)>1` probe. | Declared logical keys; composite join edges. |
| `enums.py` (magic numbers) | Low-cardinality numeric/char columns; decode hypotheses from procmine CASEs, value co-occurrence with dates (e.g. status 9 rows have `CANCEL_DT`), and LLM guess with evidence. | Enum maps flagged `hypothesis` until accepted. |
| `softdelete.py` | Flag-like names (`DEL`, `DLT`, `ACTV`, `STS`), tombstone dates, `ZZ_` prefixes, rows never referenced by children after a flag date. | Default filters per entity. |
| `formats.py` (embedded XML/JSON/k=v) | Pattern signatures → parse samples → infer paths. | Extraction expressions per dialect; if the dialect can't (JSON on 2008), mark field `python_extract`. |
| `overloaded.py` | Column whose value pattern depends on a sibling discriminator (`REF_ID` pattern differs by `REF_TYP`). | Conditional relationships. |
| `godtable.py` | Very wide table + a low-cardinality code column that partitions null patterns. | Multiple logical entities over one table with discriminator filters. |
| `drift.py` | Column pairs with complementary null patterns over a time column (`PHONE` vs `PHN_NBR`); value-scale shifts over time (dollars → cents). | `COALESCE` expressions, unit-normalizing `CASE` by date. |
| `types.py` + `quality.py` | Same-meaning columns with different types; date strings in several formats; null markers; case/whitespace variants; country/code synonyms (value clustering with rapidfuzz). | Normalization expressions; value index canonical forms. |
| `perf.py` | Row counts vs indexes on likely filter columns (dates, statuses). | Cost-guard hints; "suggest index" notes. |

4. **LLM enrichment** (batched, token-frugal): one call per ~15 tables with a compact "table card" (names, types, 3 sample values per column, detector findings). Output: logical names, one-line descriptions, synonyms, enum-label guesses. Cache by table-card hash so re-runs are free.
5. **Compile** everything into the semantic model (§4.3). Every item carries `status: hypothesis | accepted | rejected` and a review priority (uncertainty × how many other items depend on it).

### 4.3 Semantic model (YAML, versioned, human-editable)

```yaml
version: 12
database: HALCYON
profile: tsql2008
entities:
  customer:
    source: dbo.CUST_MSTR
    key: [CUST_ID]
    description: "A customer account"
    default_filters:
      - expr: "ISNULL(DEL_FLG,'N') <> 'Y'"
        reason: "soft delete; 3.1% of rows flagged, none referenced by orders after flag date"
        status: accepted
    columns:
      first_name: {expr: "RTRIM(CFNM1)", status: accepted, confidence: 0.91}
      phone:      {expr: "COALESCE(PHN_NBR, PHONE)", note: "PHONE deprecated ~2015-03", status: hypothesis}
      city:       {expr: "UPPER(RTRIM(CAST(ADDR_XML AS XML).value('(/a/c)[1]','varchar(60)')))", status: accepted}
  order:
    source: dbo.TRANS_MSTR
    discriminator: "TRANS_TYP_CD = 'O'"          # god table split
    key: [STORE_CD, ORD_NO]                       # implicit composite key
    columns:
      order_date: {expr: "CONVERT(datetime, ORD_DT_TXT, 112)", from_type: "CHAR(8) YYYYMMDD"}
      status:
        expr: ORD_STS
        enum: {1: pending, 2: approved, 3: shipped, 5: returned, 9: cancelled}
        status: hypothesis
      total_amount:
        expr: "CASE WHEN ORD_DT_TXT >= '20150101' THEN TOT_AMT_C / 100.0 ELSE TOT_AMT END"
        note: "unit drift: cents after 2015-01-01"
relationships:
  - name: order_customer
    from: order
    to: customer
    on: "RIGHT('0000000000' + CAST(customer.CUST_ID AS VARCHAR(10)), 10) = order.CUST_NO"
    evidence: {inclusion: 0.998, name_similarity: 0.71, source: inferred}
    confidence: 0.93
    status: accepted
  - name: payment_party_customer
    from: payment
    to: customer
    condition: "payment.REF_TYP = 'C'"            # overloaded field
    on: "..."
metrics:
  revenue: {expr: "SUM(order.total_amount)", filters: ["order.status NOT IN (5, 9)"], status: accepted}
glossary:
  "active customer": "customer with at least one non-cancelled order in the last 12 months"
```

Note the safe cast in `order_customer`: `tsql2008` has no `TRY_CAST`, and `ISNUMERIC` is unreliable, so the compiler compares as zero-padded strings instead of casting the dirty side to INT.

**Verified query library** (Cortex VQR / Genie trusted assets): `{question, logical_sql, params, verified_by, verified_at}` — written against logical names so it survives physical changes.

### 4.4 Answering a question (online pipeline)

Every step emits a trace event over SSE.

1. **Route**: embedding match against the verified library and semantic cache (local `fastembed`, `BAAI/bge-small-en-v1.5`). If it matches with high similarity and the parameters can be slot-filled → run it and label the answer **Verified**. Zero generation tokens.
2. **Understand** (small/cheap model): rewrite into an explicit question, extract entities, time ranges, literal values, and requested measures (Uber's "prompt enhancer").
3. **Ambiguity check**: vague terms ("top", "recent", "active", "best"), glossary misses, and enum-hypothesis columns in play. Classify as intent / implementation / knowledge ambiguity. If it needs the user, ask one clarification with 2–4 concrete options — each option shows how many rows it would return.
4. **Link**: hybrid retrieval (BM25 + embeddings) over entities, columns, synonyms, glossary; then LLM rerank to top ≤ 7 entities; prune to relevant columns. **Value linking**: map literals to stored values with the value index (trigram + rapidfuzz): "new york" → `'NY '`, `'N.Y.'`, `'New York'`. If the rerank margin is small, show proposed tables as editable chips (Uber's table agent).
5. **Plan joins deterministically**: Steiner tree over the join graph (networkx approximation), edge weight = 1 − confidence. The LLM never invents join conditions.
6. **Generate**: the LLM writes SQL against the *logical* schema (entity.column names, ANSI-ish SQL), given the pruned schema, the join plan, 2–3 retrieved verified examples, and glossary hits. Default: 2 candidates with different strategies (direct; decompose-then-compose). High-stakes mode: 3–4.
7. **Compile** (`compiler.py`, deterministic): parse with sqlglot → expand each entity into a derived table exposing logical columns (applying default filters, discriminators, normalization expressions) → substitute relationship conditions → push down predicates where safe → hand to `dialect.py` for the capability profile → final physical SQL.
8. **Validate**: single SELECT only; every referenced object exists; required default filters are present; no join fan-out (compare `COUNT(*)` vs `COUNT(DISTINCT key)` on a probe when aggregating over a 1-to-many join); profile linter passes; **cost guard**: estimated plan (`SET SHOWPLAN_XML ON` on SQL Server, `EXPLAIN` on Postgres, `EXPLAIN QUERY PLAN` on SQLite) — if a full scan of a huge table is predicted, stop and ask the human (show estimated rows/time and a cheaper variant, e.g. with a date bound).
9. **Execute** read-only with timeout and row cap.
10. **Repair** on error: feed the error + the relevant schema slice back, up to 3 attempts; a researcher step may run small probe queries (`SELECT DISTINCT TOP 20 …`) to check values (LinkedIn's fixer).
11. **Select**: group candidates by result-set equality; majority wins; tie → one LLM pairwise judgment on a result preview.
12. **Confidence** (0–1) from: candidate agreement, min confidence of join edges and columns used (accepted = 1.0), validator warnings, repairs needed (penalty), linking margin, verified hit (→ 0.99). Start hand-weighted; in Phase 5 fit a logistic regression on Gauntlet runs and show a reliability diagram in the Lab.
13. **Gate**: ≥ 0.80 → show. 0.50–0.80 → show with an **Assumptions** panel and a "Looks right / Fix" prompt. < 0.50 → clarify, or abstain with reasons and the best partial answer.
14. **Explain**: plain-English summary of what the SQL does, plus every assumption the compiler applied ("excluded 412 soft-deleted customers", "converted amounts stored in cents after 2015").
15. **Learn**: "Looks right" on a high-value question → candidate for the verified library (human confirms). An edited SQL → diffed against the generated one; if the difference is a mapping error, open a review item in the Workbench.

### 4.5 Fallback ladder

| Rung | Trigger | What happens |
|---|---|---|
| R0 | Verified / cache hit | Run the trusted query. |
| R1 | Default | Logical generation, 2 candidates, vote. |
| R2 | Execution or validation error | Repair loop (≤ 3). |
| R3 | Dialect can't express it (JSON on 2008, LAG/LEAD, running totals) or question is multi-step | Decompose: run simpler SQL pieces, pull limited results into DuckDB/pandas, finish there. Mark the answer "computed partly outside the database". |
| R4 | Needed columns aren't in the semantic model | Physical-schema agent: pruned raw schema + probe queries; answer marked lower confidence; open a Workbench item to map the new columns. |
| R5 | Still unsure | Ask the human, or abstain with reasons. |

LLM provider fallback, independent of the ladder: primary model → next provider on 429/timeout (respect `retry-after`) → local Ollama if configured → graceful "LLM unavailable, verified queries still work".

### 4.6 LLM layer without a paid key

- One client (`openai` SDK with `base_url` swapped). Providers in `llm/providers.yaml`: Groq, Gemini (OpenAI-compatible endpoint), OpenRouter free models, Ollama. Model IDs and limits change often — keep them in config and check provider docs at build time.
- Role routing: small fast model for understand/ambiguity/rerank; the strongest available open model for generation and enrichment.
- **Budgeter**: tracks tokens-per-minute and per-day per provider/model, queues calls, and degrades gracefully (fewer candidates) instead of failing.
- Prompt hygiene: compact schema serialization (one line per column: `customer.first_name text — "first name" e.g. 'Ana','Raj'`), static system prompts first (to benefit from provider-side prompt caching).
- Eval runs are the heaviest consumer: the harness must support resume, spreading a suite across days, and rotating providers.

### 4.7 MCP mode ("Claude as the brain", no API key)

`engine/stratum/mcp/server.py` (Python MCP SDK) exposes: `list_databases`, `search_semantic_model`, `describe_entity`, `sample_values`, `plan_joins`, `compile_logical_sql`, `validate_sql`, `run_sql`, `propose_semantic_fix`. The user adds the server to Claude Desktop or Claude Code; Claude does the reasoning under their Claude plan and the engine does everything deterministic and safe. Stretch: runs started via MCP are written to the app store so the web UI shows them live.

### 4.8 Safety

Read-only enforcement (§CLAUDE.md), statement timeout, row cap, cost guard, no credentials or bulk data to LLMs, per-connection audit log of every executed statement, prompt-injection hygiene (data values are quoted as data in prompts, never as instructions).

---

## 5. Frontend

### 5.1 Concept and design system

**Concept: excavation.** A legacy database is sediment — decades of layers. Stratum digs through them. Visual language borrows from core samples, survey maps and lab instruments, not from "AI" clichés.

- Palette (dark default): Ink `#0B0C0E` background, Bone `#E9E4D8` text, Phosphor `#FFB23F` accent (old amber terminals), Oxide `#D9573B` for danger/low confidence, Lichen `#9BC48A` for verified/high confidence, Slate `#2A2D33` for surfaces. Light mode inverts Ink/Bone.
- Type: **Bricolage Grotesque** (display; animate its width/optical-size axes on scroll), **IBM Plex Sans** (UI/body), **IBM Plex Mono** (SQL, data, numbers; `tabular-nums`). Plex is a quiet nod to the mainframe era these databases come from.
- Large, confident type scale with `clamp()`; tight grid; generous negative space; hairline rules like survey drawings.
- **Anti-slop rules**: no purple/blue gradient blobs, no glassmorphism card grids, no sparkle icon for "AI", no "Unlock the power of…" copy, no three-feature cards with emoji. Copy is concrete and uses real examples from HALCYON (`CFNM1 → customer.first_name`). Every animation explains a state change.

### 5.2 Motion spec

- Durations 180 / 320 / 600 ms; default ease `expo.out`; exits faster than entrances.
- Headlines: GSAP SplitText line-mask reveals.
- The **pipeline trace** is a vertical core sample: each stage is a stratum that fills as its SSE event arrives, with tokens and milliseconds printed in mono. Repairs show as a visible re-drill.
- GSAP Flip: when the user accepts proposed tables, the nodes fly from the 3D atlas / list into the query chips.
- Result rows stagger in (capped at the first 30 rows); numbers count up once.
- Confidence meter: needle settles with a slight overshoot; color crosses Oxide → Phosphor → Lichen.
- Landing: Lenis smooth scroll + ScrollTrigger scrubbing through the 16 anti-patterns as strata; each stratum shows a before/after micro-demo (raw `ORD_STS = 9` → "cancelled").
- `prefers-reduced-motion`: replace all motion with instant state changes and opacity-only fades; pause the 3D scene.

### 5.3 Pages

| Route | What it does |
|---|---|
| `/` | Narrative landing: the problem, the 16 anti-patterns as scrollable strata, live demo query on HALCYON, the Gauntlet scoreboard. |
| `/connect` | Three doors: **Demo databases** (one click), **Paste a connection URL** (parsed live into fields), **Drop a SQLite file**. Test → privileges check → go. |
| `/db/[id]/excavate` | Live excavation: counters, detector findings streaming in, 3D schema field assembling as tables are classified and join edges light up. |
| `/db/[id]/atlas` | 3D schema galaxy (instanced meshes; clusters by inferred domain; dead tables sink and dim; edges thickness = confidence). Click → table inspector with profile, findings, logical mapping. 2D fallback with xyflow. |
| `/db/[id]/workbench` | HITL review queue sorted by impact: accept / edit / reject hypotheses (enum labels, joins, soft-delete rules, descriptions), each with evidence (value distributions, sample rows, the procedure text it came from). Keyboard-first (J/K/A/R/E). |
| `/db/[id]/ask` | The main surface: question box → trace → clarification card or table chips when needed → answer: result grid, auto chart, SQL (Monaco, logical ⇄ physical toggle), assumptions, confidence, Verified badge, feedback. |
| `/lab` | Eval dashboard: run suites, EX per anti-pattern heatmap, run-vs-run diff, per-question drill-down (gold result vs ours, side by side), reliability diagram, token/latency charts, knowledge-recall scores. |

### 5.4 3D performance budget

Instanced meshes up to 5k nodes, LOD labels (only nearest/selected), `frameloop="demand"` when idle, pause off-screen, DPR clamp 1.5, target 60 fps on integrated GPUs. Lazy-load three.js only on routes that use it.

---

## 6. API contract

```
POST   /connections                  {url | fields}           → {id, dialect, profile, privileges}
POST   /connections/test             {url | fields}           → {ok, error_hint}
POST   /databases/{id}/excavate      → SSE: stage.*, finding, progress, done
GET    /databases/{id}/model         → semantic model (JSON)
GET    /databases/{id}/review        → review queue
PATCH  /databases/{id}/model/items/{item_id}   {action: accept|reject|edit, value}
POST   /ask                          {database_id, question, mode: default|thorough} → SSE
POST   /ask/{run_id}/clarify         {option_id | free_text}
POST   /ask/{run_id}/approve         {tables?, allow_expensive?}
POST   /ask/{run_id}/feedback        {verdict, edited_sql?}
POST   /verified                     {question, logical_sql, params}
POST   /eval/runs                    {database, suite, config}  → run id (async)
GET    /eval/runs/{id}               → report
```

SSE events for `/ask`: `stage.start`, `stage.end {name, ms, tokens, detail}`, `clarification.needed {question, options[]}`, `tables.proposed {entities[], scores}`, `sql.candidate {logical, physical}`, `validation {warnings[]}`, `cost.warning {est_rows, est_ms, cheaper_variant}`, `result {columns, rows, truncated}`, `confidence {score, reasons[]}`, `explanation {text, assumptions[]}`, `done`.

---

## 7. Build order (phases with acceptance criteria)

Estimates are focused working days.

### Phase 0 — Scaffold (0.5 d)
Monorepo, Makefile, docker-compose (SQL Server 2022, Postgres 16), `uv` project, Next.js app, CI lint/typecheck.
**Done when:** `make up && make engine && make web` all run; `/health` returns versions of both DB servers.

### Phase 1 — The Gauntlet: HALCYON first (2 d)
Download/restore AdventureWorks → `AW_CLEAN`; legacifier → `HALCYON` + manifest; `halcyon_v1.yaml` with ≥ 40 questions (two gold formulations each); scorer; naive baseline runner.
**Done when:** gold formulations agree for every question; naive baseline EX is reported per anti-pattern (expect it to be poor — that's the point).

### Phase 2 — Connectors, profiles, introspection, profiling (1.5 d)
Three connectors, capability profiles + linter, read-only executor, cost guard, introspection and profiling with pattern signatures.
**Done when:** connecting to all three engines works from a URL; a DML statement is rejected before hitting the driver; profiling HALCYON completes in < 3 min.

### Phase 3 — Excavation (3 d)
All detectors, procmine, LLM enrichment with caching, semantic model compile, review-queue ordering.
**Done when:** knowledge-recall vs manifest: join recall ≥ 0.85 at precision ≥ 0.85; soft-delete rule recall ≥ 0.9; dead-table F1 ≥ 0.85; composite keys recovered for order lines. (Tune thresholds, not the manifest.)

### Phase 4 — Ask pipeline v1 (3 d)
Route, understand, link (+ value index), join planning, logical generation, compiler, dialect rewriter, validation, execution, repair.
**Done when:** HALCYON EX clearly beats the naive baseline overall and on every anti-pattern tag; every compiled query passes the `tsql2008` linter; mean tokens per question < 6K.

### Phase 5 — Robustness and HITL (2 d)
Multi-candidate + voting, ambiguity detection + clarification, confidence scoring + calibration, gating, fallback ladder R3–R5, verified library + semantic cache, simulated user for ambiguous questions.
**Done when:** clarification precision and recall ≥ 0.8 on the ambiguous subset; confidence is monotonic with accuracy on the reliability diagram; the performance-trap questions trigger the cost guard.

### Phase 6 — Frontend core (3 d)
Design tokens, fonts, layout; Connect; Ask with streaming trace, clarification cards, table chips, results grid, Monaco SQL (logical ⇄ physical), assumptions, confidence, feedback.
**Done when:** a full question-to-answer flow on HALCYON works in the browser with every SSE event rendered; Lighthouse accessibility ≥ 95.

### Phase 7 — Frontend showpieces (3–4 d)
Landing narrative with ScrollTrigger strata; Excavate live view; 3D Atlas; Workbench; Lab dashboard; motion polish; reduced-motion pass.
**Done when:** 60 fps on the Atlas with HALCYON's ~400 tables on a laptop iGPU; reduced-motion mode verified; no page ships a pattern from the anti-slop list.

### Phase 8 — Breadth and MCP (2 d)
`pagila_legacy` and `chinook_legacy` suites, BIRD mini-dev sanity run, `legacy_strict` profile runs, MCP server, ablation report in `docs/PROGRESS.md`.
**Done when:** all four suites run from the Lab; Claude Desktop can answer a HALCYON question through the MCP server end to end.

---

## 8. How to drive Claude Code

Start each session with:

> Read CLAUDE.md and docs/PLAN.md. We are on Phase N. Before writing code, list the files you will create or change and the acceptance checks you'll run. Then implement, run the checks, and update docs/PROGRESS.md.

Useful follow-ups:
- "Run `make eval DB=halcyon SUITE=v1`, show EX per anti-pattern, and pick the worst tag. Diagnose three failing questions from their traces before changing anything."
- "Add an ablation flag for <component> and report the delta."
- "Review the last diff against the non-negotiables in CLAUDE.md."

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Free-tier token limits stall evals | Budgeter, caching, provider rotation, resumable eval runs spread over days. |
| AdventureWorks restore is fiddly on some machines (SQL Server image is x86-64) | Document both `.bak` restore and a scripted load; on Apple Silicon use Docker's x86 emulation. |
| Overfitting the engine to our own legacifier | Keep BIRD mini-dev as an untouched external check; write detectors from general principles; never read the manifest from engine code. |
| 3D and motion eat the schedule | Phases 1–6 must work without any 3D; Phase 7 is additive. |
| sqlglot T-SQL edge cases | Round-trip every compiled query through parse → generate → the real server in tests; keep a regression file of fixed cases. |
