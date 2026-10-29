.PHONY: help setup up down sources gauntlet chinook pagila excavate engine web eval eval-naive eval-offline eval-resume calibrate mcp test lint fmt clean

PY := engine/.venv/Scripts/python.exe
ifeq ($(OS),)
PY := engine/.venv/bin/python
endif

# Which benchmark. SUITE names its own database, dialect and clean copy, so it is
# usually the only thing to set:   make eval SUITE=pagila_v1
SUITE ?= chinook_v1
DB    ?= $(shell $(PY) -m gauntlet.databases name $(SUITE))
URL    = $(shell $(PY) -m gauntlet.databases url $(SUITE))

# Pin a run to one model family (see engine/stratum/llm/providers.yaml). A naive run and
# a full run are only comparable if they used the same model.
PROVIDERS ?=
PIN = $(if $(PROVIDERS),--providers $(PROVIDERS),)

help:                       ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

setup:                      ## install the engine and its dev tools
	cd engine && uv sync --python 3.12 --extra postgres --extra mssql --extra mcp
	cd engine && uv pip install pgserver

up:                         ## start SQL Server + Postgres (needs hardware virtualisation)
	docker compose -f infra/docker-compose.yml up -d
	@echo "waiting for the servers to accept connections..."
	@docker compose -f infra/docker-compose.yml ps

down:                       ## stop the containers
	docker compose -f infra/docker-compose.yml down

sources:                    ## download the clean source databases
	$(PY) -m gauntlet.sources.fetch chinook pagila pagila-data

gauntlet: chinook pagila    ## build every benchmark database and verify them

chinook: sources            ## Chinook (SQLite): legacify, prove reversible, cross-check gold
	$(PY) -m gauntlet.legacify.chinook
	$(PY) -m gauntlet.legacify.verify
	$(PY) -m gauntlet.scorer --check-gold --suite chinook_v1

pagila: sources             ## Pagila (Postgres 16, no Docker): legacify, verify, cross-check gold
	$(PY) -m gauntlet.legacify.pagila
	$(PY) -m gauntlet.legacify.verify_pagila
	$(PY) -m gauntlet.scorer --check-gold --suite pagila_v1

excavate:                   ## excavate the suite's legacy database
	cd engine && .venv/Scripts/python.exe -m stratum.cli excavate "$(URL)" --name $(DB)

engine:                     ## run the API on :8000
	cd engine && .venv/Scripts/python.exe -m stratum.cli serve --reload

web:                        ## run the web app on :3000
	cd web && npm run dev

eval:                       ## run the benchmark suite (PROVIDERS=groq to pin the model)
	$(PY) -m gauntlet.runner --suite $(SUITE) --mode full --pace 22 --patience 900 $(PIN)

eval-naive:                 ## run the naive one-prompt baseline
	$(PY) -m gauntlet.runner --suite $(SUITE) --mode naive --pace 22 --patience 900 $(PIN)

eval-offline:               ## exercise the deterministic half without an LLM
	$(PY) -m gauntlet.runner --suite $(SUITE) --offline

eval-resume:                ## continue a run that a rate limit interrupted
	$(PY) -m gauntlet.runner --suite $(SUITE) --mode full --resume --pace 22 --patience 900 $(PIN)

partial:                    ## build chinook_partial (half the keys declared) and score the labelling
	$(PY) -m gauntlet.partial build
	$(PY) -m gauntlet.partial score

calibrate:                  ## fit the confidence weights and check the score is honest
	cd engine && .venv/Scripts/python.exe -m stratum.cli calibrate --suite $(SUITE)

mcp:                        ## run the MCP server (drive Stratum from Claude Desktop)
	cd engine && .venv/Scripts/python.exe -m stratum.cli mcp --database $(DB) --url "$(URL)"

test:                       ## engine unit tests + scorer tests + web typecheck
	cd engine && .venv/Scripts/python.exe -m pytest -q
	cd engine && .venv/Scripts/python.exe -m ruff check stratum tests
	engine/.venv/Scripts/python.exe -m pytest gauntlet/tests -q
	@if [ -d web/node_modules ]; then cd web && npm run typecheck && npm run lint; fi

lint:                       ## lint the engine
	cd engine && .venv/Scripts/python.exe -m ruff check stratum tests ../gauntlet

fmt:                        ## format the engine
	cd engine && .venv/Scripts/python.exe -m ruff format stratum tests ../gauntlet
	cd engine && .venv/Scripts/python.exe -m ruff check --fix stratum tests ../gauntlet

clean:                      ## remove build artefacts (not the downloaded sources)
	rm -rf gauntlet/build engine/.stratum gauntlet/reports/*.json gauntlet/reports/*.md
