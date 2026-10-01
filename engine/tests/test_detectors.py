"""One test per detector, on the tiny fixture (CLAUDE.md conventions).

These pin behaviour rather than measuring accuracy — the gauntlet does the measuring.
Each assertion names the anti-pattern it covers so a failure says what broke.
"""

from __future__ import annotations

from stratum.excavate.findings import Kind

from .conftest import find

# ----------------------------------------------------------- #4, #5 sprawl and dead


def test_artifacts_classifies_backup_staging_and_temp(excavated):
    classes = {k.lower(): v for k, v in excavated.table_classes.items()}
    assert classes["cust_mstr_bak_20240101"] == "backup"
    assert classes["stg_cust_load"] == "staging"
    assert classes["tmp_scratch"] == "dead"
    assert classes["so_hdr_2022"] == "archive"
    assert classes["cust_mstr"] == "core"
    assert classes["trans_mstr"] == "core"


def test_artifacts_marks_a_broken_view_dead(excavated):
    classes = {k.lower(): v for k, v in excavated.table_classes.items()}
    assert classes["v_broken"] == "dead"


# ------------------------------------------------------------------- #3 procmine


def test_procmine_harvests_join_enum_and_filters(excavated):
    mined = excavated.mined
    pairs = {
        (j.left_table.lower(), j.right_table.lower()) for j in mined.joins
    }
    assert ("cust_mstr", "trans_mstr") in pairs or ("trans_mstr", "cust_mstr") in pairs

    enums = {(e.table.lower(), e.column.lower()): e.mapping for e in mined.enums}
    mapping = enums.get(("trans_mstr", "ord_sts"))
    assert mapping is not None, "the CASE in V_ORD documents the status codes"
    assert mapping["9"].lower() == "cancelled"

    filters = {(f.table.lower(), f.column.lower(), str(f.value)) for f in mined.filters}
    assert ("trans_mstr", "trans_typ_cd", "I") in filters   # the discriminator
    assert ("trans_mstr", "ord_sts", "9") in filters        # the soft delete


# --------------------------------------------------------------- #2 missing FKs


def test_joins_recovers_the_cast_relationship(findings):
    edges = {
        (
            f.value["from_table"].lower(),
            tuple(c.lower() for c in f.value["from_columns"]),
            f.value["to_table"].lower(),
        )
        for f in find(findings, Kind.JOIN)
    }
    assert ("trans_mstr", ("cust_no",), "cust_mstr") in edges, (
        "TRANS_MSTR.CUST_NO is a zero-padded string against an integer key"
    )


def test_joins_recovers_the_composite_relationship(findings):
    composite = [
        f for f in find(findings, Kind.JOIN)
        if len(f.value["from_columns"]) > 1
    ]
    assert composite, "SO_LN joins TRANS_MSTR on (STORE_CD, ORD_NO)"
    columns = {c.lower() for c in composite[0].value["from_columns"]}
    assert columns == {"store_cd", "ord_no"}


def test_joins_does_not_invent_edges_between_unrelated_keys(findings):
    # PRD_ID and EMP_ID are both small dense integers; neither includes into the other
    # for any reason but coincidence, and the detector must not claim it does.
    bad = [
        f for f in find(findings, Kind.JOIN)
        if f.value["from_table"].lower() == "emp_mstr"
        and f.value["to_table"].lower() == "tblprd"
    ]
    assert not bad


# ------------------------------------------------------------ #10 composite keys


def test_keys_recovers_the_order_line_key(findings):
    keys = {
        f.target.lower(): [c.lower() for c in f.value["columns"]]
        for f in find(findings, Kind.KEY)
    }
    assert keys["so_ln"] == ["store_cd", "ord_no", "ln_seq"]


def test_keys_includes_the_discriminator_in_a_god_table_key(findings):
    keys = {
        f.target.lower(): {c.lower() for c in f.value["columns"]}
        for f in find(findings, Kind.KEY)
    }
    assert keys["trans_mstr"] == {"store_cd", "ord_no", "trans_typ_cd"}


# ------------------------------------------------------------------ #7 god table


def test_godtable_splits_on_the_transaction_type(findings):
    god = find(findings, Kind.GOD_TABLE)
    assert len(god) == 1
    assert god[0].value["discriminator"].lower() == "trans_typ_cd"
    assert set(god[0].value["entities"]) == {"I", "P"}


# --------------------------------------------------------------- #12 soft deletes


def test_softdelete_finds_all_three_styles(findings):
    rules = {
        (f.value["table"].lower(), f.value["column"].lower()): f.value["style"]
        for f in find(findings, Kind.SOFT_DELETE)
    }
    assert rules.get(("cust_mstr", "del_flg")) == "flag"
    assert rules.get(("tblprd", "dlt_dt")) == "tombstone_date"
    assert rules.get(("trans_mstr", "ord_sts")) == "status_code"


def test_softdelete_predicate_excludes_only_the_flagged_rows(executor, findings):
    rule = next(
        f for f in find(findings, Kind.SOFT_DELETE)
        if f.value["table"].lower() == "cust_mstr"
    )
    live = executor.run(
        f"SELECT COUNT(*) FROM CUST_MSTR WHERE {rule.value['live_predicate']}"
    )
    assert live.rows[0][0] == 8, "two soft-deleted rows, eight live ones"


# ------------------------------------------------------------- #11 magic numbers


def test_enums_prefers_the_labels_shipped_sql_documents(model):
    mapping = model.enums.get("TRANS_MSTR.ORD_STS") or {}
    assert mapping.get("9", "").lower() == "cancelled"
    assert mapping.get("3", "").lower() == "shipped"


def test_enums_ignores_foreign_keys(findings):
    targets = {f.target.lower() for f in find(findings, Kind.ENUM)}
    assert "so_ln.prd_id" not in targets, "a foreign key is not an enum"


# --------------------------------------------------------- #8 embedded formats


def test_formats_finds_the_xml_and_kv_columns(findings):
    formats = {
        (f.value["table"].lower(), f.value["column"].lower()): f.value["format"]
        for f in find(findings, Kind.EMBEDDED_FORMAT)
    }
    assert formats.get(("cust_mstr", "addr_xml")) == "xml"
    assert formats.get(("tblprd", "attrs")) == "kv"


def test_format_extraction_expression_actually_works(executor, findings):
    finding = next(
        f for f in find(findings, Kind.EMBEDDED_FORMAT)
        if f.value["column"].lower() == "attrs"
    )
    field = next(f for f in finding.value["fields"] if f["path"] == "color")
    result = executor.run(
        f"SELECT {field['expression']} FROM tblPrd WHERE PRD_ID = 1"
    )
    assert result.rows[0][0] == "Red"


# ------------------------------------------------------------------- #6 drift


def test_drift_finds_the_column_replacement(findings):
    drifts = [
        f for f in find(findings, Kind.DRIFT)
        if f.value["table"].lower() == "cust_mstr"
    ]
    assert drifts, "PHONE was replaced by PHN_NBR"
    assert drifts[0].value["kind"] == "column_replacement"
    assert "COALESCE" in drifts[0].value["resolution"].upper()


def test_drift_finds_the_unit_change(findings):
    drifts = [
        f for f in find(findings, Kind.DRIFT)
        if f.value["table"].lower() == "trans_mstr"
    ]
    assert drifts, "TOT_AMT became TOT_AMT_C in cents"
    assert drifts[0].value["kind"] == "unit_change"
    assert drifts[0].value["ratio"] == 100


# ------------------------------------------------------------ #13 type mismatch


def test_types_normalises_the_date_text_column(findings):
    mismatches = {
        f.target.lower(): f.value for f in find(findings, Kind.TYPE_MISMATCH)
        if "expression" in f.value
    }
    order_date = mismatches.get("trans_mstr.ord_dt_txt")
    assert order_date is not None
    assert order_date["logical_type"] == "date"


def test_types_does_not_treat_a_padded_number_as_a_date(findings):
    """`'00000001'` matches \\d{8} but is not a date. Reading it as one corrupts joins."""
    mismatches = {
        f.target.lower(): f.value for f in find(findings, Kind.TYPE_MISMATCH)
        if "expression" in f.value
    }
    rep = mismatches.get("cust_mstr.rep_no")
    assert rep is not None
    assert rep["logical_type"] == "integer"


# ---------------------------------------------------------------- #14 quality


def test_quality_finds_padding_and_null_markers(findings):
    issues = {
        (f.target.lower(), f.value.get("issue"))
        for f in find(findings, Kind.QUALITY)
        if isinstance(f.value, dict)
    }
    assert ("cust_mstr.cfnm1", "whitespace") in issues
    assert ("cust_mstr.co_nm", "null_markers") in issues


def test_enums_skip_a_column_where_every_row_is_different():
    """Two staff named Mike and Jon are short strings in a tiny table, but a code is
    shared by many rows; a value per row names something."""
    from stratum.excavate.enums import is_enum_shaped
    from stratum.excavate.patterns import ValueShape
    from stratum.excavate.profile import ColumnProfile

    def profile(values):
        return ColumnProfile(
            table="stf", column="stf_fnm", declared_type="varchar",
            sampled_rows=len(values), distinct_count=len(set(values)),
            value_shape=ValueShape(sampled=len(values), non_null=len(values), max_length=4),
        )

    assert not is_enum_shaped("stf_fnm", profile(["Mike", "Jon"]), small_table=True)
    assert is_enum_shaped("sts_cd", profile(["A", "I", "A", "X", "A"]), small_table=True)
