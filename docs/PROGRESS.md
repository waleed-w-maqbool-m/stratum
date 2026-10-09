# Stratum — Progress

Every number here was produced by running the commands shown. Nothing is projected, and
where a number has a caveat the caveat is next to it.

---

## Results

Two benchmark databases, two SQL dialects, two model families. Each comparison is a
**controlled pair**: the naive baseline and the full pipeline were run on the same
questions with the same model, pinned so a rate limit could not silently swap it.

| | Chinook (SQLite) | Pagila (Postgres 16) |
|---|---:|---:|
| model | `gpt-oss-120b` | `qwen3.8-27b` |
| naive baseline — whole schema, one prompt | **23.9%** | 30.8%* |
| **full pipeline** | **60.9%** | **57.8%** |
| tokens / question, naive → full | 5,817 → **3,095** | → 2,911 |
| cost guard on the perf-trap questions | 2 / 2 | 1 / 2 |
| false abstentions | 0 | 9 (7 of them one bug, below) |
| gold pairs agreeing · reversibility checks | 46/46 · 21/21 | 45/45 · 27/27 |

\* Pagila's baseline covers 26 of 45 questions — the daily token budget ran out. On those
same 26 the full pipeline scores 57.7%. See below.

On Chinook the full pipeline is **2.5× as accurate as the baseline on 53% of the
tokens**: the expensive part of the baseline's prompt is the whole physical schema, and
the semantic model replaces it with the few entities a question actually touches.

### Chinook, per anti-pattern (same model, same 46 questions)

| anti-pattern | naive | full | n |
|---|---:|---:|---:|
| soft_delete | 23.3% | **63.3%** | 30 |
| cryptic_names | 33.3% | **52.4%** | 21 |
| god_table | 15.8% | **63.2%** | 19 |
| missing_fk | 31.6% | **57.9%** | 19 |
| drift | 7.1% | **64.3%** | 14 |
| inconsistent_types | 0.0% | **80.0%** | 10 |
| string_quality | 0.0% | **37.5%** | 8 |
| composite_key | 57.1% | **71.4%** | 7 |
| ambiguous | 20.0% | **60.0%** | 5 |
| embedded_format | 20.0% | 20.0% | 5 |

The rows that move furthest — drift 7% → 64%, inconsistent types 0% → 80%, god tables
16% → 63% — are the ones whose rules live in the data rather than the schema. The
baseline cannot see that money moved from dollars to cents in 2023, or that one table
holds three kinds of transaction; of its 46 queries, **0** applied the cents cutover and
**0** the god-table discriminator. Composite keys barely move because they *are* visible
in a schema — the baseline was already decent at them.

### Pagila, per anti-pattern (full pipeline)

| anti-pattern | EX | n | | anti-pattern | EX | n |
|---|---:|---:|---|---|---:|---:|
| drift | 44.4% | 18 | | missing_fk | 53.8% | 13 |
| god_table | 57.1% | 14 | | cryptic_names | 80.0% | 10 |
| soft_delete | 60.0% | 10 | | inconsistent_types | 40.0% | 10 |
| composite_key | 28.6% | 7 | | dialect_friction | 71.4% | 7 |
| string_quality | 57.1% | 7 | | magic_numbers | 50.0% | 6 |
| embedded_format | 33.3% | 3 | | misleading_docs | 100.0% | 2 |

**Pagila's baseline is partial.** The naive run shares Qwen's 200,000-token day with the
full run, and the day ran out after 26 of 45 questions. On those 26 — the same
questions, the same model — the comparison is:

| | EX | tokens / question |
|---|---:|---:|
| naive baseline | 8 / 26 = **30.8%** | 3,711 |
| full pipeline | 15 / 26 = **57.7%** | 2,354 |

The same shape as Chinook: nearly twice the accuracy for 37% fewer tokens. Its answers
are saved, and this asks the remaining 19 once the budget refreshes:

```bash
python -m gauntlet.runner --suite pagila_v1 --mode naive --resume --providers groq-qwen --pace 30
```
 The naive baseline failed on
`tblFilm` exactly as designed: `relation "tblfilm" does not exist`.

**Seven of pagila's nine false abstentions are one bug.** The generation stage gave the
model 500 output tokens; on those seven questions Qwen spent all 500 reasoning and
returned nothing, and the client dropped the provider instead of retrying. It is fixed —
one retry at double the budget, within the question's token ceiling, unit-tested — but
the fix landed while the run was in flight, and **57.8% is the number for the code that
was measured.** Re-asking just those seven with the fix would be choosing which
questions to retry; the fair measurement is a full re-run on a fresh daily budget.

### Knowledge recall — what excavation recovered, scored against the answer key

No LLM in the query path; this is the deterministic half plus enrichment, scored by the
one module allowed to read `manifest.json`.

| | Chinook | **Pagila (held out)** |
|---|---:|---:|
| joins F1 | 1.000 | **0.850** (P 0.895 · R 0.810) |
| keys F1 | 1.000 | **0.875** |
| composite keys | 3 / 3 | 1 / 3 |
| soft-delete rules F1 | 1.000 | **0.800** (recall 1.000) |
| dead-table F1 | 1.000 | **1.000** |
| table classification | 134 / 134 | **89 / 90** |
| drift F1 | 1.000 | **0.800** |
| god tables F1 | 1.000 | **1.000** |
| embedded formats F1 | 1.000 | 0.667 |
| enum values decoded | 11 / 22 | 10 / 15 |

**Pagila is the honest test, and it is not perfect.** Chinook's structure is recovered
completely; Pagila's — built *after* the engine, by a legacifier the detectors were not
written against — at 0.85 on joins. That gap is what a detector set looks like on a
database it has not seen, and it is reported as it is. Its misses are specific: an
email that moved *into* a JSON blob (the relocation detector looks for moves between
tables), a `|`-delimited list, and a reference column that means a rental for payments
and a transaction number for refunds.

The strongest single result is not an accuracy figure. **Summing revenue through
Pagila's excavated model returns $67,416.51 — the clean database's figure, to the
cent** — with no LLM involved. That total requires the god-table discriminator
(`txn_typ = 'P'`) and the dollars-to-cents cutover, both found from the data:

```sql
CASE WHEN txn_ts >= '2022-04-01 00:02:24' THEN amt_c / 100.0 ELSE amt END
```

The plausible wrong answers were all nearby. Summing both money columns over every
transaction type gives $67,828.52 — 0.6% off, exactly the number that gets shipped to a
dashboard and believed. Trusting the column's comment ("amount in cents") gives $42,989.

### Calibration

Fitted on the Chinook run: monotonic, **expected calibration error 0.228 → 0.081**. The
fit is in-sample on 48 observations, so the 0.081 is optimistic; a held-out split needs
more runs than one free-tier day allows.

---

## What this pass found and fixed

A second database on a second dialect was the point of this pass, and it paid for itself
several times over. The most important finding affects the Chinook numbers reported
before it.

### A data-corrupting bug in value normalisation

The synonym clusterer — built to fold `USA` / `U.S.A.` / `usa` together — was rewriting
*real values*. It judged "same spelling" by whole-string fuzzy similarity, which rewards
shared structure rather than shared meaning, so near-identical *different* values were
merged. In the Chinook model that earlier results were measured on:

- **`billing.customer_number`**: customers 4, 42, 21 and 18 others were rewritten to
  customer 2. Every per-customer revenue question ran on corrupted keys.
- `representative_number`: support reps 4 and 5 merged into 3.
- every employee email merged into `andrew@chinookcorp.com`; two hire dates merged; two
  different media types merged.
- on Pagila, `20220822` merged into `20220823`, and a replacement cost of `9.99` into
  `19.99`.

SQLite raised no error for any of it; the answers were simply wrong. Fixed three ways,
each tested: clustering only runs on word-like categorical text (values containing
digits, `@`, `<`, `{`, `|`, `/` or `:` are identifiers, dates or documents, where *near*
means *different*); fuzzy matching is word by word; and the prefix rule that merged `US`
into `USA` is gone — it also merged `NS` into `NSW`, Nova Scotia into New South Wales.
Abbreviations are world knowledge and now come only from the LLM value step, which knows
the difference. **The earlier 50.0% was measured with this bug in place; 60.9% is
without it.**

### Postgres, which the engine had never run against

| found | fix |
|---|---|
| psycopg 3 rejects `NOT IN %s` with a tuple — the very first catalog query failed | `<> ALL(%s)` with a list |
| partitions listed as tables: seven copies of `payment` | hidden (`relispartition`) |
| primary keys read via `pg_get_indexdef` came back *quoted*, so `"intFilmID"` never matched `intFilmID` | read `attname` |
| mixed-case names unquoted, so `tblFilm` folded to `tblfilm` and every probe into it silently failed | the model records which names need quoting; the compiler and excavation's probes quote exactly those — never raw model output, so the baseline is not helped |
| `Decimal` not counted as numeric: every money column profiled as text | counted |
| native timestamps sliced with `SUBSTR`; text dates spliced into strings that only *look* like dates | `TO_DATE` / `CAST … AS DATE` on Postgres, no conversion for native types |
| casts to `REAL` (float4 — and `ROUND(real, 2)` does not exist) and unguarded `CAST … AS INTEGER` on blanks | `NUMERIC`, and casts that only fire on digits |
| drift merged `COALESCE(timestamp, 'DD/MM/YYYY' text)` | each side normalised to its type first |
| the dollars/cents drift oriented backwards: the profiler samples head and tail equally, so both columns came out exactly half NULL and the tie went the wrong way | the dated cutover decides the order, tried both ways |

### General detector and pipeline fixes

- **Pushed-down predicates compared the raw column.** A filter moved into the derived
  table kept its logical name, which there resolved to the *physical* column of the
  same spelling — `cfnm = 'AUSTIN'` against the space-padded original, matching nothing
  with no error. Now translated to the column's expression, or not pushed.
- **Names in capitals.** "Austin Cintron" against a column loaded in upper case. Columns
  whose case carries no meaning are compared case-insensitively.
- **Stale documentation reached the generator.** Enrichment copied `fin_txn.amt`'s
  comment — "amount in cents", false — into the description, and the model divided by 100
  a second time. Where the model has resolved a unit drift it now describes the unit
  itself, and comments are passed to enrichment labelled as claims the values override.
- **Soft deletes** in two new styles: a lettered status (`A` / `I` / `X`) and a
  Hungarian tombstone (`dtmDltd`) — dated by its type, not its name.
- **Names**: `cntry` read as an English word because of its `y`; `street` was being
  split into `str` + `eet`; `str_no` lost the `str` that says *store*.
- **Joins**: inclusion only counts as evidence for a column named like an identifier —
  a six-digit rental *time* sits inside any large dense key — and a declared primary
  key is used even on a two-row table.
- **The cost guard** used a fixed half-million-row threshold, sized for the 8M-row log
  in the original plan; against scaled-down databases it could never fire. A big table
  is now big relative to its own database.
- **Validation** compared filters as text, so a correctly applied `"dtmDltd" IS NULL`
  read as missing and the repair loop "fixed" a correct query.

### Measurement integrity

- **The LLM cache ignored the model.** A completion cached from one provider was
  replayed for another, and a replay costs 0 tokens — so a repeated run reported a token
  cost that never happened. The key now includes the model, and eval runs do not read it.
- **Hidden reasoning tokens were uncounted.** Gemini reports thinking in `total_tokens`
  but not `completion_tokens`; one answer claimed 19 tokens and cost 670.
- **Runs can be pinned** (`--providers`), and every report records which model actually
  served it, counted from the calls made.
- **A model's own failures are never retried as outages** — only 429s, 503s and dropped
  connections are.

---

## Integrity notes

- **Chinook's abbreviation table is not independent of Chinook.** `naming.py` contains
  tokens (`artst`, `albm`, `gnr`, `plst`, `trk`, `mtyp`) written by the same process
  that wrote the Chinook legacifier. They inflate Chinook's cryptic-name handling.
  Nothing was added for Pagila: its abbreviations reach the model through enrichment or
  not at all, which is why Pagila's entity names include `rntl` and `lang`.
- **Pagila was not held out from engineering.** It was built after the engine, and the
  fixes it provoked are listed above. Each is a general rule, justified without Pagila
  and unit-tested on a minimal case — but the numbers were measured after them, and a
  truly untouched database would score lower.
- **Each database is paired with one model.** The Chinook pair ran on `gpt-oss-120b`,
  the Pagila pair on `qwen3.8-27b`, because each free-tier model allows 200,000 tokens a
  day and a controlled pair needs ~300,000. The numbers compare methods within a row,
  not databases across one.

---

## Reproducing it

```bash
make setup                         # engine + psycopg + a local Postgres (pgserver), no Docker
make gauntlet                      # both databases: legacify, prove reversible, check gold
make excavate SUITE=pagila_v1      # or chinook_v1
make eval-naive SUITE=chinook_v1 PROVIDERS=groq
make eval       SUITE=chinook_v1 PROVIDERS=groq
make eval-resume SUITE=pagila_v1   PROVIDERS=groq-qwen   # continue after a daily limit
make test                          # 120 tests (109 engine, 11 scorer) + lint
```

Pagila runs on a real PostgreSQL 16 started from a pip wheel (`gauntlet/pg.py`), because
Docker needs hardware virtualisation, which is switched off on the build machine. The
source dump is pinned to `pagila-v3.1.0`: upstream master now needs Postgres 18.

**Free-tier budgets, measured rather than quoted:** Groq allows 200,000 tokens a day per
model. Gemini's free tier allows **20 requests a day per model** — not the 1,500 that
current articles repeat — so it serves enrichment, not evaluation.

## The web app

Every page has been driven in a browser against the running engine: Ask, Review, Atlas
and Benchmark on Pagila (Postgres), and excavation plus accept/reject decisions on a
scratch copy of the test fixture, so the benchmark models were never touched by hand.
Review was also checked at phone width.

| Page | What it does |
|---|---|
| **Landing** (`/`) | The revenue figure a naive query gets on a legacy schema, layer by layer, and the per-anti-pattern results from the tables above |
| **Connect** (`/connect`) | One-click demo databases, or your own URL; connections are saved Fernet-encrypted and reopened on demand |
| **Ask** (`/db/:id/ask`) | Streams the pipeline trace stage by stage; shows the answer, a confidence meter, the rules applied with rows excluded, and both the logical and physical SQL. Vague questions get a clarification built from the model; expensive ones get a cost warning with a cheaper variant |
| **Excavate** (`/db/:id/excavate`) | All sixteen steps laid out before they run, each filling with what it found as its event arrives |
| **Review** (`/db/:id/workbench`) | The review queue ordered by impact, each inference stated in one sentence with its evidence, kind-specific tables (code meanings, the parts of a mixed table), and editing for names and codes. Keyboard-driven (J/K/A/R/E) |
| **Atlas** (`/db/:id/atlas`) | The schema in 3D: live tables above the grid, excluded ones sunk below it, join brightness as confidence. A table view replaces it under reduced motion |
| **Benchmark** (`/lab`) | Paired baseline-vs-Stratum results per suite, per anti-pattern, every question with gold and actual result side by side, knowledge recall, and the calibration chart |

Engine fixes that came out of building it:

- **Clarification offered meaningless rankings.** "Best customers" offered "top 10 by
  total id" and "by total store number". Ranking measures now exclude identifiers,
  codes, statuses, years and join columns, come only from the ranked entity and the
  tables that point at it, and always include a count reading.
- **Review decisions were lost on re-excavation.** Findings were overwritten with fresh
  hypotheses. Decisions a person made are now reapplied to every finding the new run
  reproduces, and the run reports how many were kept.
- **The enum detector called a two-row name column a code** (`stf_fnm`: Mike, Jon). A
  column where every row has its own value is no longer treated as a code.
- **The enrich stage reported 0 ms** in its trace event regardless of how long it took.
- **SSE never parsed in the browser** (sse-starlette sends `
`) and the dev proxy
  gzip-buffered the stream; both fixed, so the Ask page had not worked in a browser
  before this pass.
- `DELETE /connections/{id}` forgets a connection and keeps its models.

Known: drei's HTML labels log a React "synchronously unmount a root" warning in
development mode only.

### Public demo build

The app builds as a static site for Cloudflare (`npm run build:demo`), replaying
engine output recorded by `web/scripts/record_demo.py`. The recordings were checked
answer by answer before publishing; every one shown is correct. Recording against a
real engine turned up four more bugs, all fixed:

- **Clarified answers leaked through the answer cache.** The cache is keyed by question
  text, so once "best customers by money" was answered, "best customers by number of
  orders" replayed the same rows for 0 tokens. A clarified question now bypasses the
  cache in both directions. Covered by a test that fails without the fix.
- **One unreachable database froze the whole engine.** Reopening a saved connection ran
  on the event loop, so every request, `/health` included, waited for its timeout.
- **"Run the full query anyway" dropped the chosen clarification**, so the engine asked
  the same clarifying question again.
- **The landing page scrolled sideways on phones**: a grid's implicit column grew to fit
  the longest SQL line.

Found and not fixed:
- **Enrichment varies from run to run.** Re-excavating Pagila three times gave three
  models; one lost the film-rating codes, another lost two status-code meanings. The
  demo's Pagila answers come from the model the benchmark measured (v1). Chinook's come
  from a fresh excavation: the benchmarked Chinook model returned an empty total revenue
  and an unmerged "U.S.A." for two of the demo questions, which the fresh one got right.
- **A cached answer skips the cost guard.** The replayed SQL runs without the estimate
  check that the first run went through.

---

## Not built

| | |
|---|---|
| **HALCYON** (SQL Server) | first blocked by hardware virtualisation, then out of time. The `tsql2008` profile and its linter are implemented and unit-tested, but have never run against a live SQL Server. An unrun draft of the legacifier is on the `halcyon-wip` branch |
| ablation report, BIRD mini-dev, `legacy_strict` runs | the harness supports all three (`--ablate`); each needs its own day of free-tier budget |
| held-out calibration | the fit above is in-sample |
