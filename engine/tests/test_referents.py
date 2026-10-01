"""Resolving which table a cryptic key points at, when the values cannot say.

`CUST_MSTR.REP_NO` holds three zero-padded ids. Cast to integers they sit inside
`EMP_MSTR.EMP_ID` and inside `MEDIA_TYP.MTYP_ID` and inside most other dense keys in
the database, so inclusion, uniqueness and the cast all hold for every candidate and
none of them separates a support rep from a media type. Only reading the name does.

These tests drive the path with a fixed reply rather than a live model, because what
needs pinning down is what the engine asks and what it does with the answer — both of
which must hold whatever the model says, including when it says something useless.
"""

from __future__ import annotations

from stratum.excavate.enrich import (
    TableCard,
    _findings_from_payload,
    merge_referent_findings,
)
from stratum.excavate.findings import Finding, Kind, Status


def _card() -> TableCard:
    card = TableCard(table="CUST_MSTR", row_estimate=59)
    card.references_to_resolve = {
        "REP_NO": (
            ["00000003", "00000004", "00000005"],
            ["MEDIA_TYP", "ALBM", "EMP_MSTR", "GNR"],
        )
    }
    return card


def _join_finding(to_table: str = "MEDIA_TYP") -> Finding:
    return Finding(
        kind=Kind.JOIN,
        target="CUST_MSTR",
        value={
            "from_table": "CUST_MSTR",
            "from_columns": ["REP_NO"],
            "to_table": to_table,
            "to_columns": ["MTYP_ID"],
        },
        confidence=0.65,
        evidence={
            "rival_parents": ["ALBM", "EMP_MSTR", "GNR"],
            "rival_parent_columns": {
                "ALBM": ["ALBM_ID"],
                "EMP_MSTR": ["EMP_ID"],
                "GNR": ["GNR_ID"],
            },
        },
        detector="joins",
        status=Status.HYPOTHESIS,
    )


# ------------------------------------------------------------------ reading the answer


def test_the_named_table_becomes_a_referent_finding():
    payload = {
        "tables": [{"table": "CUST_MSTR", "references": {"REP_NO": "EMP_MSTR"}}]
    }
    findings = _findings_from_payload(payload, [_card()])
    referents = [f for f in findings if f.kind is Kind.JOIN_REFERENT]
    assert len(referents) == 1
    assert referents[0].value["to_table"] == "EMP_MSTR"
    assert referents[0].status is Status.HYPOTHESIS, "a guess is never an acceptance"


def test_a_table_that_was_not_offered_is_not_an_answer():
    """The question was multiple choice. Anything else is the model changing subject."""
    payload = {
        "tables": [{"table": "CUST_MSTR", "references": {"REP_NO": "SOME_OTHER_TABLE"}}]
    }
    findings = _findings_from_payload(payload, [_card()])
    assert not [f for f in findings if f.kind is Kind.JOIN_REFERENT]


def test_a_column_nobody_asked_about_is_ignored():
    payload = {"tables": [{"table": "CUST_MSTR", "references": {"CUST_ID": "EMP_MSTR"}}]}
    findings = _findings_from_payload(payload, [_card()])
    assert not [f for f in findings if f.kind is Kind.JOIN_REFERENT]


# ------------------------------------------------------------------ using the answer


def test_the_relationship_is_repointed_with_the_parent_s_own_key():
    join = _join_finding()
    referent = Finding(
        kind=Kind.JOIN_REFERENT, target="CUST_MSTR.REP_NO",
        value={"to_table": "EMP_MSTR"}, confidence=0.55, evidence={},
        detector="enrich", status=Status.HYPOTHESIS,
    )
    merge_referent_findings([join], [referent])

    assert join.value["to_table"] == "EMP_MSTR"
    assert join.value["to_columns"] == ["EMP_ID"], "the key travels with the parent"
    assert join.evidence["referent_was"] == "MEDIA_TYP"
    assert join.status is Status.HYPOTHESIS


def test_a_relationship_the_data_decided_is_never_moved():
    """No rivals means the evidence chose. A guess must not overrule it."""
    join = _join_finding()
    join.evidence.pop("rival_parents")
    referent = Finding(
        kind=Kind.JOIN_REFERENT, target="CUST_MSTR.REP_NO",
        value={"to_table": "EMP_MSTR"}, confidence=0.55, evidence={},
        detector="enrich", status=Status.HYPOTHESIS,
    )
    merge_referent_findings([join], [referent])
    assert join.value["to_table"] == "MEDIA_TYP"


def test_agreeing_with_the_statistical_choice_is_recorded_not_rewritten():
    join = _join_finding(to_table="EMP_MSTR")
    join.value["to_columns"] = ["EMP_ID"]
    referent = Finding(
        kind=Kind.JOIN_REFERENT, target="CUST_MSTR.REP_NO",
        value={"to_table": "EMP_MSTR"}, confidence=0.55, evidence={},
        detector="enrich", status=Status.HYPOTHESIS,
    )
    merge_referent_findings([join], [referent])
    assert join.evidence["referent_confirmed"] is True
    assert "referent_was" not in join.evidence


def test_no_referent_answer_leaves_everything_alone():
    join = _join_finding()
    merge_referent_findings([join], [])
    assert join.value["to_table"] == "MEDIA_TYP"
