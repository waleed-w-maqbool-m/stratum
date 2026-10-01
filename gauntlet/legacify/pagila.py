"""Legacify Pagila (PostgreSQL 16).

Reads the pristine `pagila_clean` database and writes `pagila_legacy` — the same facts
under the anti-patterns PLAN.md §3.1 assigns to Pagila (cryptic names, missing FKs,
JSON blobs, drift, soft deletes, string mess) — plus the `manifest.json` answer key.

Two things this database exercises that Chinook cannot:

* **Postgres itself.** Date parsing, JSON extraction and identifier quoting all differ
  from SQLite, so every rule the engine compiles has to be rendered for a second
  dialect. One table is a quoted mixed-case vendor import (`"tblFilm"`, `"intFilmID"`),
  the usual residue of a SQL Server migration: unquoted, Postgres folds it to
  `tblfilm` and the table does not exist.
* **Documentation, some of it wrong** (anti-pattern #3). SQLite has no comments;
  Postgres does, and a legacy database's comments are as likely to mislead as help.
  `fin_txn.amt` is documented as cents and holds dollars.

CHAR padding is done in VARCHAR on purpose. Postgres `character(n)` ignores trailing
spaces in comparisons, so a padded CHAR column is not actually a trap there; a
fixed-width load into VARCHAR is, and is what migrated data usually looks like.

Invariant that keeps the gold answers valid: **every transformation is reversible.**
Refunds, late fees, deleted customers and tombstoned films are *extra* rows that do
not exist in the clean database, so filtering them out reproduces the clean set exactly.

Usage:
    python -m gauntlet.legacify.pagila [--out DIR]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import (
    NULL_MARKERS,
    Manifest,
    PgLegacyDB,
    json_pack,
    rng,
    zero_pad,
)

# Money moved from dollars in AMT to integer cents in AMT_C on this date (#6).
CENTS_CUTOVER = datetime(2022, 4, 1)

# Rentals from this date record their return as a timestamp; earlier ones as text (#6).
RETURN_CUTOVER = "20220601"

# Customers above this id have their email only inside the PRFL JSON blob (#6, #8).
EMAIL_CUTOVER_ID = 300

RATING_CODES = {"G": 1, "PG": 2, "PG-13": 3, "R": 4, "NC-17": 5}
TXN_TYPES = {"P": "payment", "R": "refund", "A": "adjustment (late fee)"}
STATUS_CODES = {"A": "active", "I": "inactive", "X": "deleted"}

EVT_LOG_ROWS = 400_000

GOD_TABLE_FILLER = [
    ("pmt_mthd_cd", "varchar(4)"), ("card_lst4", "char(4)"), ("auth_cd", "varchar(12)"),
    ("batch_no", "integer"), ("src_sys_cd", "varchar(6)"), ("crncy_cd", "char(3)"),
    ("fx_rt", "numeric(10,6)"), ("tax_amt", "numeric(7,2)"), ("dsc_amt", "numeric(7,2)"),
    ("gl_acct", "varchar(12)"), ("cc_cd", "varchar(8)"), ("recon_flg", "char(1)"),
    ("recon_dt_txt", "char(8)"), ("xport_flg", "char(1)"), ("xport_dt_txt", "char(8)"),
    ("hold_flg", "char(1)"), ("hold_rsn", "varchar(40)"), ("apprv_by", "varchar(12)"),
    ("apprv_dt_txt", "char(8)"), ("notes", "varchar(200)"), ("term_id", "varchar(10)"),
    ("lane_no", "integer"), ("lst_upd_by", "varchar(12)"), ("lst_upd_ts", "varchar(20)"),
]


# ------------------------------------------------------------------------------ helpers


def _pad(value: str | None, width: int) -> str | None:
    """A fixed-width load: right-padded, but into VARCHAR, so the spaces are kept."""
    if value is None:
        return None
    return value[:width].ljust(width)


def _null_marker(value: str | None, r) -> str | None:
    """NULL spelled 'N/A', '-', '' or 'UNKNOWN' (#14)."""
    if value is None or value == "":
        return r.choice(NULL_MARKERS) if r.random() < 0.85 else None
    return value


def _mixed_case(value: str | None, r) -> str | None:
    if not value:
        return value
    roll = r.random()
    if roll < 0.2:
        return value.upper()
    if roll < 0.32:
        return value.lower()
    return value


def _naive(ts: datetime | None) -> datetime | None:
    """Legacy systems store local wall-clock time without a zone. Pagila's timestamps
    are UTC, so dropping the zone keeps the same instant as the clean copy."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def _read(src, sql: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with src.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return list(cur.fetchall())


# ------------------------------------------------------------------------ reference data


def build_reference(src, db: PgLegacyDB, m: Manifest) -> None:
    """Countries, cities, addresses, languages, categories and actors."""
    r = rng("pagila-reference")

    db.create("cntry", [("cntry_id", "integer"), ("cntry_nm", "varchar(50)")], pk=["cntry_id"])
    db.insert("cntry", ["cntry_id", "cntry_nm"],
              [(x["country_id"], x["country"]) for x in _read(src, "SELECT * FROM country")])

    db.create("city_mstr", [("city_id", "integer"), ("city_nm", "varchar(50)"),
                            ("cntry_id", "integer")], pk=["city_id"])
    db.insert("city_mstr", ["city_id", "city_nm", "cntry_id"], [
        (x["city_id"], _mixed_case(x["city"], r), x["country_id"])
        for x in _read(src, "SELECT * FROM city")
    ])

    db.create("addr", [("addr_id", "integer"), ("addr1", "varchar(50)"),
                       ("addr2", "varchar(50)"), ("dstrct", "varchar(20)"),
                       ("city_id", "integer"), ("pstl_cd", "varchar(10)"),
                       ("phn", "varchar(20)")], pk=["addr_id"])
    db.insert("addr", ["addr_id", "addr1", "addr2", "dstrct", "city_id", "pstl_cd", "phn"], [
        (x["address_id"], x["address"], _null_marker(x["address2"], r),
         _null_marker(x["district"], r), x["city_id"], _null_marker(x["postal_code"], r),
         x["phone"] or None)
        for x in _read(src, "SELECT * FROM address")
    ])

    db.create("lang", [("lang_id", "integer"), ("lang_nm", "varchar(20)")], pk=["lang_id"])
    db.insert("lang", ["lang_id", "lang_nm"],
              [(x["language_id"], x["name"].strip()) for x in _read(src, "SELECT * FROM language")])

    db.create("ctgry", [("ctgry_id", "integer"), ("ctgry_nm", "varchar(25)")], pk=["ctgry_id"])
    db.insert("ctgry", ["ctgry_id", "ctgry_nm"],
              [(x["category_id"], x["name"]) for x in _read(src, "SELECT * FROM category")])

    db.create("actr", [("actr_id", "integer"), ("actr_fnm", "varchar(45)"),
                       ("actr_lnm", "varchar(45)")], pk=["actr_id"])
    db.insert("actr", ["actr_id", "actr_fnm", "actr_lnm"], [
        (x["actor_id"], _pad(x["first_name"], 45), _pad(x["last_name"], 45))
        for x in _read(src, "SELECT * FROM actor")
    ])

    for phys, logical in [("cntry", "country"), ("city_mstr", "city"), ("addr", "address"),
                          ("lang", "language"), ("ctgry", "category"), ("actr", "actor")]:
        m.rename(phys, logical)
        m.classify(phys, "core")
    for phys, pk in [("cntry", "cntry_id"), ("city_mstr", "city_id"), ("addr", "addr_id"),
                     ("lang", "lang_id"), ("ctgry", "ctgry_id"), ("actr", "actr_id")]:
        m.key(phys, [pk])
    for tbl, col, ent, logical in [
        ("cntry", "cntry_nm", "country", "name"), ("city_mstr", "city_nm", "city", "name"),
        ("addr", "addr1", "address", "address"), ("addr", "addr2", "address", "address2"),
        ("addr", "dstrct", "address", "district"), ("addr", "pstl_cd", "address", "postal_code"),
        ("addr", "phn", "address", "phone"), ("lang", "lang_nm", "language", "name"),
        ("ctgry", "ctgry_nm", "category", "name"), ("actr", "actr_fnm", "actor", "first_name"),
        ("actr", "actr_lnm", "actor", "last_name"),
    ]:
        m.col(tbl, col, ent, logical)
    m.join(child="city_mstr", child_cols=["cntry_id"], parent="cntry", parent_cols=["cntry_id"])
    m.join(child="addr", child_cols=["city_id"], parent="city_mstr", parent_cols=["city_id"])
    m.record_quality(table="actr", column="actr_lnm", issue="char_padding", fix="RTRIM",
                     note="padded values in VARCHAR, so equality fails without RTRIM")
    m.record_quality(table="actr", column="actr_fnm", issue="char_padding", fix="RTRIM")
    m.record_quality(table="city_mstr", column="city_nm", issue="mixed_case",
                     fix="compare case-insensitively")
    m.record_quality(table="addr", column="addr2", issue="null_markers",
                     markers=NULL_MARKERS, fix="NULLIF on each marker")
    m.record_quality(table="addr", column="dstrct", issue="null_markers",
                     markers=NULL_MARKERS, fix="NULLIF on each marker")


# --------------------------------------------------------------------------- people


def build_stores_and_staff(src, db: PgLegacyDB, m: Manifest) -> dict[int, int]:
    """Returns staff id -> store id, which the transaction builder needs."""
    r = rng("pagila-staff")
    staff = _read(src, "SELECT * FROM staff ORDER BY staff_id")
    stores = _read(src, "SELECT * FROM store ORDER BY store_id")

    db.create("str_loc", [("str_id", "integer"), ("mgr_stf_no", "integer"),
                          ("addr_id", "integer")], pk=["str_id"])
    db.insert("str_loc", ["str_id", "mgr_stf_no", "addr_id"],
              [(x["store_id"], x["manager_staff_id"], x["address_id"]) for x in stores])

    # No password, no picture: nothing in the benchmark needs them, and a column of
    # credential hashes has no business sitting in a database an LLM will profile.
    db.create("stf", [("stf_id", "integer"), ("stf_fnm", "varchar(45)"),
                      ("stf_lnm", "varchar(45)"), ("addr_id", "integer"),
                      ("eml", "varchar(50)"), ("str_no", "integer"),
                      ("actv", "char(1)"), ("usr_nm", "varchar(16)")], pk=["stf_id"])
    db.insert("stf", ["stf_id", "stf_fnm", "stf_lnm", "addr_id", "eml", "str_no", "actv",
                      "usr_nm"], [
        (x["staff_id"], x["first_name"], x["last_name"], x["address_id"], x["email"],
         x["store_id"],
         # Magic flag values (#11): the same boolean written four ways.
         r.choice(["Y", "1"]) if x["active"] else r.choice(["N", "0"]),
         x["username"])
        for x in staff
    ])

    for phys, logical in [("str_loc", "store"), ("stf", "staff")]:
        m.rename(phys, logical)
        m.classify(phys, "core")
    m.key("str_loc", ["str_id"])
    m.key("stf", ["stf_id"])
    for tbl, col, ent, logical in [
        ("str_loc", "mgr_stf_no", "store", "manager_staff_id"),
        ("stf", "stf_fnm", "staff", "first_name"), ("stf", "stf_lnm", "staff", "last_name"),
        ("stf", "str_no", "staff", "store_id"), ("stf", "actv", "staff", "active"),
        ("stf", "eml", "staff", "email"),
    ]:
        m.col(tbl, col, ent, logical)
    m.enum("stf", "actv", {"Y": "active", "1": "active", "N": "inactive", "0": "inactive"})
    m.join(child="str_loc", child_cols=["mgr_stf_no"], parent="stf", parent_cols=["stf_id"])
    m.join(child="str_loc", child_cols=["addr_id"], parent="addr", parent_cols=["addr_id"])
    m.join(child="stf", child_cols=["str_no"], parent="str_loc", parent_cols=["str_id"])
    m.join(child="stf", child_cols=["addr_id"], parent="addr", parent_cols=["addr_id"])
    return {x["staff_id"]: x["store_id"] for x in staff}


def build_customers(src, db: PgLegacyDB, m: Manifest) -> None:
    r = rng("pagila-customer")
    rows = _read(src, "SELECT * FROM customer ORDER BY customer_id")

    db.create("cstmr", [
        ("cstmr_id", "integer"), ("str_no", "integer"), ("cfnm", "varchar(45)"),
        ("clnm", "varchar(45)"), ("addr_id", "integer"), ("eml", "varchar(60)"),
        ("sts_cd", "char(1)"), ("prfl", "text"), ("crt_dt", "varchar(10)"),
    ], pk=["cstmr_id"])

    payload = []
    for x in rows:
        cid = x["customer_id"]
        email = x["email"]
        # Drift and embedding at once (#6, #8): after a CRM migration, email lives only
        # inside the profile blob. The column is still there, empty, for newer rows.
        prfl: dict[str, Any] = {
            "nws": bool(cid % 3),
            "chnl": ["email", "sms", "post"][cid % 3],
        }
        if cid > EMAIL_CUTOVER_ID:
            prfl["eml"] = email
        payload.append((
            cid, x["store_id"], _pad(x["first_name"], 45), _pad(x["last_name"], 45),
            x["address_id"], email if cid <= EMAIL_CUTOVER_ID else None,
            "A" if x["active"] == 1 else "I",
            json_pack(prfl),
            x["create_date"].strftime("%d/%m/%Y"),
        ))

    # Soft-deleted customers (#12): extra rows with a status nobody documented.
    injected = 0
    for i in range(9):
        payload.append((
            9001 + i, 1 + i % 2, _pad(f"PURGED{i}", 45), _pad("ACCOUNT", 45),
            1 + i, None, "X", json_pack({"nws": False, "chnl": "none"}), "01/06/2022",
        ))
        injected += 1
    del r

    db.insert("cstmr", ["cstmr_id", "str_no", "cfnm", "clnm", "addr_id", "eml", "sts_cd",
                        "prfl", "crt_dt"], payload)
    db.comment("cstmr", "sts_cd", "Customer status")

    m.rename("cstmr", "customer")
    m.classify("cstmr", "core")
    m.key("cstmr", ["cstmr_id"])
    for col, logical in [("cfnm", "first_name"), ("clnm", "last_name"), ("str_no", "store_id"),
                         ("eml", "email"), ("sts_cd", "active"), ("crt_dt", "create_date")]:
        m.col("cstmr", col, "customer", logical)
    m.enum("cstmr", "sts_cd", STATUS_CODES)
    m.soft_delete("cstmr", style="status_code", predicate="sts_cd = 'X'",
                  live_predicate="sts_cd <> 'X'", injected_rows=injected)
    m.record_format(table="cstmr", column="prfl", format="json",
                    paths={"eml": "email", "nws": "newsletter", "chnl": "channel"})
    m.record_drift(table="cstmr", kind="column_relocation", old="cstmr.eml",
                   new="cstmr.prfl ->> 'eml'",
                   resolution="COALESCE(eml, prfl::json ->> 'eml')",
                   boundary=f"cstmr_id > {EMAIL_CUTOVER_ID}")
    m.record_type_mismatch(column="cstmr.crt_dt", stored="VARCHAR(10) DD/MM/YYYY",
                           matches="a date", fix="TO_DATE(crt_dt, 'DD/MM/YYYY')")
    m.record_quality(table="cstmr", column="cfnm", issue="char_padding", fix="RTRIM")
    m.record_quality(table="cstmr", column="clnm", issue="char_padding", fix="RTRIM")
    m.join(child="cstmr", child_cols=["addr_id"], parent="addr", parent_cols=["addr_id"])
    m.join(child="cstmr", child_cols=["str_no"], parent="str_loc", parent_cols=["str_id"])


# ---------------------------------------------------------------------------- catalogue


def build_films(src, db: PgLegacyDB, m: Manifest) -> None:
    """The film table arrived from a vendor system as a quoted, Hungarian-named import."""
    films = _read(src, "SELECT * FROM film ORDER BY film_id")

    db.create("tblFilm", [
        ('intFilmID', "integer"), ('strTitle', "varchar(255)"), ('strDescr', "text"),
        ('intRlsYr', "integer"), ('intLangID', "integer"), ('intRntlDur', "smallint"),
        ('curRntlRate', "numeric(4,2)"), ('intLen', "smallint"),
        ('curRplcCost', "numeric(5,2)"), ('bytRtg', "smallint"),
        ('strFeatures', "varchar(120)"), ('dtmDltd', "timestamp"),
    ], pk=["intFilmID"])
    payload = [
        (
            x["film_id"], x["title"], x["description"], x["release_year"], x["language_id"],
            x["rental_duration"], x["rental_rate"], x["length"], x["replacement_cost"],
            RATING_CODES[str(x["rating"])],
            # Embedded list (#8): an array flattened to a delimited string.
            "|".join(x["special_features"] or []) or None,
            None,
        )
        for x in films
    ]
    # Tombstoned films (#12): extra rows, deleted by timestamp rather than flag.
    retired = []
    for i in range(12):
        fid = 5001 + i
        retired.append(fid)
        payload.append((
            fid, f"WITHDRAWN TITLE {i:02d}", "Removed from catalogue", 2006, 1, 3,
            Decimal("0.99"), 90, Decimal("19.99"), 1 + i % 5, None,
            datetime(2022, 3, 1) + timedelta(days=i),
        ))
    db.insert("tblFilm", [
        "intFilmID", "strTitle", "strDescr", "intRlsYr", "intLangID", "intRntlDur",
        "curRntlRate", "intLen", "curRplcCost", "bytRtg", "strFeatures", "dtmDltd",
    ], payload)
    db.comment("tblFilm", "bytRtg", "MPAA rating")

    # Junctions with no declared key at all (#10): identity is the pair.
    db.create("film_actr", [("film_no", "integer"), ("actr_no", "integer")])
    fa = [(x["film_id"], x["actor_id"]) for x in
          _read(src, "SELECT * FROM film_actor ORDER BY film_id, actor_id")]
    # The retired films kept their cast rows, which is exactly what makes counting
    # films per actor wrong unless the tombstone is honoured.
    for i, fid in enumerate(retired):
        for k in range(4):
            fa.append((fid, 1 + (i * 17 + k * 29) % 200))
    db.insert("film_actr", ["film_no", "actr_no"], fa)

    db.create("film_ctgry", [("film_no", "integer"), ("ctgry_no", "integer")])
    fc = [(x["film_id"], x["category_id"]) for x in
          _read(src, "SELECT * FROM film_category ORDER BY film_id")]
    fc += [(fid, 1 + i % 16) for i, fid in enumerate(retired)]
    db.insert("film_ctgry", ["film_no", "ctgry_no"], fc)

    db.create("invtry", [("inv_id", "integer"), ("film_no", "integer"),
                         ("str_no", "integer")], pk=["inv_id"])
    db.insert("invtry", ["inv_id", "film_no", "str_no"], [
        (x["inventory_id"], x["film_id"], x["store_id"])
        for x in _read(src, "SELECT * FROM inventory ORDER BY inventory_id")
    ])

    m.rename("tblFilm", "film")
    m.rename("film_actr", "film_actor")
    m.rename("film_ctgry", "film_category")
    m.rename("invtry", "inventory")
    for phys in ("tblFilm", "film_actr", "film_ctgry", "invtry"):
        m.classify(phys, "core")
    m.key("tblFilm", ["intFilmID"])
    m.key("film_actr", ["film_no", "actr_no"])
    m.key("film_ctgry", ["film_no", "ctgry_no"])
    m.key("invtry", ["inv_id"])
    for col, logical in [
        ("intFilmID", "id"), ("strTitle", "title"), ("strDescr", "description"),
        ("intRlsYr", "release_year"), ("intLangID", "language_id"),
        ("intRntlDur", "rental_duration"), ("curRntlRate", "rental_rate"),
        ("intLen", "length"), ("curRplcCost", "replacement_cost"), ("bytRtg", "rating"),
        ("strFeatures", "special_features"),
    ]:
        m.col("tblFilm", col, "film", logical)
    m.enum("tblFilm", "bytRtg", {v: k for k, v in RATING_CODES.items()})
    m.soft_delete("tblFilm", style="tombstone", predicate='"dtmDltd" IS NOT NULL',
                  live_predicate='"dtmDltd" IS NULL', injected_rows=len(retired))
    m.record_format(table="tblFilm", column="strFeatures", format="delimited",
                    separator="|", meaning="the special_features array")
    m.join(child="tblFilm", child_cols=["intLangID"], parent="lang", parent_cols=["lang_id"])
    m.join(child="film_actr", child_cols=["film_no"], parent="tblFilm",
           parent_cols=["intFilmID"])
    m.join(child="film_actr", child_cols=["actr_no"], parent="actr", parent_cols=["actr_id"])
    m.join(child="film_ctgry", child_cols=["film_no"], parent="tblFilm",
           parent_cols=["intFilmID"])
    m.join(child="film_ctgry", child_cols=["ctgry_no"], parent="ctgry",
           parent_cols=["ctgry_id"])
    m.join(child="invtry", child_cols=["film_no"], parent="tblFilm",
           parent_cols=["intFilmID"])
    m.join(child="invtry", child_cols=["str_no"], parent="str_loc", parent_cols=["str_id"])
    m.scale_notes["tblFilm"] = (
        "Quoted mixed-case identifiers: unquoted, Postgres folds tblFilm to tblfilm and the "
        "relation does not exist. Generated SQL has to quote it."
    )


# ------------------------------------------------------------------------ transactions


def build_rentals(src, db: PgLegacyDB, m: Manifest) -> None:
    rows = _read(src, "SELECT * FROM rental ORDER BY rental_id")
    db.create("rntl_hdr", [
        ("rntl_id", "integer"), ("rntl_dt", "char(8)"), ("rntl_tm", "char(6)"),
        ("inv_no", "varchar(10)"), ("cstmr_no", "integer"), ("stf_no", "integer"),
        ("rtn_dt_txt", "varchar(10)"), ("rtn_ts", "timestamp"),
    ], pk=["rntl_id"])
    payload = []
    for x in rows:
        rented = _naive(x["rental_date"])
        returned = _naive(x["return_date"])
        day = rented.strftime("%Y%m%d")
        # Drift (#6): the return date moved from a text column to a timestamp.
        old = day < RETURN_CUTOVER
        payload.append((
            x["rental_id"], day, rented.strftime("%H%M%S"),
            zero_pad(x["inventory_id"], 10), x["customer_id"], x["staff_id"],
            returned.strftime("%d/%m/%Y") if (returned and old) else None,
            returned if (returned and not old) else None,
        ))
    db.insert("rntl_hdr", ["rntl_id", "rntl_dt", "rntl_tm", "inv_no", "cstmr_no", "stf_no",
                           "rtn_dt_txt", "rtn_ts"], payload)
    db.comment("rntl_hdr", "inv_no", "Inventory number")

    m.rename("rntl_hdr", "rental")
    m.classify("rntl_hdr", "core")
    m.key("rntl_hdr", ["rntl_id"])
    for col, logical in [("rntl_dt", "rental_date"), ("inv_no", "inventory_id"),
                         ("cstmr_no", "customer_id"), ("stf_no", "staff_id"),
                         ("rtn_ts", "return_date"), ("rtn_dt_txt", "return_date")]:
        m.col("rntl_hdr", col, "rental", logical)
    m.record_type_mismatch(column="rntl_hdr.inv_no", stored="VARCHAR(10) zero-padded",
                           matches="invtry.inv_id INTEGER", fix="CAST(inv_no AS INTEGER)")
    m.record_type_mismatch(column="rntl_hdr.rntl_dt", stored="CHAR(8) YYYYMMDD",
                           matches="a date", fix="TO_DATE(rntl_dt, 'YYYYMMDD')")
    m.record_drift(table="rntl_hdr", kind="column_replacement", old="rtn_dt_txt",
                   new="rtn_ts",
                   resolution="COALESCE(rtn_ts::date, TO_DATE(rtn_dt_txt, 'DD/MM/YYYY'))",
                   boundary=f"rntl_dt >= '{RETURN_CUTOVER}'")
    m.join(child="rntl_hdr", child_cols=["inv_no"], parent="invtry", parent_cols=["inv_id"],
           cast="CAST(rntl_hdr.inv_no AS INTEGER) = invtry.inv_id",
           note="zero-padded string against an integer key")
    m.join(child="rntl_hdr", child_cols=["cstmr_no"], parent="cstmr",
           parent_cols=["cstmr_id"])
    m.join(child="rntl_hdr", child_cols=["stf_no"], parent="stf", parent_cols=["stf_id"])


def build_transactions(src, db: PgLegacyDB, m: Manifest, store_of: dict[int, int]) -> None:
    """Anti-pattern #7: payments, refunds and late fees in one mostly-NULL table."""
    r = rng("pagila-fin")
    payments = _read(src, "SELECT * FROM payment ORDER BY payment_id")
    rentals = {x["rental_id"]: x for x in _read(
        src, "SELECT r.rental_id, r.rental_date, r.return_date, r.customer_id, r.staff_id, "
             "f.rental_duration FROM rental r JOIN inventory i USING (inventory_id) "
             "JOIN film f USING (film_id) ORDER BY r.rental_id")}

    # (store, ts, type order, source id, type, customer, staff, ref, amount, source)
    events: list[dict[str, Any]] = []
    for p in payments:
        events.append({
            "store": store_of[p["staff_id"]], "ts": _naive(p["payment_date"]), "typ": "P",
            "order": 0, "src": p["payment_id"], "cust": p["customer_id"],
            "staff": p["staff_id"], "ref": p["rental_id"], "amt": p["amount"],
        })
    # Refunds: extra rows that point back at the payment they reverse (#9 overload).
    for p in payments:
        if p["payment_id"] % 80 == 7:
            events.append({
                "store": store_of[p["staff_id"]],
                "ts": _naive(p["payment_date"]) + timedelta(days=2), "typ": "R",
                "order": 1, "src": p["payment_id"], "cust": p["customer_id"],
                "staff": p["staff_id"], "ref": None, "amt": -p["amount"],
                "refunds": p["payment_id"],
            })
    # Late fees: extra rows for rentals returned after their allowed duration.
    late = 0
    for rid, x in rentals.items():
        if late >= 320 or not x["return_date"]:
            continue
        overdue = (x["return_date"] - x["rental_date"]).days - x["rental_duration"]
        if overdue > 2:
            events.append({
                "store": store_of[x["staff_id"]], "ts": _naive(x["return_date"]), "typ": "A",
                "order": 2, "src": rid, "cust": x["customer_id"], "staff": x["staff_id"],
                "ref": rid, "amt": Decimal(min(overdue, 5)) * Decimal("1.00"),
            })
            late += 1

    # Transaction numbers restart per store (#10): identity is (str_cd, txn_no).
    events.sort(key=lambda e: (e["store"], e["ts"], e["order"], e["src"]))
    counters: dict[int, int] = {}
    txn_of_payment: dict[int, int] = {}
    for e in events:
        counters[e["store"]] = counters.get(e["store"], 0) + 1
        e["txn_no"] = counters[e["store"]]
        if e["typ"] == "P":
            txn_of_payment[e["src"]] = e["txn_no"]
    for e in events:
        if e["typ"] == "R":
            e["ref"] = txn_of_payment[e["refunds"]]

    base_cols = [
        ("str_cd", "char(3)"), ("txn_no", "integer"), ("txn_typ", "char(1)"),
        ("cstmr_no", "integer"), ("stf_no", "integer"), ("ref_no", "integer"),
        ("txn_ts", "timestamp"), ("amt", "numeric(7,2)"), ("amt_c", "integer"),
    ]
    all_cols = base_cols + GOD_TABLE_FILLER
    db.create("fin_txn", all_cols)

    payload = []
    for e in events:
        after = e["ts"] >= CENTS_CUTOVER
        filler: list[Any] = [None] * len(GOD_TABLE_FILLER)
        # A few filler columns are sparsely populated, as real ones are.
        if e["typ"] == "P":
            filler[0] = r.choice(["CC", "CASH", "DBT"])          # pmt_mthd_cd
            if r.random() < 0.1:
                filler[3] = r.randint(1000, 1099)                # batch_no
            filler[4] = r.choice(["POS", "POS", "WEB"])          # src_sys_cd
        if r.random() < 0.02:
            filler[19] = "DSC:" + r.choice(["SUMMER", "LOYAL5", "STAFF"])  # notes
        payload.append((
            f"S{e['store']:02d}", e["txn_no"], e["typ"], e["cust"], e["staff"], e["ref"],
            e["ts"],
            None if after else e["amt"],
            int((Decimal(e["amt"]) * 100).to_integral_value()) if after else None,
            *filler,
        ))
    db.insert("fin_txn", [c for c, _ in all_cols], payload)

    # Documentation, some of it wrong (#3).
    db.comment("fin_txn", None, "Financial transactions")
    db.comment("fin_txn", "amt", "Transaction amount in cents")
    db.comment("fin_txn", "txn_typ", "P = payment, R = refund")
    db.comment("fin_txn", "ref_no", "Rental reference")

    m.rename("fin_txn", "payment")
    m.classify("fin_txn", "core")
    m.record_god_table(
        table="fin_txn", discriminator="txn_typ", column_count=len(all_cols),
        entities={
            "payment": "txn_typ = 'P'",
            "refund": "txn_typ = 'R'",
            "adjustment": "txn_typ = 'A'",
        },
    )
    m.enum("fin_txn", "txn_typ", TXN_TYPES)
    m.key("fin_txn", ["str_cd", "txn_no"])
    for col, logical in [("cstmr_no", "customer_id"), ("stf_no", "staff_id"),
                         ("ref_no", "rental_id"), ("txn_ts", "payment_date"),
                         ("amt", "amount"), ("amt_c", "amount")]:
        m.col("fin_txn", col, "payment", logical)
    m.record_drift(
        table="fin_txn", kind="unit_change", old="amt (dollars)", new="amt_c (integer cents)",
        resolution=(f"CASE WHEN txn_ts >= '{CENTS_CUTOVER:%Y-%m-%d}' "
                    "THEN amt_c / 100.0 ELSE amt END"),
        boundary=f"txn_ts >= '{CENTS_CUTOVER:%Y-%m-%d}'",
    )
    m.record_overload(
        table="fin_txn", column="ref_no", discriminator="txn_typ",
        meanings={"P": "rntl_hdr.rntl_id", "A": "rntl_hdr.rntl_id",
                  "R": "fin_txn.txn_no of the payment refunded (same store)"},
    )
    m.record_overload(table="fin_txn", column="notes", pattern="DSC:<code>",
                      meaning="free-text note that also carries a discount code")
    m.join(child="fin_txn", child_cols=["cstmr_no"], parent="cstmr", parent_cols=["cstmr_id"])
    m.join(child="fin_txn", child_cols=["stf_no"], parent="stf", parent_cols=["stf_id"])
    m.join(child="fin_txn", child_cols=["ref_no"], parent="rntl_hdr", parent_cols=["rntl_id"],
           condition="fin_txn.txn_typ IN ('P', 'A')", note="overloaded reference column")
    m.scale_notes["fin_txn_comments"] = (
        "Misleading documentation on purpose: amt is commented as cents and holds dollars; "
        "the txn_typ comment omits 'A'. Only the data says otherwise."
    )
    m.scale_notes["fin_txn_rows"] = (
        f"{sum(1 for e in events if e['typ']=='P')} payments, "
        f"{sum(1 for e in events if e['typ']=='R')} refunds, "
        f"{sum(1 for e in events if e['typ']=='A')} late fees"
    )


# ------------------------------------------------------------------ sprawl and artifacts


def build_sprawl(db: PgLegacyDB, m: Manifest) -> None:
    """Anti-patterns #4 and #5: tables nobody has deleted, some of them plausible."""
    r = rng("pagila-sprawl")

    for year in (2020, 2021):
        name = f"rntl_hdr_{year}"
        db.exec(f"CREATE TABLE {name} AS SELECT * FROM rntl_hdr WHERE 1 = 0")
        m.classify(name, "archive")

    # A stale backup of the god table, taken before the cents cutover: plausible, and
    # wrong for any question about the whole period.
    db.exec(
        "CREATE TABLE fin_txn_bak_20220315 AS SELECT str_cd, txn_no, txn_typ, cstmr_no, "
        "stf_no, ref_no, txn_ts, amt FROM fin_txn WHERE txn_ts < '2022-03-15'"
    )
    m.classify("fin_txn_bak_20220315", "backup")
    db.exec("CREATE TABLE cstmr_old AS SELECT cstmr_id, cfnm, clnm FROM cstmr WHERE 1 = 0")
    m.classify("cstmr_old", "backup")

    # Staging: a partial duplicate load of payments.
    db.exec(
        "CREATE TABLE stg_pmt_load AS SELECT str_cd, txn_no, cstmr_no, txn_ts, "
        "COALESCE(amt, amt_c / 100.0) AS amt FROM fin_txn "
        "WHERE txn_typ = 'P' AND txn_no % 7 = 0"
    )
    db.exec("CREATE TABLE stg_film_feed AS SELECT \"intFilmID\", \"strTitle\" FROM "
            "\"tblFilm\" LIMIT 40")
    for name in ("stg_pmt_load", "stg_film_feed"):
        m.classify(name, "staging")

    # A report snapshot computed before anyone knew about the cents column: the
    # numbers in it are wrong from April 2022 on.
    db.exec(
        "CREATE TABLE rpt_mthly_rev AS SELECT to_char(txn_ts, 'YYYY-MM') AS mth, "
        "SUM(amt) AS rev FROM fin_txn WHERE txn_typ = 'P' GROUP BY 1"
    )
    m.classify("rpt_mthly_rev", "archive")

    for name in ("tmp_dedupe_cust", "tmp_fix_20220405", "zz_scratch", "xref_unused"):
        db.exec(f"CREATE TABLE {name} (id integer, val varchar(80), note varchar(200))")
        m.classify(name, "dead")

    prefixes = [("stg_", "staging"), ("rpt_", "archive"), ("tmp_", "dead"),
                ("wrk_", "dead"), ("bak_", "backup"), ("hist_", "archive")]
    subjects = ["cust", "rntl", "film", "inv", "gl", "ar"]
    for i in range(60):
        prefix, klass = prefixes[i % len(prefixes)]
        name = f"{prefix}{subjects[i % len(subjects)]}_{i:03d}"
        cols = ", ".join(f"f{j:02d} varchar(40)" for j in range(r.randint(3, 8)))
        db.exec(f"CREATE TABLE {name} (id integer, {cols})")
        if r.random() < 0.25:
            db.exec(f"INSERT INTO {name} (id) SELECT generate_series(1, {r.randint(1, 5)})")
        m.classify(name, klass)


def build_views(db: PgLegacyDB, m: Manifest) -> None:
    """Shipped SQL: views that encode some of the rules, and one that is out of date."""
    db.exec("CREATE VIEW v_actv_cstmr AS SELECT * FROM cstmr WHERE sts_cd = 'A'")
    db.exec(
        "CREATE VIEW v_rntl_film AS SELECT r.rntl_id, r.rntl_dt, r.cstmr_no, "
        "f.\"intFilmID\", f.\"strTitle\" FROM rntl_hdr r "
        "JOIN invtry i ON CAST(r.inv_no AS INTEGER) = i.inv_id "
        "JOIN \"tblFilm\" f ON f.\"intFilmID\" = i.film_no"
    )
    # Written before the cents cutover and never updated: sums the dollar column only.
    db.exec(
        "CREATE VIEW v_cstmr_rev_old AS SELECT cstmr_no, SUM(amt) AS rev "
        "FROM fin_txn WHERE txn_typ = 'P' GROUP BY cstmr_no"
    )
    for name in ("v_actv_cstmr", "v_rntl_film"):
        m.classify(name, "core")
    m.classify("v_cstmr_rev_old", "dead")


def build_event_log(db: PgLegacyDB, m: Manifest) -> None:
    """Anti-pattern #16: a large log with no index on the column people filter by."""
    db.create("evt_log", [
        ("log_id", "bigint"), ("evt_ts", "timestamp"), ("usr_cd", "varchar(12)"),
        ("tbl_nm", "varchar(30)"), ("actn_cd", "char(1)"), ("rec_id", "integer"),
        ("old_val", "text"), ("new_val", "text"),
    ], pk=["log_id"])
    # Generated server-side and deterministically: no random(), so two builds match.
    db.exec(
        f"INSERT INTO evt_log SELECT g, timestamp '2021-01-01' + g * interval '97 seconds', "
        f"'USR' || lpad((g % 37)::text, 3, '0'), "
        f"(array['cstmr','rntl_hdr','fin_txn','tblFilm','invtry'])[1 + g % 5], "
        f"(array['I','U','D'])[1 + g % 3], g % 16000, "
        f"CASE WHEN g % 3 = 1 THEN 'v' || (g % 101) END, 'v' || (g % 103) "
        f"FROM generate_series(1, {EVT_LOG_ROWS}) g"
    )
    m.rename("evt_log", "audit_log")
    m.classify("evt_log", "core")
    m.key("evt_log", ["log_id"])
    m.record_perf_trap(table="evt_log", rows=EVT_LOG_ROWS, missing_index_on="evt_ts",
                       note="any date-bounded question over this table is a full scan")


# ------------------------------------------------------------------------------ driver


def legacify(out_dir: Path) -> Path:
    import psycopg

    from gauntlet import pg

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "pagila_legacy.manifest.json"

    # The clean copy is rebuilt from the pinned dump every time, so the gold answers
    # and the legacy copy always derive from the same bytes.
    pg.load_clean("pagila_clean")
    legacy_uri = pg.recreate("pagila_legacy")

    src = psycopg.connect(pg.uri("pagila_clean"))
    db = PgLegacyDB(legacy_uri)
    m = Manifest(database="pagila_legacy", dialect="postgres", source="Pagila v3.1.0")
    m.scale_notes["treatment"] = (
        "The 'moderate' Pagila treatment from PLAN.md 3.1, plus anti-pattern #3 "
        "(misleading comments), which only a database with comments can carry."
    )

    build_reference(src, db, m)
    store_of = build_stores_and_staff(src, db, m)
    build_customers(src, db, m)
    build_films(src, db, m)
    build_rentals(src, db, m)
    build_transactions(src, db, m, store_of)
    build_sprawl(db, m)
    build_views(db, m)
    build_event_log(db, m)

    db.finish()
    src.close()
    m.write(manifest_path)
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacify the Pagila database.")
    here = Path(__file__).resolve().parents[1]
    parser.add_argument("--out", type=Path, default=here / "build")
    args = parser.parse_args()

    manifest = legacify(args.out)

    import psycopg

    from gauntlet import pg

    with psycopg.connect(pg.uri("pagila_legacy")) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        ).fetchone()[0]
        views = conn.execute(
            "SELECT count(*) FROM information_schema.views WHERE table_schema = 'public'"
        ).fetchone()[0]
        size = conn.execute(
            "SELECT pg_size_pretty(pg_database_size('pagila_legacy'))"
        ).fetchone()[0]
    print(f"wrote pagila_legacy  ({size}, {tables} tables, {views} views)")
    print(f"wrote {manifest}")


if __name__ == "__main__":
    main()
