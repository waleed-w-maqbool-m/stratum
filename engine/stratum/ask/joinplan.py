"""Deterministic join planning (PLAN.md 4.4 step 5).

The language model never invents a join condition. It is told which entities to use and
handed the exact conditions that connect them, because join conditions are the single
thing a model gets wrong most expensively on a schema with no declared foreign keys —
and wrong quietly, since a bad join returns rows rather than an error.

The graph is the semantic model's relationships, weighted by `1 - confidence`, so a
path through a join that was read out of a working view is preferred over one through a
join that was inferred statistically. Connecting a set of entities is a Steiner tree
problem; `networkx` has a good enough approximation and the graphs here are tiny.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from ..semantic.model import Relationship, SemanticModel


@dataclass
class JoinStep:
    from_entity: str
    to_entity: str
    on: str                       # logical join condition
    condition: str = ""           # extra predicate for an overloaded column
    confidence: float = 1.0
    relationship: str = ""

    def clause(self) -> str:
        predicate = self.on
        if self.condition:
            predicate = f"{predicate} AND {self.condition}"
        return f"JOIN {self.to_entity} ON {predicate}"


@dataclass
class JoinPlan:
    entities: list[str] = field(default_factory=list)
    steps: list[JoinStep] = field(default_factory=list)
    root: str = ""
    connected: bool = True
    unreachable: list[str] = field(default_factory=list)
    min_confidence: float = 1.0
    fan_out_risk: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The FROM clause the generator is given, already joined."""
        if not self.root:
            return ""
        lines = [f"FROM {self.root}"]
        lines.extend(f"  {step.clause()}" for step in self.steps)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "entities": self.entities,
            "connected": self.connected,
            "unreachable": self.unreachable,
            "min_confidence": round(self.min_confidence, 3),
            "steps": [
                {
                    "from": s.from_entity, "to": s.to_entity, "on": s.on,
                    "condition": s.condition, "confidence": round(s.confidence, 3),
                }
                for s in self.steps
            ],
            "fan_out_risk": self.fan_out_risk,
        }


def build_graph(model: SemanticModel) -> nx.Graph:
    """Entities as nodes, relationships as edges weighted by how much we trust them."""
    graph = nx.Graph()
    for entity in model.accepted_entities():
        graph.add_node(entity.name, row_estimate=entity.row_estimate)

    for relationship in model.relationships:
        if relationship.status == "rejected" or not relationship.logical_on:
            continue
        if not relationship.from_entity or not relationship.to_entity:
            continue
        if relationship.from_entity == relationship.to_entity:
            continue        # a self-join is never part of a connecting path
        weight = max(0.01, 1.0 - relationship.confidence)
        existing = graph.get_edge_data(
            relationship.from_entity, relationship.to_entity
        )
        if existing is not None and existing["weight"] <= weight:
            continue
        graph.add_edge(
            relationship.from_entity,
            relationship.to_entity,
            weight=weight,
            relationship=relationship,
        )
    return graph


def plan(
    model: SemanticModel,
    entities: list[str],
    *,
    graph: nx.Graph | None = None,
) -> JoinPlan:
    """Connect `entities` through the highest-confidence path available."""
    wanted = [e for e in dict.fromkeys(entities) if model.entity(e) is not None]
    result = JoinPlan(entities=wanted)
    if not wanted:
        result.connected = False
        return result

    graph = graph if graph is not None else build_graph(model)
    present = [e for e in wanted if e in graph]
    result.unreachable = [e for e in wanted if e not in graph]

    if len(present) <= 1:
        result.root = present[0] if present else (wanted[0] if wanted else "")
        result.connected = not result.unreachable
        return result

    # Only entities in one connected component can be joined at all.
    components = list(nx.connected_components(graph))
    component = next(
        (c for c in components if present[0] in c), set()
    )
    reachable = [e for e in present if e in component]
    result.unreachable += [e for e in present if e not in component]
    if len(reachable) <= 1:
        result.root = reachable[0] if reachable else present[0]
        result.connected = False
        return result

    try:
        tree = nx.approximation.steiner_tree(graph, reachable, weight="weight")
    except Exception:
        # Fall back to a shortest-path union, which is what steiner_tree approximates.
        tree = nx.Graph()
        for target in reachable[1:]:
            try:
                path = nx.shortest_path(graph, reachable[0], target, weight="weight")
            except nx.NetworkXNoPath:
                result.unreachable.append(target)
                continue
            for left, right in zip(path, path[1:], strict=False):
                tree.add_edge(left, right, **graph.get_edge_data(left, right))

    if tree.number_of_nodes() == 0:
        result.root = reachable[0]
        result.connected = False
        return result

    # Root at the requested entity with the most rows: joining outwards from the
    # largest table reads better and matches how a person would write it.
    root = max(
        (n for n in tree.nodes if n in reachable),
        key=lambda n: graph.nodes[n].get("row_estimate", 0),
        default=reachable[0],
    )
    result.root = root

    # Walk the tree outwards so every step joins something already in scope.
    for left, right in nx.bfs_edges(tree, root):
        data = tree.get_edge_data(left, right) or {}
        relationship: Relationship | None = data.get("relationship")
        if relationship is None:
            continue
        # Orient the condition so it reads from the entity already in scope.
        on = relationship.logical_on
        result.steps.append(
            JoinStep(
                from_entity=left,
                to_entity=right,
                on=on,
                condition=relationship.condition,
                confidence=relationship.confidence,
                relationship=relationship.name,
            )
        )

    result.min_confidence = min(
        (s.confidence for s in result.steps), default=1.0
    )
    result.connected = not result.unreachable
    result.fan_out_risk = _fan_out_risk(model, result)
    return result


def _fan_out_risk(model: SemanticModel, plan_result: JoinPlan) -> list[str]:
    """Entities whose join multiplies rows, which corrupts any sum above it.

    Joining orders to order lines and then summing the order total gives each order's
    total once per line. The validator turns this list into a real check; here it is
    just the warning that tells it where to look.
    """
    out: list[str] = []
    for step in plan_result.steps:
        child = model.entity(step.to_entity)
        parent = model.entity(step.from_entity)
        if child is None or parent is None:
            continue
        if child.row_estimate > parent.row_estimate * 1.5:
            out.append(step.to_entity)
    return out


def neighbours(model: SemanticModel, entity: str, depth: int = 1) -> list[str]:
    """Entities within `depth` joins — used to widen retrieval when linking is unsure."""
    graph = build_graph(model)
    if entity not in graph:
        return []
    lengths = nx.single_source_shortest_path_length(graph, entity, cutoff=depth)
    return [node for node, distance in lengths.items() if distance > 0]
