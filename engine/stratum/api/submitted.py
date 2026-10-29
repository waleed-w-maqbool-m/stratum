"""The Atlas payload for a database *as submitted*: only what its own catalog says.

Drawn from the catalog snapshot saved at excavation, never from the semantic model. No
table is classified, nothing is renamed, and the only relationships are declared foreign
keys. On a legacy database that is usually none at all, which is the point: the contrast
with the refined view is what excavation added.
"""

from __future__ import annotations

import math
from typing import Any

from ..excavate.explain_relationship import describe


def _components(tables: list[str], links: list[tuple[str, str]]) -> dict[str, int]:
    adjacency: dict[str, set[str]] = {}
    for a, b in links:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    domains: dict[str, int] = {}
    next_id = 0
    for table in tables:
        if table in domains or table not in adjacency:
            continue
        stack = [table]
        while stack:
            current = stack.pop()
            if current in domains:
                continue
            domains[current] = next_id
            stack.extend(adjacency.get(current, ()))
        next_id += 1
    return domains


def submitted_atlas(database_id: str, catalog: dict[str, Any]) -> dict[str, Any]:
    tables = [t for t in catalog["tables"] if t["kind"] != "view"]
    names = [t["name"] for t in tables]
    known = set(names)
    foreign_keys = [
        fk for fk in catalog["foreign_keys"]
        if fk["from_table"] in known and fk["to_table"] in known
    ]
    domains = _components(names, [(fk["from_table"], fk["to_table"]) for fk in foreign_keys])

    # Everything on one level: nothing has been judged live or dead yet. Tables that
    # declare no relationship fill a spiral; connected ones sit in a ring per group.
    nodes: list[dict[str, Any]] = []
    loose = 0
    for index, table in enumerate(tables):
        domain = domains.get(table["name"], -1)
        angle = index * 2.399963
        if domain >= 0:
            radius = 5 + domain * 4.5
            position = [math.cos(angle + domain) * radius, 0.0,
                        math.sin(angle + domain) * radius]
        else:
            radius = 2.4 * math.sqrt(loose + 1) + 12
            loose += 1
            position = [math.cos(angle) * radius, 0.0, math.sin(angle) * radius]
        nodes.append({
            "id": table["name"],
            "entity": None,
            "class": "core",
            "rows": table["rows"],
            "columns": len(table["columns"]),
            "domain": domain,
            "position": [round(v, 3) for v in position],
            "filters": 0,
            "unreviewed": 0,
        })

    nullable = {
        (t["name"], c["name"].lower()): c["nullable"] for t in tables for c in t["columns"]
    }
    edges = []
    for fk in foreign_keys:
        evidence = {"origin": "declared", "fk_name": fk["name"], "enforced": fk["enforced"]}
        edges.append({
            "from": fk["from_table"], "to": fk["to_table"], "confidence": 1.0,
            "on": " AND ".join(
                f"{fk['from_table']}.{a} = {fk['to_table']}.{b}"
                for a, b in zip(fk["from_columns"], fk["to_columns"], strict=False)
            ),
            "physical_on": "", "from_columns": fk["from_columns"],
            "to_columns": fk["to_columns"], "conditional": False, "condition": "",
            "origin": "declared", "corroborated_by": [], "inferred": False,
            "reason": describe("declared", evidence, fk["from_table"], fk["from_columns"],
                               fk["to_table"], fk["to_columns"]),
            "cardinality": "many_to_one",
            "optional": any(nullable.get((fk["from_table"], c.lower()), False)
                            for c in fk["from_columns"]),
            "status": "accepted",
            "evidence": {
                "inclusion": None, "values_tested": None, "measured": False,
                "orphan_count": None, "orphan_samples": [], "name_similarity": None,
                "parent_unique": None, "needs_cast": None, "sources": [],
                "fk_name": fk["name"], "enforced": fk["enforced"], "chosen_by": "",
            },
        })

    return {
        "database": database_id,
        "view": "submitted",
        "version": 0,
        "nodes": nodes,
        "edges": edges,
        "ghost_edges": [],
        "domains": len(set(domains.values())),
        "summary": {
            "relationships_by_origin": {"declared": len(edges)} if edges else {},
            "core": len(nodes),
            "excluded": 0,
            "by_class": {"core": len(nodes)},
            "views": sum(1 for t in catalog["tables"] if t["kind"] == "view"),
            "columns": sum(len(t["columns"]) for t in tables),
        },
    }


def table_from_catalog(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    found = next((t for t in catalog["tables"] if t["name"].lower() == name.lower()), None)
    if found is None:
        return None
    primary = {c.lower() for c in found["primary_key"]}
    declared = {
        c.lower(): fk["to_table"]
        for fk in catalog["foreign_keys"] if fk["from_table"].lower() == name.lower()
        for c in fk["from_columns"]
    }
    return {
        "table": found["name"],
        "rows": found["rows"],
        "primary_key": found["primary_key"],
        "columns": [
            {
                "physical": c["name"], "logical": "", "type": c["type"],
                "nullable": c["nullable"], "is_primary_key": c["name"].lower() in primary,
                "references": declared.get(c["name"].lower(), ""),
                "decoded": {}, "derived": False, "expr": "", "description": c["comment"] or "",
                "status": "", "confidence": None,
            }
            for c in found["columns"]
        ],
    }
