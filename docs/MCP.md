# Using Stratum from Claude Desktop (no API key)

A website cannot call Claude through a subscription, but MCP is the sanctioned way for
Claude to use local tools. So Stratum ships as an MCP server: **Claude does the
reasoning under your own plan, and Stratum does everything deterministic and everything
dangerous.**

This is the route that needs no Groq key, no card and no cloud account.

---

## What each side is responsible for

| Claude decides | Stratum guarantees |
|---|---|
| which entities a question needs | that the SQL is read-only, parsed before it reaches a driver |
| what the SQL should compute | that it compiles to correct physical SQL for this dialect |
| how to phrase the answer | that soft-delete and discriminator filters are applied |
| when to ask you something | that joins are the ones excavation actually verified |
| | that an expensive query is refused, not run |

Claude never writes physical SQL and never sees a cryptic column name. That is the same
bet the rest of the system makes — the legacy mess is handled once, in reviewed code —
just with a different model holding the pen.

---

## Setup

**1. Install the extra and excavate a database.**

```bash
cd engine
uv sync --extra mcp
.venv/Scripts/python.exe -m stratum.cli excavate \
    sqlite:///../gauntlet/build/chinook_legacy.sqlite --name chinook_legacy --offline
```

`--offline` skips the LLM enrichment step, which is the only part of excavation that
wants a key. Everything else — the detectors, the join inference, the compiler — is
deterministic.

**2. Register the server with Claude Desktop.**

Add this to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "stratum": {
      "command": "C:\\path\\to\\Database Intelligence\\engine\\.venv\\Scripts\\python.exe",
      "args": [
        "-m", "stratum.cli", "mcp",
        "--database", "chinook_legacy",
        "--url", "sqlite:///C:/path/to/Database Intelligence/gauntlet/build/chinook_legacy.sqlite"
      ],
      "cwd": "C:\\path\\to\\Database Intelligence\\engine"
    }
  }
}
```

On macOS and Linux use `.venv/bin/python` and forward slashes throughout.

**3. Restart Claude Desktop.** Stratum's tools appear under the connectors icon.

For Claude Code, the same server registers with:

```bash
claude mcp add stratum -- /path/to/engine/.venv/bin/python -m stratum.cli mcp \
    --database chinook_legacy --url sqlite:///$PWD/gauntlet/build/chinook_legacy.sqlite
```

---

## The tools

| Tool | What it is for |
|---|---|
| `list_databases` | which databases have been excavated |
| `search_semantic_model` | **start here** — the relevant entities, their columns and the joins between them |
| `describe_entity` | one entity in full, including the filters applied by default |
| `sample_values` | the values actually stored in a column, before writing a filter on it |
| `plan_joins` | the verified join path between entities, with a fan-out warning |
| `compile_logical_sql` | logical SQL → physical SQL, plus every assumption applied |
| `validate_sql` | read-only, names exist, filters intact, dialect legal, no fan-out, not too expensive |
| `run_sql` | compile, validate and run, returning rows |
| `review_queue` | which mappings are still unreviewed guesses |
| `propose_semantic_fix` | record a correction for a human to accept |

---

## How a question goes

Ask Claude something like *"using stratum, what was our total revenue by billing
country?"* and it will:

1. `search_semantic_model("revenue by billing country")` → gets `billing`, `customer`
   and the join between them, with the logical column names.
2. Write SQL against those names:
   ```sql
   SELECT billing.billing_country, SUM(billing.total_amount) AS revenue
   FROM billing GROUP BY billing.billing_country
   ```
3. `run_sql(...)` → Stratum compiles that into the physical query, which applies the
   god-table discriminator (`TRANS_TYP_CD = 'I'`), excludes cancelled orders
   (`ORD_STS <> 9`) and resolves the cents cutover
   (`CASE WHEN ORD_DT_TXT >= '20230101' THEN TOT_AMT_C / 100.0 ELSE TOT_AMT END`) —
   none of which Claude had to know.
4. Report the rows **and the assumptions**, which `run_sql` returns and asks it to
   repeat. That is the part that makes the number checkable.

---

## Safety

The same non-negotiables apply here as anywhere else in Stratum, and they are enforced
on this side of the boundary rather than trusted to the model:

- `run_sql` refuses anything that is not a single SELECT, at both the compiler and the
  executor.
- Execution runs inside a transaction that is rolled back, under a statement timeout
  and a row cap.
- The cost guard refuses a query that would scan a huge table. Claude can retry with
  `allow_expensive=True`, but only after telling you what it will cost.
- `propose_semantic_fix` writes a proposal for review; it never edits the model.
- Credentials stay in Stratum's config and are never returned by any tool.
