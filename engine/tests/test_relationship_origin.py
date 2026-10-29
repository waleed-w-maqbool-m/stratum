"""Where each relationship came from: declared, shipped SQL, data, or a model's choice.

Built on `fixtures/build_declared.py`, which has one relationship of each origin. These pin
the labels, the corroboration, the orphan counts and the sentence a person reads.
"""

from __future__ import annotations

import pytest

from stratum.api.app import _edge_payload
from stratum.connectors.base import ForeignKeyInfo
from stratum.connectors.registry import build
from stratum.excavate import declared, enrich
from stratum.excavate.compile_model import compile_model
from stratum.excavate.explain_relationship import describe
from stratum.excavate.findings import Finding, Kind, Status
from stratum.excavate.introspect import Schema, Table
from stratum.excavate.pipeline import excavate
from stratum.semantic.model import Relationship, SemanticModel

from .fixtures.build_declared import ORPHAN_CUSTOMERS
from .fixtures.build_declared import build as build_declared


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    path = build_declared(tmp_path_factory.mktemp("declared") / "declared.sqlite")
    conn = build(f"sqlite:///{path}")
    conn.connect()
    try:
        yield excavate(conn)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def edges(result):
    out = {}
    for finding in result.findings.of(Kind.JOIN):
        value = finding.value
        out[(value["from_table"].lower(), value["to_table"].lower())] = finding
    return out


def test_the_catalog_foreign_keys_are_read(result):
    assert result.schema.summary()["declared_foreign_keys"] == 3


def test_a_declared_key_is_labelled_declared_with_full_confidence(edges):
    finding = edges[("order_line", "orders")]
    assert finding.evidence["origin"] == "declared"
    assert finding.confidence == 1.0
    assert finding.status is Status.ACCEPTED


def test_a_declared_key_is_checked_against_the_data(edges):
    finding = edges[("order_line", "orders")]
    assert finding.evidence["inclusion_measured"] is True
    assert finding.evidence["inclusion"] == 1.0
    assert finding.evidence["orphan_count"] == 0
    assert "data" in finding.evidence["corroborated_by"]


def test_a_declared_key_with_orphans_reports_them(edges):
    finding = edges[("orders", "customer")]
    assert finding.evidence["origin"] == "declared"
    assert finding.evidence["orphan_count"] == len(ORPHAN_CUSTOMERS)
    assert {int(v) for v in finding.evidence["orphan_samples"]} == set(ORPHAN_CUSTOMERS)
    assert finding.evidence["inclusion"] < 1.0
    # Still declared, still believed: the constraint exists even though the data breaks it.
    assert finding.confidence == 1.0


def test_a_join_written_only_in_a_view_is_labelled_shipped_sql(edges):
    finding = edges[("stock", "warehouse")]
    assert finding.evidence["origin"] == "shipped_sql"
    assert finding.evidence["sources"] == ["view v_stock"]
    assert finding.evidence["inclusion"] == 1.0


def test_a_join_visible_only_in_the_data_is_labelled_data(edges):
    finding = edges[("invoice", "orders")]
    assert finding.evidence["origin"] == "data"
    assert finding.evidence["corroborated_by"] == []
    assert finding.status is Status.HYPOTHESIS
    assert finding.confidence < 1.0


def test_cardinality_and_optionality_come_from_the_profile(edges):
    nullable = edges[("orders", "customer")]
    assert nullable.evidence["cardinality"] == "many_to_one"
    assert nullable.evidence["optional"] is True       # ten orders have no customer
    assert edges[("order_line", "orders")].evidence["optional"] is False
    assert edges[("customer_profile", "customer")].evidence["cardinality"] == "one_to_one"
    assert edges[("invoice", "orders")].evidence["cardinality"] == "many_to_one"


def test_every_relationship_in_the_model_has_an_origin_and_a_reason(result):
    model = compile_model(result)
    assert model.relationships
    for relationship in model.relationships:
        assert relationship.origin in {"declared", "shipped_sql", "data", "llm", "conditional"}
        assert relationship.provenance and relationship.provenance.reason


def test_an_edge_found_twice_is_kept_once_under_its_strongest_origin(result):
    model = compile_model(result)
    pairs = [(r.from_table.lower(), r.to_table.lower()) for r in model.relationships]
    assert len(pairs) == len(set(pairs))


def test_the_atlas_payload_carries_the_label_not_a_guess_from_the_detector(result):
    model = compile_model(result)
    payload = {(e["from"].lower(), e["to"].lower()): e
               for e in map(_edge_payload, model.relationships)}
    declared_edge = payload[("orders", "customer")]
    assert declared_edge["origin"] == "declared" and declared_edge["inferred"] is False
    assert "Declared as a foreign key" in declared_edge["reason"]
    assert payload[("stock", "warehouse")]["inferred"] is False
    inferred = payload[("invoice", "orders")]
    assert inferred["origin"] == "data" and inferred["inferred"] is True
    assert inferred["evidence"]["measured"] is True


def test_the_catalog_snapshot_holds_structure_and_no_values(result):
    catalog = result.schema.to_catalog()
    assert {t["name"] for t in catalog["tables"]} >= {"customer", "orders", "v_stock"}
    assert len(catalog["foreign_keys"]) == 3
    text = str(catalog)
    assert "Customer 1" not in text and "Warehouse 1" not in text


# -------------------------------------------------------------- without a database


def _schema_with(fk: ForeignKeyInfo) -> Schema:
    from stratum.connectors.base import TableInfo

    schema = Schema(dialect="mssql", database="x")
    for name in ("child", "parent"):
        schema.tables[name] = Table(info=TableInfo(schema="", name=name))
    schema.foreign_keys = [fk]
    return schema


def test_a_constraint_the_engine_does_not_enforce_is_less_certain():
    fk = ForeignKeyInfo(schema="", table="child", columns=["p_id"], ref_schema="",
                        ref_table="parent", ref_columns=["id"], name="FK_c_p", enforced=False)
    [finding] = declared.detect(_schema_with(fk))
    assert finding.evidence["origin"] == "declared"
    assert finding.evidence["enforced"] is False
    assert finding.confidence == declared.UNENFORCED_CONFIDENCE


def test_a_declared_key_with_mismatched_column_counts_is_ignored():
    fk = ForeignKeyInfo(schema="", table="child", columns=["a", "b"], ref_schema="",
                        ref_table="parent", ref_columns=["id"])
    assert declared.detect(_schema_with(fk)) == []


def test_a_models_choice_never_moves_a_declared_edge():
    declared_edge = Finding(
        kind=Kind.JOIN, target="child",
        value={"from_table": "child", "from_columns": ["p_id"],
               "to_table": "parent", "to_columns": ["id"]},
        confidence=1.0,
        evidence={"origin": "declared", "rival_parents": ["other"]},
        detector="declared",
    )
    answer = Finding(
        kind=Kind.JOIN_REFERENT, target="child.p_id",
        value={"to_table": "other"}, confidence=0.8, detector="enrich",
    )
    enrich.merge_referent_findings([declared_edge], [answer])
    assert declared_edge.value["to_table"] == "parent"


def test_a_model_that_breaks_a_data_tie_says_so_and_what_it_replaced():
    edge = Finding(
        kind=Kind.JOIN, target="child",
        value={"from_table": "child", "from_columns": ["p_id"],
               "to_table": "parent", "to_columns": ["id"]},
        confidence=0.8,
        evidence={"origin": "data", "rival_parents": ["other"],
                  "rival_parent_columns": {"other": ["oid"]}},
        detector="joins",
    )
    answer = Finding(
        kind=Kind.JOIN_REFERENT, target="child.p_id",
        value={"to_table": "other"}, confidence=0.8, detector="enrich",
    )
    enrich.merge_referent_findings([edge], [answer])
    assert edge.value["to_table"] == "other"
    assert edge.evidence["chosen_by"] == "llm"
    assert edge.evidence["measured_against"] == "parent"
    sentence = describe("data", edge.evidence, "child", ["p_id"], "other", ["oid"])
    assert "language model chose other over parent" in sentence


def test_an_old_model_without_origin_derives_it_on_load():
    def relationship(detector: str, **evidence):
        return Relationship(
            name="a_b", from_entity="a", to_entity="b", from_table="A", to_table="B",
            provenance={"detector": detector, "reason": "", "evidence": evidence},
        )

    assert relationship("procmine").origin == "shipped_sql"
    assert relationship("joins", source="shipped SQL").origin == "shipped_sql"
    assert relationship("joins", source="inclusion test").origin == "data"
    assert relationship("overloaded").origin == "conditional"
    assert relationship("enrich").origin == "llm"


def test_origin_survives_a_save_and_load(result):
    model = compile_model(result)
    again = SemanticModel.from_yaml(model.to_yaml())
    assert {r.name: r.origin for r in again.relationships} == {
        r.name: r.origin for r in model.relationships
    }


def test_the_sentences_name_the_evidence():
    assert describe("shipped_sql", {"sources": ["view v_a", "procedure p_b"]}, "a", ["x"],
                    "b", ["y"]).startswith("Not declared. Written as a join in view v_a")
    data = describe(
        "data",
        {"inclusion_measured": True, "inclusion": 0.975, "distinct_values_tested": 400,
         "orphan_count": 10, "orphan_samples": ["7"], "parent_key_is_unique": True,
         "name_similarity": 0.82, "needs_cast": True},
        "ORD", ["CUST_NO"], "CUST", ["CUST_ID"],
    )
    assert "97.5% of 400 distinct values tested exist in CUST" in data
    assert "10 have no match (for example 7)" in data
    assert "The names match at 0.82" in data
    assert "casts one" in data
    assert describe("llm", {}, "a", ["x"], "b", ["y"]).startswith("Guessed by the language model")
