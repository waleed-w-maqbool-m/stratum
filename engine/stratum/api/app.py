"""The FastAPI application (PLAN.md §6).

Two kinds of endpoint: ordinary REST for things that return, and SSE for the two long
operations — excavation and answering — because both are worth watching rather than
waiting for. The trace events the UI renders are the same ones the eval harness reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..ask.cache import SemanticCache
from ..config import settings
from ..connectors.costguard import CostGuard
from ..connectors.executor import ReadOnlyExecutor
from ..connectors.registry import build, describe_url, test_connection
from ..llm.client import LLMClient
from ..semantic.store import ModelStore, build_review_queue
from ..semantic.verified import VerifiedLibrary, VerifiedQuery

router = APIRouter()

# `no-transform` tells any proxy in between not to compress the stream. Next's dev
# server gzipped it, which buffers every event until the last one, so a pipeline trace
# meant to fill in stage by stage arrived all at once at the end.
_STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


# ------------------------------------------------------------------ in-memory state
class Registry:
    """Open connections and their assembled contexts, keyed by database id.

    Deliberately in-process: a connection is a live socket, and the app is a local
    single-user tool. Restarting the engine re-opens from the stored connection list.
    """

    def __init__(self) -> None:
        self.connections: dict[str, dict[str, Any]] = {}
        self.store = ModelStore(settings().models_dir)
        self.runs: dict[str, Any] = {}

    # The connection list survives a restart; the sockets do not. URLs are stored
    # encrypted with the Fernet key from the environment (PLAN.md 4.1), and a stored
    # connection is only reopened when something asks for it — so one database that
    # is down cannot stall the engine starting up.
    @property
    def _saved_path(self) -> Path:
        return settings().ensure_home() / "connections.json"

    def saved(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._saved_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, database_id: str, url: str, profile: str | None) -> None:
        from ..config import encrypt

        entries = self.saved()
        entries[database_id] = {
            "url": encrypt(url),
            "profile": profile,
            "dialect": url.split(":", 1)[0].split("+", 1)[0],
        }
        self._saved_path.write_text(json.dumps(entries, indent=1), "utf-8")

    def register(self, database_id: str, url: str, profile: str | None = None) -> dict:
        connector = build(url, profile)
        connector.connect()
        info = connector.server_info()
        entry = {
            "id": database_id,
            "url": url,
            "connector": connector,
            "executor": ReadOnlyExecutor(connector, connector.profile),
            "cost_guard": CostGuard(connector),
            "dialect": info.dialect,
            "profile": info.profile.name,
            "version": info.version,
            "database": info.database,
            "privileges": {
                "can_write": info.privileges.can_write,
                "username": info.privileges.username,
                "notes": info.privileges.notes,
            },
        }
        self.connections[database_id] = entry
        self._save(database_id, url, profile)
        return entry

    def get(self, database_id: str) -> dict:
        entry = self.connections.get(database_id)
        if entry is not None:
            return entry
        stored = self.saved().get(database_id)
        if stored is None:
            raise HTTPException(404, f"database {database_id!r} is not connected")
        from ..config import decrypt

        try:
            return self.register(database_id, decrypt(stored["url"]), stored.get("profile"))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a 503
            raise HTTPException(
                503, f"{database_id} is saved but could not be reopened: {exc}"
            ) from exc

    def context(self, database_id: str):
        from ..ask.pipeline import AskContext

        entry = self.get(database_id)
        if "context" not in entry:
            model = self.store.load(database_id)
            if model is None:
                raise HTTPException(
                    409,
                    f"{database_id} has not been excavated yet — "
                    f"POST /databases/{database_id}/excavate first",
                )
            entry["context"] = AskContext(
                model=model,
                executor=entry["executor"],
                llm=LLMClient(cache_dir=self.store.cache_dir(database_id)),
                cost_guard=entry["cost_guard"],
                verified=VerifiedLibrary(self.store.verified_path(database_id)),
                cache=SemanticCache(self.store.cache_path(database_id)),
            )
        return entry["context"]

    def forget(self, database_id: str) -> bool:
        """Close and forget a connection. Its models and review history stay on disk,
        so connecting again with the same id picks up where it left off."""
        entry = self.connections.pop(database_id, None)
        if entry is not None:
            # Closing a socket that already died is not an error worth reporting.
            with contextlib.suppress(Exception):
                entry["connector"].close()
        entries = self.saved()
        found = entries.pop(database_id, None) is not None
        if found:
            self._saved_path.write_text(json.dumps(entries, indent=1), "utf-8")
        return found or entry is not None

    def invalidate(self, database_id: str) -> None:
        entry = self.connections.get(database_id)
        if entry is not None:
            entry.pop("context", None)


registry = Registry()


# ----------------------------------------------------------------------- schemas
class ConnectionRequest(BaseModel):
    url: str
    id: str | None = None
    profile: str | None = None


class AskRequest(BaseModel):
    database_id: str
    question: str
    mode: str = "default"
    clarification: str = ""
    approved_entities: list[str] | None = None
    allow_expensive: bool = False


class ModelItemPatch(BaseModel):
    action: str                      # accept | reject | edit
    value: Any = None


class VerifiedRequest(BaseModel):
    database_id: str
    question: str
    logical_sql: str
    entities: list[str] = []
    verified_by: str = ""


class FeedbackRequest(BaseModel):
    verdict: str                     # looks_right | wrong
    edited_sql: str = ""
    note: str = ""


# ------------------------------------------------------------------- connections
@router.get("/health")
def health() -> dict[str, Any]:
    import sqlite3
    import sys

    servers: dict[str, str] = {"sqlite": sqlite3.sqlite_version}
    for entry in registry.connections.values():
        servers[entry["dialect"]] = entry["version"]
    return {
        "ok": True,
        "python": sys.version.split()[0],
        "servers": servers,
        "connections": len(registry.connections),
        "profiles_loaded": True,
    }


@router.post("/connections/test")
def connections_test(request: ConnectionRequest) -> dict[str, Any]:
    result = test_connection(request.url, request.profile)
    return {**result.to_dict(), "parsed": describe_url(request.url)}


@router.post("/connections")
def connections_create(request: ConnectionRequest) -> dict[str, Any]:
    database_id = request.id or Path(describe_url(request.url).get("database", "db")).stem
    entry = registry.register(database_id, request.url, request.profile)
    return {
        "id": entry["id"],
        "dialect": entry["dialect"],
        "profile": entry["profile"],
        "version": entry["version"],
        "privileges": entry["privileges"],
        "excavated": registry.store.latest_version(database_id) > 0,
    }


@router.get("/demos")
def demos() -> list[dict[str, Any]]:
    """The benchmark databases built on this machine, ready to connect in one click.

    Resolved here rather than hard-coded in the web app: the SQLite path depends on
    where the repository lives, and the local Postgres picks its port when it starts.
    A demo that has not been built is listed as unavailable with the command that
    builds it, instead of a URL that would fail.
    """
    root = Path(__file__).resolve().parents[3]
    build = root / "gauntlet" / "build"
    out: list[dict[str, Any]] = []

    chinook = build / "chinook_legacy.sqlite"
    out.append({
        "id": "chinook_legacy",
        "name": "Chinook",
        "engine": "SQLite",
        "description": (
            "A music store, put through fourteen kinds of legacy mess: cryptic names, no "
            "foreign keys, a 53-column table holding three things, money that switches "
            "from dollars to cents in 2023."
        ),
        "url": f"sqlite:///{chinook.as_posix()}" if chinook.exists() else None,
        "build": "make chinook",
    })

    pagila_url = None
    if (build / "pagila_legacy.manifest.json").exists():
        try:
            import sys

            sys.path.insert(0, str(root))
            from gauntlet import pg

            pagila_url = pg.sqlalchemy_url("pagila_legacy")
        except Exception:  # noqa: BLE001 - reported as unavailable, not raised
            pagila_url = None
    out.append({
        "id": "pagila_legacy",
        "name": "Pagila",
        "engine": "Postgres 16",
        "description": (
            "A film-rental business on real Postgres: a vendor table with quoted "
            "mixed-case names, payments and refunds in one table, and a column comment "
            "that is simply wrong."
        ),
        "url": pagila_url,
        "build": "make pagila",
    })
    return out


@router.delete("/connections/{database_id}")
def connections_forget(database_id: str) -> dict[str, Any]:
    if not registry.forget(database_id):
        raise HTTPException(404, f"no connection {database_id!r}")
    return {"ok": True, "id": database_id}


@router.get("/connections")
def connections_list() -> list[dict[str, Any]]:
    """Open connections, and saved ones not reopened since the engine started."""
    out = [
        {
            "id": e["id"], "dialect": e["dialect"], "profile": e["profile"],
            "database": e["database"],
            "excavated": registry.store.latest_version(e["id"]) > 0,
        }
        for e in registry.connections.values()
    ]
    for database_id, stored in registry.saved().items():
        if database_id not in registry.connections:
            out.append({
                "id": database_id, "dialect": stored.get("dialect", ""),
                "profile": stored.get("profile") or "", "database": database_id,
                "excavated": registry.store.latest_version(database_id) > 0,
            })
    return out


# -------------------------------------------------------------------- excavation
def _carry_decisions(model, findings: list[dict], previous: list[dict]) -> int:
    """Re-apply earlier review decisions to a fresh excavation. Returns how many."""
    from .review import apply_review_action

    decided = {
        f["id"]: f for f in previous if f.get("status") in ("accepted", "rejected")
    }
    kept = 0
    for finding in findings:
        prior = decided.get(finding["id"])
        # Some findings arrive already accepted (a declared key); only a decision that
        # differs from what this run concluded is one a person made.
        if prior is None or (
            prior["status"] == finding.get("status") and prior.get("value") == finding.get("value")
        ):
            continue
        action = "accept" if prior["status"] == "accepted" else "reject"
        if action == "accept" and prior.get("value") != finding.get("value"):
            action, value = "edit", prior.get("value")
        else:
            value = None
        apply_review_action(model, findings, finding["id"], action, value)
        kept += 1
    return kept


@router.post("/databases/{database_id}/excavate")
async def excavate_stream(database_id: str) -> EventSourceResponse:
    """Run excavation, streaming each stage as it completes."""
    entry = registry.get(database_id)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait, {"event": "stage.end", "data": json.dumps(event.to_dict())}
        )

    async def run() -> None:
        from ..excavate.compile_model import compile_model
        from ..excavate.pipeline import excavate

        try:
            llm = LLMClient(cache_dir=registry.store.cache_dir(database_id))
            result = await asyncio.to_thread(
                excavate, entry["connector"], emit=emit, llm=llm
            )
            model = compile_model(
                result, database=database_id, profile_name=entry["profile"]
            )
            findings = [f.to_dict() for f in result.findings]
            # A person's decisions outlive a re-excavation: every finding the new run
            # reproduces is decided again the way it was decided before.
            carried = _carry_decisions(
                model, findings, registry.store.load_findings(database_id)
            )
            version = registry.store.save(model)
            registry.store.save_findings(database_id, findings)
            registry.invalidate(database_id)
            await queue.put({
                "event": "done",
                "data": json.dumps({
                    "version": version,
                    "summary": result.summary(),
                    "model": model.stats(),
                    "decisions_kept": carried,
                }),
            })
        except Exception as exc:
            await queue.put({"event": "error", "data": json.dumps({"message": str(exc)})})
        finally:
            await queue.put(None)

    asyncio.create_task(run())

    async def stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return EventSourceResponse(stream(), headers=_STREAM_HEADERS)


@router.get("/databases/{database_id}/model")
def get_model(database_id: str, version: int | None = None) -> dict[str, Any]:
    model = registry.store.load(database_id, version)
    if model is None:
        raise HTTPException(404, "no semantic model — run excavation first")
    return json.loads(model.model_dump_json())


@router.get("/databases/{database_id}/model.yaml")
def get_model_yaml(database_id: str) -> dict[str, str]:
    model = registry.store.load(database_id)
    if model is None:
        raise HTTPException(404, "no semantic model — run excavation first")
    return {"yaml": model.to_yaml()}


@router.get("/databases/{database_id}/review")
def get_review(database_id: str, limit: int = 100) -> list[dict[str, Any]]:
    model = registry.store.load(database_id)
    if model is None:
        raise HTTPException(404, "no semantic model — run excavation first")
    findings = registry.store.load_findings(database_id)
    return [item.to_dict() for item in build_review_queue(findings, model, limit)]


@router.patch("/databases/{database_id}/model/items/{item_id}")
def patch_model_item(
    database_id: str, item_id: str, patch: ModelItemPatch
) -> dict[str, Any]:
    """Accept, reject or edit one hypothesis, producing a new model version."""
    from .review import apply_review_action

    model = registry.store.load(database_id)
    if model is None:
        raise HTTPException(404, "no semantic model — run excavation first")
    findings = registry.store.load_findings(database_id)
    changed = apply_review_action(model, findings, item_id, patch.action, patch.value)
    if not changed:
        raise HTTPException(404, f"review item {item_id!r} not found")
    version = registry.store.save(model)
    registry.store.save_findings(database_id, findings)
    registry.invalidate(database_id)
    return {"ok": True, "version": version, "item": item_id, "action": patch.action}


# --------------------------------------------------------------------------- ask
@router.post("/ask")
async def ask_stream(request: AskRequest) -> EventSourceResponse:
    from ..ask.pipeline import ask

    context = registry.context(request.database_id)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"event": "stage.end", "data": json.dumps(event.to_dict())},
        )

    async def run() -> None:
        try:
            answer = await asyncio.to_thread(
                ask, context, request.question,
                mode=request.mode,
                clarification=request.clarification,
                approved_entities=request.approved_entities,
                allow_expensive=request.allow_expensive,
                token_limit=settings().question_token_limit,
                emit=emit,
            )
            payload = answer.to_dict()
            run_id = f"{request.database_id}:{len(registry.runs)}"
            registry.runs[run_id] = answer
            payload["run_id"] = run_id

            if answer.status == "cost_guard":
                await queue.put({
                    "event": "cost.warning",
                    "data": json.dumps(answer.cost_warning or {}),
                })
            if answer.clarification:
                await queue.put({
                    "event": "clarification.needed",
                    "data": json.dumps(answer.clarification),
                })
            if answer.linking and answer.linking.needs_confirmation:
                await queue.put({
                    "event": "tables.proposed",
                    "data": json.dumps(answer.linking.to_dict()),
                })
            if answer.physical_sql:
                await queue.put({
                    "event": "sql.candidate",
                    "data": json.dumps({
                        "logical": answer.logical_sql,
                        "physical": answer.physical_sql,
                    }),
                })
            if answer.rows:
                await queue.put({
                    "event": "result",
                    "data": json.dumps({
                        "columns": answer.columns,
                        "rows": [list(r) for r in answer.rows[:200]],
                        "truncated": answer.truncated,
                        "row_count": answer.row_count,
                    }, default=str),
                })
            if answer.confidence:
                await queue.put({
                    "event": "confidence",
                    "data": json.dumps(answer.confidence.to_dict()),
                })
            await queue.put({
                "event": "explanation",
                "data": json.dumps({
                    "text": answer.explanation, "assumptions": answer.assumptions,
                }),
            })
            await queue.put({"event": "done", "data": json.dumps(payload, default=str)})
        except Exception as exc:
            await queue.put({"event": "error", "data": json.dumps({"message": str(exc)})})
        finally:
            await queue.put(None)

    asyncio.create_task(run())

    async def stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return EventSourceResponse(stream(), headers=_STREAM_HEADERS)


@router.post("/ask/{run_id}/feedback")
def ask_feedback(run_id: str, feedback: FeedbackRequest) -> dict[str, Any]:
    answer = registry.runs.get(run_id)
    if answer is None:
        raise HTTPException(404, "unknown run")
    database_id = run_id.split(":")[0]
    library = VerifiedLibrary(registry.store.verified_path(database_id))
    if feedback.verdict == "looks_right" and answer.logical_sql:
        proposal = library.suggest_from_feedback(
            answer.question, answer.logical_sql, answer.entities
        )
        return {
            "ok": True,
            "proposed_verified_query": proposal.to_dict(),
            "message": "Confirm this to add it to the verified library.",
        }
    return {"ok": True, "recorded": feedback.verdict}


# --------------------------------------------------------------- verified library
@router.get("/databases/{database_id}/verified")
def verified_list(database_id: str) -> list[dict[str, Any]]:
    library = VerifiedLibrary(registry.store.verified_path(database_id))
    return [entry.to_dict() for entry in library.entries]


@router.post("/verified")
def verified_add(request: VerifiedRequest) -> dict[str, Any]:
    library = VerifiedLibrary(registry.store.verified_path(request.database_id))
    entry = library.add(
        VerifiedQuery(
            question=request.question,
            logical_sql=request.logical_sql,
            entities=request.entities,
            verified_by=request.verified_by or "unknown",
        )
    )
    registry.invalidate(request.database_id)
    return entry.to_dict()


@router.get("/databases/{database_id}/atlas")
def atlas(database_id: str) -> dict[str, Any]:
    """Nodes and edges for the Atlas.

    Everything the 3D view needs in one request, including the tables excavation
    *excluded*: seeing 117 dead tables sink away from the 17 live ones is the point of
    the view, so they have to be in the payload even though no query will touch them.

    Positions are computed here rather than in the browser. A force layout over 134
    nodes is not expensive, but doing it server-side means the layout is stable across
    reloads — a map that rearranges itself every time you open it is not a map.
    """
    model = registry.store.load(database_id)
    if model is None:
        raise HTTPException(404, "no semantic model — run excavation first")

    import math

    # Group by inferred domain: entities that join to each other belong together.
    adjacency: dict[str, set[str]] = {}
    for relationship in model.relationships:
        if relationship.status == "rejected":
            continue
        a, b = relationship.from_table, relationship.to_table
        if not a or not b:
            continue
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    # Connected components of the join graph are the domains.
    domains: dict[str, int] = {}
    seen: set[str] = set()
    domain_id = 0
    for table in adjacency:
        if table in seen:
            continue
        stack, members = [table], []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            members.append(current)
            stack.extend(adjacency.get(current, ()))
        for member in members:
            domains[member] = domain_id
        domain_id += 1

    entity_by_table = {
        e.source: e for e in model.accepted_entities()
    }

    nodes: list[dict[str, Any]] = []
    for index, (table, klass) in enumerate(sorted(model.table_classes.items())):
        entity = entity_by_table.get(table)
        domain = domains.get(table, -1)
        rows = entity.row_estimate if entity else 0
        # Live tables sit in a ring per domain; everything excluded sinks below.
        if klass == "core" and domain >= 0:
            angle = (index * 2.399963)          # golden angle, so rings do not align
            radius = 6 + domain * 5.5
            position = [
                math.cos(angle + domain) * radius,
                math.log10(max(10, rows)) - 1.5,
                math.sin(angle + domain) * radius,
            ]
        else:
            angle = index * 2.399963
            radius = 14 + (index % 7)
            position = [math.cos(angle) * radius, -6 - (index % 5) * 0.8,
                        math.sin(angle) * radius]

        nodes.append({
            "id": table,
            "entity": entity.name if entity else None,
            "class": klass,
            "rows": rows,
            "columns": len(entity.columns) if entity else 0,
            "domain": domain,
            "position": [round(v, 3) for v in position],
            "filters": len(entity.active_filters()) if entity else 0,
            "unreviewed": sum(
                1 for c in entity.columns.values() if c.status == "hypothesis"
            ) if entity else 0,
        })

    edges = [
        {
            "from": r.from_table,
            "to": r.to_table,
            "confidence": round(r.confidence, 3),
            "on": r.logical_on or r.on,
            "conditional": bool(r.condition),
            "inferred": (r.provenance.detector if r.provenance else "") != "procmine",
        }
        for r in model.relationships
        if r.status != "rejected" and r.from_table and r.to_table
    ]

    return {
        "database": database_id,
        "version": model.version,
        "nodes": nodes,
        "edges": edges,
        "domains": domain_id,
        "summary": {
            "core": sum(1 for n in nodes if n["class"] == "core"),
            "excluded": sum(1 for n in nodes if n["class"] != "core"),
            "by_class": {
                klass: sum(1 for n in nodes if n["class"] == klass)
                for klass in sorted({n["class"] for n in nodes})
            },
        },
    }


# ---------------------------------------------------------------------------- eval
@router.get("/eval/reports")
def eval_reports() -> list[dict[str, Any]]:
    """Every benchmark report the harness has written.

    Read off disk rather than kept in memory: an eval run can take hours and may be
    spread across days on a free tier, so the reports outlive any one engine process.
    """
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(reports_dir.glob("*.json")):
        # A run summary is exactly `<suite>.<tag>.json`. The calibration, per-question
        # and recall files live alongside it and are not summaries — reading them as
        # ones is what put two NaN% cards on the benchmark page.
        match = _SUMMARY.match(path.name)
        if not match:
            continue
        try:
            report = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        if "execution_accuracy" not in report:
            continue
        report["tag"] = match.group("tag")
        report["has_questions"] = (
            reports_dir / f"{match.group('suite')}.{match.group('tag')}.questions.json"
        ).exists()
        out.append(report)
    # Full runs first, then baselines and ablations: the comparison reads better that
    # way, and the first report is the one the summary cards lead with.
    out.sort(key=lambda r: (r.get("suite", ""), r.get("mode") != "full", r.get("tag", "")))
    return out


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_+-]+$")
_SUMMARY = re.compile(r"^(?P<suite>[A-Za-z0-9_-]+)\.(?P<tag>[A-Za-z0-9_+-]+)\.json$")


def _reports_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "gauntlet" / "reports"


def _published(name: str) -> Any:
    path = _reports_dir() / name
    if not path.exists():
        raise HTTPException(404, f"{name} has not been published — run `python -m gauntlet.publish`")
    return json.loads(path.read_text("utf-8"))


@router.get("/eval/questions/{suite}/{tag}")
def eval_questions(suite: str, tag: str) -> Any:
    """Every question of one run: outcome, the gold answer and ours.

    Written by `gauntlet.publish`, which is allowed to run gold SQL; served as is, so the
    engine never touches the answers it is being graded against.
    """
    if not (_SAFE_NAME.match(suite) and _SAFE_NAME.match(tag)):
        raise HTTPException(400, "suite and tag are plain names")
    return _published(f"{suite}.{tag}.questions.json")


@router.get("/eval/recall/{suite}")
def eval_recall(suite: str) -> Any:
    """How much of the benchmark's answer key excavation recovered, scored by the
    gauntlet — the engine never opens the manifest itself."""
    if not _SAFE_NAME.match(suite):
        raise HTTPException(400, "suite is a plain name")
    return _published(f"{suite}.recall.json")


@router.get("/eval/suites")
def eval_suites() -> list[dict[str, Any]]:
    """The question suites available to run."""
    import yaml

    suites_dir = Path(__file__).resolve().parents[3] / "gauntlet" / "suites"
    out: list[dict[str, Any]] = []
    if not suites_dir.exists():
        return out
    for path in sorted(suites_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text("utf-8"))
        except (ValueError, OSError):
            continue
        questions = raw.get("questions", [])
        out.append({
            "suite": raw.get("suite", path.stem),
            "database": raw.get("database", ""),
            "questions": len(questions),
            "ambiguous": sum(1 for q in questions if q.get("ambiguity")),
            "behavioural": sum(1 for q in questions if q.get("expect")),
        })
    return out


def create_app() -> FastAPI:
    config = settings()
    config.ensure_home()
    app = FastAPI(
        title="Stratum",
        description="Natural language to SQL for legacy databases.",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
