# CLAUDE.md — Stratum

Stratum is a natural-language-to-SQL system for **legacy databases**. It connects to a database, excavates meaning from a messy schema (cryptic names, missing FKs, god tables, magic numbers, soft deletes, mixed types, embedded XML/JSON, schema drift), compiles that into a human-reviewable **semantic model**, and answers questions by generating SQL against that clean logical layer, then compiling it down to the physical, dialect-specific SQL.

The full build plan lives in `docs/PLAN.md`. Read it before starting any phase, and work phase by phase in the order it gives.

## Stack

- `engine/` — Python 3.12, `uv`, FastAPI, SQLAlchemy 2 (Core only), `sqlglot`, DuckDB, `rapidfuzz`, `fastembed`, `networkx`, pydantic v2, `sse-starlette`, `openai` SDK (used against OpenAI-compatible endpoints: Groq, Gemini, OpenRouter, Ollama).
- `web/` — Next.js (App Router, latest stable), React, TypeScript strict, Tailwind CSS v4, GSAP (+ ScrollTrigger, SplitText, Flip via `@gsap/react`), three.js via `@react-three/fiber` + `drei`, Lenis, Motion, Monaco, TanStack Table + Query, Zustand, `@xyflow/react`.
- `gauntlet/` — benchmark databases, the legacifier, question sets, gold SQL, scorer.
- `infra/` — `docker-compose.yml` (SQL Server 2022, Postgres 16), seed scripts.

## Commands

```
make up            # start SQL Server + Postgres containers
make gauntlet      # build clean + legacy benchmark databases
make engine        # run FastAPI on :8000
make web           # run Next.js on :3000
make eval DB=halcyon SUITE=v1   # run the benchmark and write a report to gauntlet/reports/
make test          # engine unit tests + web typecheck/lint
```

## Non-negotiables

1. **Read-only, always.** Every user SQL statement is parsed with sqlglot; anything that is not a single SELECT is rejected before it reaches a driver. Execution runs inside a transaction that is rolled back, with a statement timeout and a row cap.
2. **Credentials never reach an LLM**, and neither do full tables. LLMs see schema, profiles, and at most a handful of sample values per column.
3. **The legacifier manifest is the answer key.** `gauntlet/**/manifest.json` records every anti-pattern injected. Engine code must never read it. You (Claude Code) must not hand-write semantic-model content for benchmark databases, because you wrote the legacifier and would be leaking the answers. The engine's own excavation pipeline must produce it.
4. **Token budget is a hard constraint.** Default runtime is Groq's free tier (roughly 8K tokens per minute on the large models). A question in default mode must stay under ~6K total LLM tokens. Prefer deterministic code over an LLM call wherever the answer can be computed.
5. **Dialect capability profiles are law.** Generated SQL for a connection must pass that connection's profile linter (e.g. `tsql2008` forbids OFFSET/FETCH, IIF, CONCAT, TRY_CAST, FORMAT, LAG/LEAD, JSON functions, STRING_AGG).
6. **Every animation must explain something** (a state change, a cause, a sequence). Respect `prefers-reduced-motion` everywhere.

## Conventions

- Engine: pure functions for analysis steps; each excavation detector is a module in `engine/stratum/excavate/` returning typed findings with `evidence` and `confidence`.
- Every pipeline stage emits a trace event (`stage`, `ms`, `tokens`, `detail`) — the UI and the eval harness both consume them.
- Tests: every detector has a unit test built on a tiny fixture DB in `engine/tests/fixtures/`.
- After finishing a phase, run `make eval` and paste the summary table into `docs/PROGRESS.md`.
