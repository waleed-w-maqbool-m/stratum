"""Behaviour Pagila surfaced that Chinook never could.

SQLite compares identifiers case-insensitively, has no comments, and was the only
dialect the engine had run against, so each of these was invisible until a second
database arrived. None of them is specific to Pagila: every case below is written in
the general form a real Postgres database produces.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import sqlglot

from stratum.ask.dialect import (
    needs_quoting,
    quote_catalog_identifiers,
    quote_catalog_sql,
)
from stratum.excavate.naming import expand
from stratum.excavate.profile import ColumnProfile
from stratum.excavate.softdelete import _status_rule, _tombstone_rule

TABLE = SimpleNamespace(qualified="t")


def _profile(column: str, declared: str, values: list[tuple], *, rows: int, nulls: int = 0,
             numeric: bool = False) -> ColumnProfile:
    return ColumnProfile(
        table="t", column=column, declared_type=declared, sampled_rows=rows,
        null_count=nulls, null_fraction=nulls / rows, distinct_count=len(values),
        top_values=values, numeric=numeric,
    )


# ------------------------------------------------------------------ identifier quoting


def test_postgres_quotes_only_what_folding_would_break():
    """Unquoted, Postgres folds `tblFilm` to `tblfilm`, which does not exist."""
    assert needs_quoting("tblFilm", "postgres")
    assert needs_quoting("CUST_MSTR", "postgres")
    assert not needs_quoting("rntl_hdr", "postgres")


def test_case_insensitive_dialects_never_quote():
    assert not needs_quoting("tblFilm", "sqlite")
    assert not needs_quoting("tblFilm", "mssql")


def test_catalog_names_are_quoted_exactly_as_spelled():
    tree = sqlglot.parse_one('SELECT intFilmID, rntl_id FROM tblFilm', read="postgres")
    sql = quote_catalog_identifiers(tree, {"tblFilm", "intFilmID"}).sql(dialect="postgres")
    assert '"tblFilm"' in sql and '"intFilmID"' in sql
    assert '"rntl_id"' not in sql, "a name that works bare is left alone"


def test_text_probes_are_quoted_the_same_way():
    sql = quote_catalog_sql("SELECT COUNT(*) FROM tblFilm WHERE dtmDltd IS NULL",
                            {"tblFilm", "dtmDltd"}, "postgres")
    assert '"tblFilm"' in sql and '"dtmDltd"' in sql
    assert quote_catalog_sql("SELECT 1", {"tblFilm"}, "sqlite") == "SELECT 1"


# -------------------------------------------------------------------- Hungarian names


def test_hungarian_type_tags_are_stripped():
    assert expand("dtmDltd").type_prefix == "dtm"
    assert expand("dtmDltd").words == ["deleted"]
    assert expand("curRntlRate").type_prefix == "cur"


def test_snake_case_cur_is_current_not_currency():
    """Only the camelCase shape is Hungarian. `cur_bal` is a current balance."""
    assert expand("cur_bal").type_prefix == ""
    assert "cur" in expand("cur_bal").words


# ----------------------------------------------------------------------- soft deletes


def test_lettered_status_with_a_rare_delete_code():
    """A / I / X: the numeric rule looked for `9` out of sequence and saw nothing."""
    rule = _status_rule(TABLE, "sts_cd", _profile(
        "sts_cd", "character(1)", [("A", 584), ("I", 15), ("X", 9)], rows=608,
    ))
    assert rule is not None
    assert rule.deleted_predicate == "sts_cd = 'X'"


def test_a_lettered_status_without_a_delete_code_is_not_a_delete():
    """`I` is inactive — still a customer, and hiding it would lose real rows."""
    assert _status_rule(TABLE, "sts_cd", _profile(
        "sts_cd", "character(1)", [("A", 584), ("I", 15)], rows=599,
    )) is None


def test_a_common_delete_code_is_not_a_soft_delete():
    assert _status_rule(TABLE, "sts_cd", _profile(
        "sts_cd", "character(1)", [("A", 300), ("X", 300)], rows=600,
    )) is None


def test_tombstone_dated_by_its_type_or_tag():
    """`dtmDltd`'s only date word is the Hungarian tag; its type says so too."""
    rule = _tombstone_rule(TABLE, "dtmDltd", _profile(
        "dtmDltd", "timestamp without time zone", [("2022-03-01", 1)], rows=1012, nulls=1000,
    ))
    assert rule is not None and rule.deleted_predicate == "dtmDltd IS NOT NULL"


def test_a_plain_deleted_column_typed_as_a_timestamp_is_a_tombstone():
    rule = _tombstone_rule(TABLE, "deleted", _profile(
        "deleted", "timestamp", [("2022-03-01", 1)], rows=100, nulls=90,
    ))
    assert rule is not None


# ------------------------------------------------------------------- compiler behaviour


def _model():
    from stratum.semantic.model import Column, DefaultFilter, Entity, SemanticModel

    customer = Entity(
        name="customer", source="public.cstmr", key=["cstmr_id"],
        columns={
            "id": Column(name="id", expr="cstmr_id", physical_column="cstmr_id",
                         data_type="integer"),
            # Same spelling as the physical column, different value: the trap for any
            # predicate that lands below the projection.
            "cfnm": Column(name="cfnm", expr="RTRIM(cfnm)", physical_column="cfnm",
                           case_insensitive=True),
        },
    )
    film = Entity(
        name="film", source="public.tblFilm", key=["intFilmID"],
        columns={"id": Column(name="id", expr="intFilmID", physical_column="intFilmID")},
        default_filters=[DefaultFilter(expr="dtmDltd IS NULL", reason="tombstone")],
    )
    return SemanticModel(
        database="t", dialect="postgres", profile="pg16",
        entities={"customer": customer, "film": film},
        quoted_identifiers=["dtmDltd", "intFilmID", "tblFilm"],
    )


def test_a_pushed_predicate_sees_the_logical_value_not_the_raw_column():
    from stratum.ask.compiler import compile_logical
    from stratum.connectors.profiles import PG16

    sql = compile_logical(
        "SELECT customer.id FROM customer WHERE customer.cfnm = 'Austin'", _model(), PG16,
    ).physical_sql
    inner = sql.split("AS customer")[0]
    assert "RTRIM(cfnm)" in inner.replace('"', "") and "cfnm = " not in inner.replace(
        "RTRIM(cfnm)", ""
    ), f"pushed predicate compared the raw padded column:\n{sql}"


def test_literals_are_case_folded_for_a_case_insensitive_column():
    from stratum.ask.compiler import compile_logical
    from stratum.connectors.profiles import PG16

    sql = compile_logical(
        "SELECT customer.id FROM customer WHERE customer.cfnm = 'Austin'", _model(), PG16,
    ).physical_sql
    assert "'AUSTIN'" in sql and "UPPER(" in sql
    assert "'Austin'" not in sql


def test_a_quoted_filter_still_counts_as_applied():
    """`"dtmDltd" IS NULL` is the filter `dtmDltd IS NULL`, whatever the quoting."""
    from stratum.ask.compiler import compile_logical
    from stratum.ask.validate import _check_default_filters
    from stratum.connectors.profiles import PG16

    model = _model()
    compiled = compile_logical("SELECT COUNT(*) AS n FROM film", model, PG16)
    assert '"dtmDltd"' in compiled.physical_sql
    assert _check_default_filters(compiled, model).passed is True


# ------------------------------------------------------------------------ drift units


def test_unit_ratio_is_found_whichever_way_round_the_columns_arrive():
    """Dollars max 10.99, cents max 1199. Given the cents column as the 'old' one, a
    one-directional check saw a ratio of 0.009 and called it a rename."""
    from stratum.excavate.drift import _unit_ratio

    dollars = _profile("amt", "numeric", [(Decimal("4.99"), 1)], rows=10, numeric=True)
    cents = _profile("amt_c", "integer", [(499, 1)], rows=10, numeric=True)
    dollars.max_value, cents.max_value = Decimal("10.99"), 1199
    assert _unit_ratio(dollars, cents) == 100
    assert _unit_ratio(cents, dollars) is None, "direction matters; detect() tries both"


# ------------------------------------------------------------------------ ambiguity


def test_new_is_only_ambiguous_when_it_qualifies_an_entity():
    from stratum.ask.ambiguity import detect

    model = _model()
    assert not detect("Show every event with the old and new values", model).ambiguities
    assert [a.term for a in detect("How many new customers joined?", model).ambiguities] \
        == ["new"]


def test_vowel_dropped_names_are_not_mistaken_for_words():
    """`cntry` keeps a `y`, so it read as an English word and was never expanded."""
    assert expand("cntry").logical == "country"
    assert expand("invtry").is_opaque, "unknown abbreviations go to enrichment"
    assert not expand("customer").is_opaque


def test_a_word_that_starts_like_a_type_tag_is_left_whole():
    """`street` is not `str` + `eet`."""
    assert expand("street").logical == "street"
    assert expand("interest").logical == "interest"
    assert expand("tblprd").logical == "product"


def test_snake_case_str_is_a_store_not_a_string():
    """Data-type tags are Hungarian only in camelCase."""
    assert "str" in expand("str_no").words
    assert expand("strTitle").words == ["title"]
    assert expand("tbl_customer").words == ["customer"]


def test_numbers_that_differ_are_different_values():
    from stratum.excavate.quality import _same_spelling

    assert not _same_spelling("FORMER1", "Former0")
    assert not _same_spelling("0000000004", "0000000002")
    assert _same_spelling("Brasil", "Brazil")
    assert not _same_spelling("NSW", "NS"), "an abbreviation is world knowledge"


def test_a_time_of_day_is_not_a_foreign_key():
    from stratum.excavate.joins import _looks_like_identifier

    assert _looks_like_identifier("REP_NO") and _looks_like_identifier("inv_no")
    assert not _looks_like_identifier("rntl_tm")
    assert not _looks_like_identifier("intRntlDur")


def test_a_big_table_is_big_relative_to_its_own_database():
    """A 400,000-row log among 16,000-row tables is the one scan worth stopping; a
    fixed half-million threshold, sized for an 8M-row log, never noticed it."""
    from stratum.connectors.costguard import CostGuard, CostPolicy

    tables = [SimpleNamespace(name=n, row_estimate=r) for n, r in
              [("rental", 16044), ("payment", 16049), ("film", 1000), ("evt_log", 400_000)]]
    connector = SimpleNamespace(list_tables=lambda: tables)
    guard = CostGuard(connector)
    assert 16_049 < guard.big_table_rows() <= 400_000
    assert CostGuard(connector, CostPolicy(big_table_rows=900_000)).big_table_rows() == 900_000


def test_a_reasoning_model_out_of_budget_gets_one_bigger_retry():
    """Qwen spent all 500 tokens thinking on one question. Dropping the provider turned
    that into an abstention; the same request with room to finish is the fix."""
    from stratum.llm.budget import Budgeter
    from stratum.llm.client import Completion, LLMClient, Provider

    provider = Provider(name="p", base_url="http://x", api_key="k",
                        models={"large": "m"}, requires_key=True, notes="", params={})
    client = LLMClient(providers=[provider], budgeter=Budgeter({}))
    seen: list[int] = []

    def fake_call(_provider, _messages, _role, _temperature, max_tokens, _json_mode):
        seen.append(max_tokens)
        if len(seen) == 1:
            raise RuntimeError("m used all 500 tokens reasoning and returned nothing")
        return Completion(text="SELECT 1", provider="p", model="m",
                          prompt_tokens=10, completion_tokens=5, elapsed_ms=1)

    client._call = fake_call
    out = client.complete([{"role": "user", "content": "q"}], max_tokens=500,
                          use_cache=False)
    assert out.text == "SELECT 1"
    assert seen == [500, 1000]
