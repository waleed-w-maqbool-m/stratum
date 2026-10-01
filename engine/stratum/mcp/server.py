"""MCP server — Claude as the brain, with no API key (PLAN.md 4.7).

A website cannot call Claude through a subscription, but MCP is the sanctioned way for
Claude to use local tools. So the engine exposes itself as an MCP server: Claude Desktop
or Claude Code does the reasoning under the user's own plan, and Stratum does everything
deterministic and everything dangerous.

The division of labour is the whole point, and it is the same one the web app uses:

* **Claude decides** which entities a question needs and writes SQL against the logical
  schema.
* **Stratum guarantees** that the SQL is read-only, that it compiles to correct physical
  SQL for this dialect, that the soft-delete and discriminator filters are applied, that
  the joins are the ones the excavation actually verified, and that an expensive query
  is refused rather than run.

`compile_logical_sql` is therefore the important tool. Claude never writes physical SQL
and never sees a cryptic column name, which is exactly the arrangement that makes the
answers correct — the same bet the rest of the system makes, with a different model
holding the pen.

Run it with:

    stratum mcp --database chinook_legacy --url sqlite:///gauntlet/build/chinook_legacy.sqlite

and register it in Claude Desktop's config (see `docs/MCP.md`).
"""

from __future__ import annotations

import json
from typing import Any

from ..ask.compiler import compile_logical
from ..ask.joinplan import build_graph
from ..ask.joinplan import plan as plan_join_path
from ..ask.linking import SchemaIndex
from ..ask.validate import validate
from ..config import settings
from ..connectors.costguard import CostGuard
from ..connectors.executor import ReadOnlyExecutor, ReadOnlyViolation
from ..connectors.registry import build
from ..semantic.store import ModelStore, build_review_queue


class Session:
    """One database, opened once and reused across tool calls."""

    def __init__(self, database: str, url: str):
        self.database = database
        self.url = url
        self.store = ModelStore(settings().models_dir)
        self.connector = build(url)
        self.connector.connect()
        self.executor = ReadOnlyExecutor(self.connector, self.connector.profile)
        self.cost_guard = CostGuard(self.connector)
        self.model = self.store.load(database)
        self._index: SchemaIndex | None = None
        self._graph: Any = None

    @property
    def index(self) -> SchemaIndex:
        if self._index is None:
            self._index = SchemaIndex(self.require_model())
        return self._index

    @property
    def graph(self):
        if self._graph is None:
            self._graph = build_graph(self.require_model())
        return self._graph

    def require_model(self):
        if self.model is None:
            raise RuntimeError(
                f"{self.database} has not been excavated yet. Run "
                f"`stratum excavate {self.url} --name {self.database}` first."
            )
        return self.model


def build_server(session: Session):
    """Assemble the MCP server and its tools."""
    # The SDK renamed FastMCP to MCPServer in 2.0. Both spellings expose the same
    # decorator API, so support whichever is installed rather than pinning the user to
    # a version of a dependency they may already have.
    server_class = None
    try:
        from mcp.server.mcpserver import MCPServer as server_class  # noqa: N813
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as server_class  # noqa: N813
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "the MCP SDK is not installed. Install the extra: uv sync --extra mcp"
            ) from exc

    server = server_class("stratum")

    # ------------------------------------------------------------------ discovery
    @server.tool()
    def list_databases() -> str:
        """List the databases Stratum has excavated, with what each contains."""
        root = settings().models_dir
        out: list[dict[str, Any]] = []
        if root.exists():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                model = ModelStore(root).load(directory.name)
                if model is None:
                    continue
                out.append({
                    "database": directory.name,
                    "dialect": model.dialect,
                    "profile": model.profile,
                    "version": model.version,
                    "entities": sorted(model.entities),
                    "connected": directory.name == session.database,
                })
        return json.dumps(out, indent=2)

    @server.tool()
    def search_semantic_model(question: str, limit: int = 7) -> str:
        """Find the entities and columns relevant to a question.

        Use this first. It returns the logical schema for the relevant entities only,
        the join conditions between them, and how confident the retrieval was — which
        is far smaller than the whole schema and is the part you need.
        """
        model = session.require_model()
        linked = session.index.search(question, limit=limit)
        if not linked.entities:
            return json.dumps({
                "entities": [],
                "note": (
                    "Nothing matched. The model covers: "
                    + ", ".join(sorted(model.entities))
                ),
            })

        from ..ask.compiler import render_logical_schema, render_relationships

        names = linked.names
        return json.dumps({
            "entities": names,
            "schema": render_logical_schema(model, names),
            "joins": render_relationships(model, names),
            "retrieval_margin": round(linked.margin, 3),
            "uncertain": linked.needs_confirmation,
            "note": (
                "Write SQL against these logical names. Do not add casts, soft-delete "
                "filters or status exclusions — compile_logical_sql applies those."
            ),
        }, indent=2)

    @server.tool()
    def describe_entity(entity: str) -> str:
        """The full logical definition of one entity: columns, key, and default filters.

        The `default_filters` are applied automatically on compilation. They are shown
        so you know what is being excluded, not so you can write them yourself.
        """
        model = session.require_model()
        found = model.entity(entity)
        if found is None:
            return json.dumps({
                "error": f"no entity {entity!r}",
                "available": sorted(model.entities),
            })
        return json.dumps({
            "name": found.name,
            "physical_table": found.source,
            "description": found.description,
            "key": found.key,
            "row_estimate": found.row_estimate,
            "default_filters": [
                {
                    "expr": f.expr, "reason": f.reason, "kind": f.kind,
                    "status": f.status, "overridable": f.overridable,
                }
                for f in found.active_filters()
            ],
            "columns": {
                name: {
                    "type": column.data_type,
                    "description": column.description,
                    "enum": column.enum,
                    "status": column.status,
                    "note": column.note,
                }
                for name, column in found.columns.items()
                if column.status != "rejected"
            },
        }, indent=2, default=str)

    @server.tool()
    def sample_values(entity: str, column: str, limit: int = 15) -> str:
        """The distinct values stored in a logical column.

        Use this before writing a filter on a text column: the database may spell the
        United States four different ways, and an equality test would find one of them.
        """
        model = session.require_model()
        found = model.entity(entity)
        if found is None or found.column(column) is None:
            return json.dumps({"error": f"no column {entity}.{column}"})

        logical = f"SELECT {entity}.{column} AS value FROM {entity}"
        try:
            compiled = compile_logical(logical, model, session.connector.profile)
            sql = (
                f"SELECT value, COUNT(*) AS n FROM ({compiled.physical_sql}) AS _v "
                f"WHERE value IS NOT NULL GROUP BY value ORDER BY n DESC"
            )
            result = session.executor.run(sql, row_cap=limit)
        except Exception as exc:
            return json.dumps({"error": str(exc)[:300]})
        return json.dumps({
            "entity": entity,
            "column": column,
            "values": [{"value": row[0], "rows": row[1]} for row in result.rows],
        }, indent=2, default=str)

    @server.tool()
    def plan_joins(entities: list[str]) -> str:
        """The verified join path connecting these entities.

        Use the conditions this returns verbatim. They came from inclusion testing
        against the real data and from SQL already running in production; a join you
        invent will silently return the wrong rows, because none of the foreign keys in
        this database are declared.
        """
        model = session.require_model()
        result = plan_join_path(model, entities, graph=session.graph)
        return json.dumps({
            **result.to_dict(),
            "from_clause": result.render(),
            "fan_out_warning": (
                f"joining {', '.join(result.fan_out_risk)} multiplies rows — do not SUM "
                "a parent measure across it"
                if result.fan_out_risk else ""
            ),
        }, indent=2)

    # ------------------------------------------------------------------ execution
    @server.tool()
    def compile_logical_sql(logical_sql: str) -> str:
        """Compile SQL written against the logical schema into physical SQL.

        This is the tool that makes the answers correct. It expands each entity into a
        derived table that applies its soft-delete filters, god-table discriminators,
        type normalisations and drift resolutions, then rewrites the result for this
        connection's SQL dialect.

        It also reports every assumption applied, which you should repeat to the user.
        """
        model = session.require_model()
        try:
            compiled = compile_logical(logical_sql, model, session.connector.profile)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)[:500]})
        return json.dumps({
            "ok": compiled.ok,
            "physical_sql": compiled.physical_sql,
            "entities": compiled.entities_used,
            "assumptions": [a.to_dict() for a in compiled.assumptions],
            "dialect_rewrites": compiled.downgrades,
            "unresolved": compiled.unresolved,
            "warnings": compiled.warnings,
        }, indent=2)

    @server.tool()
    def validate_sql(logical_sql: str) -> str:
        """Check compiled SQL before running it.

        Confirms it is read-only, that every name exists, that the model's required
        filters survived, that it passes this connection's dialect profile, that no
        join multiplies an aggregate, and that it will not scan a huge table.
        """
        model = session.require_model()
        try:
            compiled = compile_logical(logical_sql, model, session.connector.profile)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)[:500]})
        result = validate(compiled, model, session.executor, cost_guard=session.cost_guard)
        return json.dumps(result.to_dict(), indent=2, default=str)

    @server.tool()
    def run_sql(logical_sql: str, allow_expensive: bool = False) -> str:
        """Compile, validate and run a logical query, returning the rows.

        Refuses anything that is not a single SELECT, and refuses a query the cost guard
        says would read too much unless `allow_expensive` is set — and you should only
        set it after telling the user what it will cost.
        """
        model = session.require_model()
        try:
            compiled = compile_logical(logical_sql, model, session.connector.profile)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)[:500]})

        checks = validate(
            compiled, model, session.executor,
            cost_guard=None if allow_expensive else session.cost_guard,
        )
        if not checks.ok:
            return json.dumps({
                "ok": False,
                "blocked_by": [
                    {"check": c.name, "message": c.message, "detail": c.detail}
                    for c in checks.blocking
                ],
                "physical_sql": compiled.physical_sql,
            }, indent=2, default=str)

        try:
            result = session.executor.run(compiled.physical_sql)
        except ReadOnlyViolation as exc:
            return json.dumps({"ok": False, "error": f"refused: {exc}"})
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "error": str(exc)[:500],
                "physical_sql": compiled.physical_sql,
                "hint": "check the column names with describe_entity",
            }, indent=2)

        return json.dumps({
            "ok": True,
            "columns": result.columns,
            "rows": [list(r) for r in result.rows[:200]],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "physical_sql": compiled.physical_sql,
            "assumptions": [a.to_dict() for a in compiled.assumptions],
            "tell_the_user": (
                "Repeat the assumptions above when you give the answer. They explain "
                "why a count may be lower than expected."
            ),
        }, indent=2, default=str)

    # ------------------------------------------------------------------ the model
    @server.tool()
    def review_queue(limit: int = 15) -> str:
        """Hypotheses a human should confirm, most consequential first.

        Useful when an answer looks wrong: the mapping it depended on may be an
        unreviewed guess, and this says which.
        """
        model = session.require_model()
        findings = session.store.load_findings(session.database)
        items = build_review_queue(findings, model, limit)
        return json.dumps([item.to_dict() for item in items], indent=2, default=str)

    @server.tool()
    def propose_semantic_fix(
        target: str, kind: str, value: str, reason: str
    ) -> str:
        """Propose a correction to the semantic model.

        Writes a proposal for a human to accept in the Workbench; it does not change the
        model. `target` is an entity or `entity.column`, `kind` is one of naming, enum,
        soft_delete, join or description.
        """
        proposals = session.store._dir(session.database) / "proposals.json"
        existing: list[dict[str, Any]] = []
        if proposals.exists():
            try:
                existing = json.loads(proposals.read_text("utf-8"))
            except ValueError:
                existing = []
        entry = {
            "target": target, "kind": kind, "value": value, "reason": reason,
            "source": "mcp", "status": "proposed",
        }
        existing.append(entry)
        proposals.write_text(json.dumps(existing, indent=2), "utf-8")
        return json.dumps({
            "ok": True,
            "proposal": entry,
            "note": "recorded for human review; the model is unchanged",
        }, indent=2)

    return server


def main(database: str, url: str) -> None:
    session = Session(database, url)
    server = build_server(session)
    server.run()


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--url", required=True)
    arguments = parser.parse_args()
    main(arguments.database, arguments.url)
