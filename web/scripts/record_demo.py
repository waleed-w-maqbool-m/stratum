"""Record the public demo's data from a running engine.

The static site (`npm run build:demo`) has no engine behind it. Everything it shows is
recorded here from a real one: each excavation and each answer is captured event by
event with the time it arrived, so the site can replay it at the same pace, and the
review queue, atlas and benchmark results are saved as the engine served them.

Run it against an engine that does not replay cached LLM completions, so every
recorded trace is a real run with real token counts:

    cd engine && STRATUM_LLM_REPLAY=0 .venv/Scripts/python.exe -m stratum.cli serve
    engine/.venv/Scripts/python.exe web/scripts/record_demo.py

It re-excavates both demo databases, which writes a new model version for each.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parents[1] / "public" / "demo"

# The questions the demo can answer. These are also the suggestions the Ask page
# offers, so a visitor is never shown a question the demo cannot replay.
QUESTIONS: dict[str, list[str]] = {
    "pagila_legacy": [
        "What is the total revenue from all payments?",
        "How many rentals have been made in total?",
        "How many films are rated PG-13?",
        "Who are our best customers?",
        "How many customers do we have?",
        "How many events were logged in March 2022?",
    ],
    "chinook_legacy": [
        "What is our total revenue across all invoices?",
        "How many customers do we have?",
        "Which billing country generated the most revenue?",
        "Show total revenue per year.",
        "Who are our best customers?",
        "Which genre sells the most tracks?",
    ],
}

client = httpx.Client(base_url=BASE, timeout=httpx.Timeout(600.0))


def save(relative: str, data: Any) -> None:
    path = OUT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"  saved {relative} ({path.stat().st_size / 1024:.0f} KB)")


def get(path: str) -> Any:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def record_stream(path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every server-sent event, parsed, with milliseconds since the request began."""
    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    buffer = ""
    with client.stream("POST", path, json=body) as response:
        response.raise_for_status()
        for chunk in response.iter_text():
            buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                name, data = "message", []
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                if data:
                    events.append({
                        "event": name,
                        "data": json.loads("\n".join(data)),
                        "at": int((time.perf_counter() - started) * 1000),
                    })
    return events


def final(events: list[dict[str, Any]]) -> dict[str, Any]:
    last = events[-1] if events else {"event": "error", "data": {"message": "no events"}}
    return last["data"] if last["event"] == "done" else {"status": "error", **last["data"]}


def ask(database: str, question: str, **extra: Any) -> dict[str, Any]:
    """One recorded answer, retried when the free tier's rate limit is what failed."""
    for attempt in range(4):
        events = record_stream("/ask", {"database_id": database, "question": question, **extra})
        answer = final(events)
        message = str(answer.get("message", ""))
        limited = answer.get("status") == "error" and re.search(
            r"rate|limit|quota|budget|429|unavailable", message, re.IGNORECASE
        )
        if not limited:
            label = answer.get("status")
            print(f"    {label:<10} {question}" + (f"  [{extra}]" if extra else ""))
            return {"question": question, **extra, "events": events}
        wait = 60 * (attempt + 1)
        print(f"    rate limited, waiting {wait}s: {message[:80]}")
        time.sleep(wait)
    raise SystemExit(f"gave up on {question!r}: {message}")


def follow(database: str, question: str, recordings: list[dict[str, Any]],
           **extra: Any) -> None:
    """Record one answer, then every branch a visitor can click from it, so none of them
    dead-ends: each clarification option, and running past a cost warning (which keeps
    the clarification chosen, as the Ask page does)."""
    recording = ask(database, question, **extra)
    recordings.append(recording)
    answer = final(recording["events"])
    if answer.get("status") == "clarify" and "clarification" not in extra:
        for option in (answer.get("clarification") or {}).get("options", []):
            follow(database, question, recordings, **extra,
                   clarification=option["interpretation"])
    elif answer.get("status") == "cost_guard" and not extra.get("allow_expensive"):
        follow(database, question, recordings, **extra, allow_expensive=True)


def record_database(database: str, *, excavate: bool = True) -> None:
    """Excavate (unless told not to: `--keep-model` records against the model already
    there), then record the review queue, the atlas and every answer."""
    print(f"\n{database}")
    # Connect through /demos, which knows where each demo database lives right now: the
    # local Postgres picks a new port each time it starts, so a saved URL goes stale.
    demo = next(d for d in get("/demos") if d["id"] == database)
    client.post("/connections", json={"url": demo["url"], "id": database}).raise_for_status()
    if excavate:
        events = record_stream(f"/databases/{database}/excavate", {})
        summary = final(events)
        if summary.get("status") == "error":
            raise SystemExit(f"excavation failed: {summary.get('message')}")
        save(f"databases/{database}/excavate.json", events)
    save(f"databases/{database}/review.json", get(f"/databases/{database}/review?limit=500"))
    save(f"databases/{database}/atlas.json", get(f"/databases/{database}/atlas"))

    recordings: list[dict[str, Any]] = []
    for question in QUESTIONS[database]:
        follow(database, question, recordings)
    save(f"databases/{database}/ask.json", recordings)


def record_benchmark() -> None:
    print("\nbenchmark")
    reports = get("/eval/reports")
    save("eval/reports.json", reports)
    for report in reports:
        if report.get("has_questions"):
            suite, tag = report["suite"], report["tag"]
            save(f"eval/questions/{suite}/{tag}.json", get(f"/eval/questions/{suite}/{tag}"))
    for suite in sorted({r["suite"] for r in reports}):
        try:
            save(f"eval/recall/{suite}.json", get(f"/eval/recall/{suite}"))
        except httpx.HTTPStatusError:
            print(f"  no recall published for {suite}")


def record_connections() -> None:
    connections = get("/connections")
    save("connections.json", [
        {k: c[k] for k in ("id", "dialect", "profile", "database", "excavated") if k in c}
        for c in connections
    ])
    # A demo's URL is a path on this machine; the static site connects by id instead.
    demos = [{**d, "url": f"demo:{d['id']}"} for d in get("/demos") if d.get("url")]
    save("demos.json", demos)


LEAKS = re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:/Users|/home/|AppData|gsk_|AIza|sk-[A-Za-z0-9]{16}")


def check_for_leaks() -> None:
    """Nothing from this machine, and no key, may end up on a public site."""
    bad = [p for p in OUT.rglob("*.json") if LEAKS.search(p.read_text("utf-8"))]
    if bad:
        raise SystemExit("local paths or keys in: " + ", ".join(str(p) for p in bad))
    print("\nno local paths or keys in the recorded data")


def main() -> int:
    get("/health")
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if flags - {"--keep-model"}:
        raise SystemExit(f"unknown option: {', '.join(sorted(flags - {'--keep-model'}))}")
    only = [a for a in sys.argv[1:] if not a.startswith("--")] or list(QUESTIONS)
    unknown = [name for name in only if name not in QUESTIONS]
    if unknown:
        raise SystemExit(f"no questions defined for: {', '.join(unknown)}")
    for database in only:
        record_database(database, excavate="--keep-model" not in flags)
    record_connections()
    record_benchmark()
    check_for_leaks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
