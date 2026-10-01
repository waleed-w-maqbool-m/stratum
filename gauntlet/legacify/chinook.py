"""Legacify Chinook (SQLite).

Reads the pristine Chinook database and writes `chinook_legacy.sqlite` — the same
facts, buried under the anti-patterns catalogued in docs/PLAN.md §3.2 — plus the
`manifest.json` answer key.

The plan lists Chinook as a "light" treatment. It is heavier than that here, and
deliberately so: SQL Server is the flagship but needs Docker, whereas this database
builds anywhere in about twenty seconds. Making it exercise fourteen of the sixteen
anti-patterns is what lets the acceptance checks for phases 2-5 actually run. The
two it cannot exercise are #3 (SQLite has no column comments) and the SQL Server
half of #15 (covered instead by the artificial `legacy_strict` profile).

Invariant that keeps the gold answers valid: **every transformation is reversible.**
Soft-deleted rows are *extra* rows that do not exist in the clean database, never
existing rows hidden, so filtering them out reproduces the clean set exactly.

Usage:
    python -m gauntlet.legacify.chinook [--source PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .common import (
    COUNTRY_VARIANTS,
    NULL_MARKERS,
    LegacyDB,
    Manifest,
    as_datetime,
    ddmmyyyy,
    json_pack,
    kv_pack,
    pad_char,
    read_table,
    rng,
    to_cents,
    xml_pack,
    yyyymmdd,
    zero_pad,
)

# The date after which money is stored in cents instead of dollars (anti-pattern #6).
CENTS_CUTOVER = "20230101"

# Which store a billing country belongs to. Order numbers restart inside each store,
# which is what makes (STORE_CD, ORD_NO) the real key (anti-pattern #10).
STORE_BY_COUNTRY = {
    "USA": "US1",
    "Canada": "CA1",
    "Brazil": "BR1",
}
DEFAULT_STORE = "EU1"

ORD_STS_ENUM = {
    1: "pending",
    2: "approved",
    3: "shipped",
    4: "delivered",
    5: "returned",
    6: "closed",
    9: "cancelled",
}

PMT_MTHD_ENUM = {"CC": "credit card", "CH": "cheque", "WT": "wire transfer", "CA": "cash"}
RTN_RSN_ENUM = {1: "damaged", 2: "wrong item", 3: "customer changed mind", 4: "duplicate"}

AUDIT_LOG_ROWS = 250_000

# Filler columns for the god table: real legacy god tables are mostly NULL.
GOD_TABLE_FILLER = [
    ("SHIP_MTHD_CD", "VARCHAR(4)"), ("SHIP_DT_TXT", "CHAR(8)"), ("TRK_NBR", "VARCHAR(30)"),
    ("CARR_CD", "VARCHAR(6)"), ("FRT_AMT", "FLOAT"), ("TAX_AMT", "FLOAT"),
    ("DSC_AMT", "FLOAT"), ("CRNCY_CD", "CHAR(3)"), ("FX_RT", "FLOAT"),
    ("PO_NBR", "VARCHAR(20)"), ("TERMS_CD", "VARCHAR(6)"), ("DUE_DT_TXT", "CHAR(8)"),
    ("APPRV_BY", "VARCHAR(12)"), ("APPRV_DT_TXT", "CHAR(8)"), ("BATCH_NO", "INTEGER"),
    ("SRC_SYS_CD", "VARCHAR(6)"), ("IMPT_ID", "INTEGER"), ("RECON_FLG", "CHAR(1)"),
    ("GL_ACCT", "VARCHAR(12)"), ("CC_CD", "VARCHAR(8)"), ("PRJ_CD", "VARCHAR(8)"),
    ("WHSE_CD", "VARCHAR(4)"), ("ROUTE_CD", "VARCHAR(6)"), ("PRIORITY_NO", "INTEGER"),
    ("HOLD_FLG", "CHAR(1)"), ("HOLD_RSN", "VARCHAR(40)"), ("XPORT_FLG", "CHAR(1)"),
    ("XPORT_DT_TXT", "CHAR(8)"), ("LST_UPD_BY", "VARCHAR(12)"), ("LST_UPD_TS", "VARCHAR(20)"),
]


def _mangle_country(value: str | None, r) -> str | None:
    """Anti-pattern #14: the same country written five different ways."""
    if not value:
        return value
    variants = COUNTRY_VARIANTS.get(value)
    return r.choice(variants) if variants else value


def _null_marker(value: str | None, r, rate: float = 0.35) -> str | None:
    """Anti-pattern #14: NULL spelled 'N/A', '-', '' or 'UNKNOWN'."""
    if value is None or value == "":
        return r.choice(NULL_MARKERS) if r.random() < 0.8 else None
    if r.random() < rate * 0.1:
        return r.choice(NULL_MARKERS)
    return value


def _mixed_case(value: str | None, r) -> str | None:
    if not value:
        return value
    roll = r.random()
    if roll < 0.18:
        return value.upper()
    if roll < 0.30:
        return value.lower()
    return value


# --------------------------------------------------------------------------- builders


def build_customers(src: sqlite3.Connection, db: LegacyDB, m: Manifest) -> dict[int, dict]:
    rows = read_table(src, "Customer")
    m.rename("CUST_MSTR", "customer")
    m.classify("CUST_MSTR", "core")

    db.create(
        "CUST_MSTR",
        [
            ("CUST_ID", "INTEGER"),
            ("CFNM1", "CHAR(20)"),
            ("CLNM1", "CHAR(20)"),
            ("CO_NM", "VARCHAR(80)"),
            ("ADDR_XML", "TEXT"),
            ("PHONE", "VARCHAR(24)"),
            ("PHN_NBR", "VARCHAR(24)"),
            ("FAX_NBR", "VARCHAR(24)"),
            ("EML", "VARCHAR(60)"),
            ("REP_NO", "VARCHAR(8)"),
            ("DEL_FLG", "CHAR(1)"),
            ("PREFS", "TEXT"),
            ("CRT_DT", "VARCHAR(19)"),
        ],
        pk=["CUST_ID"],
    )
    for phys, logical in [
        ("CUST_ID", "id"), ("CFNM1", "first_name"), ("CLNM1", "last_name"),
        ("CO_NM", "company"), ("PHONE", "phone"), ("PHN_NBR", "phone"),
        ("FAX_NBR", "fax"), ("EML", "email"), ("REP_NO", "support_rep_id"),
    ]:
        m.col("CUST_MSTR", phys, "customer", logical)

    r = rng("customer")
    out: dict[int, dict] = {}
    payload = []
    contacts = []
    contact_id = 1

    for row in rows:
        cid = row["CustomerId"]
        out[cid] = row
        # Anti-pattern #8: the whole address collapsed into an XML blob.
        addr_xml = xml_pack(
            "a",
            {
                "s1": row["Address"],
                "c": _mixed_case(row["City"], r),
                "st": row["State"],
                "cy": _mangle_country(row["Country"], r),
                "pc": row["PostalCode"],
            },
        )
        # Anti-pattern #6: PHONE deprecated in favour of PHN_NBR partway through.
        old_phone = row["Phone"] if cid <= 40 else None
        new_phone = row["Phone"] if cid > 40 else None
        # Anti-pattern #6 again: email migrated out to CUST_CNTCT partway through.
        inline_email = row["Email"] if cid <= 30 else None
        if cid > 30 and row["Email"]:
            contacts.append((contact_id, cid, "EMAIL", row["Email"]))
            contact_id += 1

        prefs = json_pack(
            {
                "newsletter": bool(cid % 3),
                "channel": ["email", "post", "phone"][cid % 3],
                "tier": ["bronze", "silver", "gold"][cid % 3],
            }
        )
        payload.append(
            (
                cid,
                pad_char(row["FirstName"], 20),
                pad_char(row["LastName"], 20),
                _null_marker(row["Company"], r),
                addr_xml,
                old_phone,
                new_phone,
                row["Fax"],
                inline_email,
                zero_pad(row["SupportRepId"], 8),
                r.choice(["N", None, "n"]),
                prefs,
                "2021-01-01 00:00:00",
            )
        )

    # Anti-pattern #12: soft deletes are EXTRA rows, so excluding them reproduces clean.
    injected = 0
    for i in range(7):
        cid = 900 + i
        payload.append(
            (
                cid,
                pad_char(f"Deleted{i}", 20),
                pad_char("Account", 20),
                "N/A",
                xml_pack("a", {"c": "Nowhere", "cy": "US"}),
                None,
                "000-000-0000",
                None,
                f"deleted{i}@example.invalid",
                zero_pad(3, 8),
                "Y",
                json_pack({"newsletter": False}),
                "2023-06-01 00:00:00",
            )
        )
        injected += 1

    db.insert(
        "CUST_MSTR",
        [
            "CUST_ID", "CFNM1", "CLNM1", "CO_NM", "ADDR_XML", "PHONE", "PHN_NBR",
            "FAX_NBR", "EML", "REP_NO", "DEL_FLG", "PREFS", "CRT_DT",
        ],
        payload,
    )

    db.create(
        "CUST_CNTCT",
        [
            ("CNTCT_ID", "INTEGER"),
            ("CUST_ID", "INTEGER"),
            ("CNTCT_TYP", "VARCHAR(8)"),
            ("CNTCT_VAL", "VARCHAR(120)"),
        ],
        pk=["CNTCT_ID"],
    )
    db.insert("CUST_CNTCT", ["CNTCT_ID", "CUST_ID", "CNTCT_TYP", "CNTCT_VAL"], contacts)
    m.classify("CUST_CNTCT", "core")
    m.rename("CUST_CNTCT", "customer_contact")
    m.key("CUST_CNTCT", ["CNTCT_ID"])

    m.soft_delete(
        "CUST_MSTR",
        style="flag",
        predicate="UPPER(COALESCE(DEL_FLG,'N')) = 'Y'",
        live_predicate="UPPER(COALESCE(DEL_FLG,'N')) <> 'Y'",
        injected_rows=injected,
    )
    m.record_format(
        table="CUST_MSTR", column="ADDR_XML", format="xml",
        paths={"s1": "address", "c": "city", "st": "state", "cy": "country", "pc": "postal_code"},
    )
    m.record_format(
        table="CUST_MSTR", column="PREFS", format="json",
        paths={"newsletter": "newsletter", "channel": "channel", "tier": "tier"},
    )
    m.record_drift(
        table="CUST_MSTR", kind="column_replacement", old="PHONE", new="PHN_NBR",
        resolution="COALESCE(PHN_NBR, PHONE)", boundary="CUST_ID > 40",
    )
    m.record_drift(
        table="CUST_MSTR", kind="column_relocation", old="CUST_MSTR.EML",
        new="CUST_CNTCT.CNTCT_VAL WHERE CNTCT_TYP='EMAIL'",
        resolution="COALESCE(EML, lookup in CUST_CNTCT)", boundary="CUST_ID > 30",
    )
    m.record_quality(
        table="CUST_MSTR", column="CFNM1", issue="char_padding", fix="RTRIM",
    )
    m.record_quality(
        table="CUST_MSTR", column="CO_NM", issue="null_markers",
        markers=NULL_MARKERS, fix="NULLIF on each marker",
    )
    m.record_quality(
        table="CUST_MSTR", column="ADDR_XML/cy", issue="country_variants",
        variants=COUNTRY_VARIANTS, fix="canonicalise to the clean spelling",
    )
    m.record_type_mismatch(
        column="CUST_MSTR.REP_NO", stored="VARCHAR(8) zero-padded",
        matches="EMP_MSTR.EMP_ID INTEGER", fix="CAST(REP_NO AS INTEGER)",
    )
    m.join(
        child="CUST_MSTR", child_cols=["REP_NO"], parent="EMP_MSTR", parent_cols=["EMP_ID"],
        cast="CAST(CUST_MSTR.REP_NO AS INTEGER) = EMP_MSTR.EMP_ID",
        note="support rep; zero-padded string against an integer key",
    )
    m.join(
        child="CUST_CNTCT", child_cols=["CUST_ID"], parent="CUST_MSTR", parent_cols=["CUST_ID"],
        note="contact rows for customers created after the email migration",
    )
    m.key("CUST_MSTR", ["CUST_ID"])
    return out


def build_employees(src: sqlite3.Connection, db: LegacyDB, m: Manifest) -> list[int]:
    rows = read_table(src, "Employee")
    m.rename("EMP_MSTR", "employee")
    m.classify("EMP_MSTR", "core")
    db.create(
        "EMP_MSTR",
        [
            ("EMP_ID", "INTEGER"),
            ("ELNM", "CHAR(20)"),
            ("EFNM", "CHAR(20)"),
            ("TTL", "VARCHAR(40)"),
            ("MGR_ID", "INTEGER"),
            ("BRTH_DT", "CHAR(8)"),
            ("HIRE_DT", "VARCHAR(10)"),
            ("ADDR1", "VARCHAR(70)"),
            ("CTY", "VARCHAR(40)"),
            ("ST", "VARCHAR(40)"),
            ("CTRY", "VARCHAR(40)"),
            ("PSTL", "VARCHAR(10)"),
            ("EPHN", "VARCHAR(24)"),
            ("EFAX", "VARCHAR(24)"),
            ("EEML", "VARCHAR(60)"),
            ("STS", "INTEGER"),
        ],
        pk=["EMP_ID"],
    )
    for phys, logical in [
        ("EMP_ID", "id"), ("ELNM", "last_name"), ("EFNM", "first_name"), ("TTL", "title"),
        ("MGR_ID", "reports_to"), ("BRTH_DT", "birth_date"), ("HIRE_DT", "hire_date"),
        ("CTY", "city"), ("ST", "state"), ("CTRY", "country"), ("EEML", "email"),
    ]:
        m.col("EMP_MSTR", phys, "employee", logical)

    r = rng("employee")
    payload = []
    for row in rows:
        payload.append(
            (
                row["EmployeeId"],
                pad_char(row["LastName"], 20),
                pad_char(row["FirstName"], 20),
                row["Title"],
                row["ReportsTo"],
                yyyymmdd(row["BirthDate"]),        # CHAR(8) YYYYMMDD
                ddmmyyyy(row["HireDate"]),         # VARCHAR DD/MM/YYYY — a second format
                row["Address"],
                _mixed_case(row["City"], r),
                row["State"],
                _mangle_country(row["Country"], r),
                row["PostalCode"],
                row["Phone"],
                row["Fax"],
                row["Email"],
                1,
            )
        )
    for i in range(2):
        eid = 90 + i
        payload.append(
            (
                eid, pad_char(f"Former{i}", 20), pad_char("Staff", 20), "IT Staff", 6,
                "19700101", "01/01/2005", None, "Calgary", "AB", "Canada", None,
                None, None, f"former{i}@chinookcorp.invalid", 9,
            )
        )

    db.insert(
        "EMP_MSTR",
        [
            "EMP_ID", "ELNM", "EFNM", "TTL", "MGR_ID", "BRTH_DT", "HIRE_DT", "ADDR1",
            "CTY", "ST", "CTRY", "PSTL", "EPHN", "EFAX", "EEML", "STS",
        ],
        payload,
    )
    m.soft_delete(
        "EMP_MSTR", style="status_code", predicate="STS = 9", live_predicate="STS <> 9",
        injected_rows=2,
    )
    m.enum("EMP_MSTR", "STS", {1: "active", 9: "terminated"})
    m.record_type_mismatch(
        column="EMP_MSTR.BRTH_DT", stored="CHAR(8) YYYYMMDD", matches="a date",
        fix="DATE(SUBSTR(BRTH_DT,1,4)||'-'||SUBSTR(BRTH_DT,5,2)||'-'||SUBSTR(BRTH_DT,7,2))",
    )
    m.record_type_mismatch(
        column="EMP_MSTR.HIRE_DT", stored="VARCHAR DD/MM/YYYY", matches="a date",
        fix="DATE(SUBSTR(HIRE_DT,7,4)||'-'||SUBSTR(HIRE_DT,4,2)||'-'||SUBSTR(HIRE_DT,1,2))",
    )
    m.join(
        child="EMP_MSTR", child_cols=["MGR_ID"], parent="EMP_MSTR", parent_cols=["EMP_ID"],
        note="self-referencing management chain",
    )
    m.key("EMP_MSTR", ["EMP_ID"])
    return [row["EmployeeId"] for row in rows]


def build_catalog(src: sqlite3.Connection, db: LegacyDB, m: Manifest) -> None:
    r = rng("catalog")

    # --- Artist -> ARTST, Album -> ALBM, Genre -> GNR, MediaType -> MEDIA_TYP
    for phys, clean, cols, pk, entity in [
        ("ARTST", "Artist", [("ARTST_ID", "INTEGER"), ("ARTST_NM", "VARCHAR(120)")],
         "ARTST_ID", "artist"),
        ("ALBM", "Album",
         [("ALBM_ID", "INTEGER"), ("ALBM_TTL", "VARCHAR(160)"), ("ARTST_ID", "INTEGER")],
         "ALBM_ID", "album"),
        ("GNR", "Genre", [("GNR_ID", "INTEGER"), ("GNR_NM", "VARCHAR(120)")],
         "GNR_ID", "genre"),
        ("MEDIA_TYP", "MediaType", [("MTYP_ID", "INTEGER"), ("MTYP_NM", "VARCHAR(120)")],
         "MTYP_ID", "media_type"),
    ]:
        # Anti-pattern #2: PKs survive on only about half the tables.
        keep_pk = phys in ("ARTST", "GNR")
        db.create(phys, cols, pk=[pk] if keep_pk else ())
        rows = read_table(src, clean)
        values = [tuple(row[k] for k in row) for row in rows]
        db.insert(phys, [c[0] for c in cols], values)
        m.rename(phys, entity)
        m.classify(phys, "core")
        m.key(phys, [pk])

    m.col("ARTST", "ARTST_NM", "artist", "name")
    m.col("ALBM", "ALBM_TTL", "album", "title")
    m.col("GNR", "GNR_NM", "genre", "name")
    m.col("MEDIA_TYP", "MTYP_NM", "media_type", "name")
    m.join(child="ALBM", child_cols=["ARTST_ID"], parent="ARTST", parent_cols=["ARTST_ID"])

    # --- Track -> tblPrd (anti-pattern #1, Hungarian notation)
    tracks = read_table(src, "Track")
    genres = {g["GenreId"]: g["Name"] for g in read_table(src, "Genre")}
    media = {t["MediaTypeId"]: t["Name"] for t in read_table(src, "MediaType")}

    db.create(
        "tblPrd",
        [
            ("PRD_ID", "INTEGER"),
            ("strNm", "VARCHAR(200)"),
            ("ALBM_ID", "INTEGER"),
            ("MTYP_ID", "INTEGER"),
            ("GNR_ID", "INTEGER"),
            ("strCmpsr", "VARCHAR(220)"),
            ("intMs", "INTEGER"),
            ("intByt", "INTEGER"),
            ("fltPrc", "FLOAT"),
            ("ATTRS", "TEXT"),
            ("DLT_DT", "VARCHAR(19)"),
        ],
        pk=["PRD_ID"],
    )
    payload = []
    for row in tracks:
        attrs = kv_pack(
            {
                "genre": genres.get(row["GenreId"], ""),
                "media": media.get(row["MediaTypeId"], ""),
                "secs": (row["Milliseconds"] or 0) // 1000,
            }
        )
        payload.append(
            (
                row["TrackId"], row["Name"], row["AlbumId"], row["MediaTypeId"],
                row["GenreId"], _null_marker(row["Composer"], r), row["Milliseconds"],
                row["Bytes"], row["UnitPrice"], attrs, None,
            )
        )
    # Anti-pattern #12, fourth style: discontinued products get a ZZ_ name prefix
    # and a tombstone date. Extra rows again, never existing ones.
    injected = 0
    for i in range(45):
        pid = 90000 + i
        payload.append(
            (
                pid, f"ZZ_Discontinued Track {i}", 1, 1, 1, "N/A", 100000, 1000000,
                0.99, "genre=Rock|media=MPEG audio file|secs=100", "2023-04-01 00:00:00",
            )
        )
        injected += 1

    db.insert(
        "tblPrd",
        [
            "PRD_ID", "strNm", "ALBM_ID", "MTYP_ID", "GNR_ID", "strCmpsr", "intMs",
            "intByt", "fltPrc", "ATTRS", "DLT_DT",
        ],
        payload,
    )
    m.rename("tblPrd", "track")
    m.classify("tblPrd", "core")
    m.key("tblPrd", ["PRD_ID"])
    for phys, logical in [
        ("PRD_ID", "id"), ("strNm", "name"), ("ALBM_ID", "album_id"),
        ("MTYP_ID", "media_type_id"), ("GNR_ID", "genre_id"), ("strCmpsr", "composer"),
        ("intMs", "milliseconds"), ("intByt", "bytes"), ("fltPrc", "unit_price"),
    ]:
        m.col("tblPrd", phys, "track", logical)
    m.soft_delete(
        "tblPrd", style="tombstone_date_and_name_prefix",
        predicate="DLT_DT IS NOT NULL OR strNm LIKE 'ZZ!_%' ESCAPE '!'",
        live_predicate="DLT_DT IS NULL",
        injected_rows=injected,
    )
    m.record_format(
        table="tblPrd", column="ATTRS", format="kv",
        separators={"pair": "|", "assign": "="},
        paths={"genre": "genre_name", "media": "media_type_name", "secs": "duration_seconds"},
    )
    m.join(child="tblPrd", child_cols=["ALBM_ID"], parent="ALBM", parent_cols=["ALBM_ID"])
    m.join(child="tblPrd", child_cols=["GNR_ID"], parent="GNR", parent_cols=["GNR_ID"])
    m.join(child="tblPrd", child_cols=["MTYP_ID"], parent="MEDIA_TYP", parent_cols=["MTYP_ID"])

    # --- Playlist
    db.create("PLST", [("PLST_ID", "INTEGER"), ("PLST_NM", "VARCHAR(120)")], pk=["PLST_ID"])
    db.insert(
        "PLST", ["PLST_ID", "PLST_NM"],
        [(p["PlaylistId"], p["Name"]) for p in read_table(src, "Playlist")],
    )
    db.create("PLST_TRK", [("PLST_ID", "INTEGER"), ("PRD_ID", "INTEGER")])  # no PK at all
    db.insert(
        "PLST_TRK", ["PLST_ID", "PRD_ID"],
        [(p["PlaylistId"], p["TrackId"]) for p in read_table(src, "PlaylistTrack")],
    )
    m.rename("PLST", "playlist")
    m.rename("PLST_TRK", "playlist_track")
    m.classify("PLST", "core")
    m.classify("PLST_TRK", "core")
    m.col("PLST", "PLST_NM", "playlist", "name")
    m.key("PLST", ["PLST_ID"])
    m.key("PLST_TRK", ["PLST_ID", "PRD_ID"])   # implicit composite key, never declared
    m.join(child="PLST_TRK", child_cols=["PLST_ID"], parent="PLST", parent_cols=["PLST_ID"])
    m.join(child="PLST_TRK", child_cols=["PRD_ID"], parent="tblPrd", parent_cols=["PRD_ID"])


def build_transactions(src: sqlite3.Connection, db: LegacyDB, m: Manifest,
                       employees: list[int]) -> None:
    """The god table (#7) plus the order lines with an implicit composite key (#10)."""
    invoices = read_table(src, "Invoice")
    lines = read_table(src, "InvoiceLine")
    r = rng("transactions")

    base_cols: list[tuple[str, str]] = [
        ("TRANS_TYP_CD", "CHAR(1)"),
        ("STORE_CD", "CHAR(3)"),
        ("ORD_NO", "INTEGER"),
        ("CUST_NO", "VARCHAR(10)"),
        ("ORD_DT_TXT", "CHAR(8)"),
        ("ORD_TS", "VARCHAR(20)"),
        ("TOT_AMT", "FLOAT"),
        ("TOT_AMT_C", "INTEGER"),
        ("ORD_STS", "INTEGER"),
        ("REF_ID", "INTEGER"),
        ("REF_TYP", "CHAR(1)"),
        ("NOTES", "VARCHAR(200)"),
        ("BILL_ADDR1", "VARCHAR(70)"),
        ("BILL_CTY", "VARCHAR(40)"),
        ("BILL_ST", "VARCHAR(40)"),
        ("BILL_CTRY", "VARCHAR(40)"),
        ("BILL_PC", "VARCHAR(10)"),
        ("PMT_AMT", "FLOAT"),
        ("PMT_DT_TXT", "CHAR(8)"),
        ("PMT_MTHD_CD", "VARCHAR(4)"),
        ("RTN_AMT", "FLOAT"),
        ("RTN_RSN_CD", "INTEGER"),
    ]
    all_cols = base_cols + GOD_TABLE_FILLER
    db.create("TRANS_MSTR", all_cols)          # no PK declared (anti-pattern #2)
    col_names = [c[0] for c in all_cols]
    filler_nulls = tuple(None for _ in GOD_TABLE_FILLER)

    # Assign each invoice to a store, then number orders inside that store from 1.
    store_counters: dict[str, int] = {}
    invoice_key: dict[int, tuple[str, int]] = {}
    for inv in sorted(invoices, key=lambda x: x["InvoiceId"]):
        store = STORE_BY_COUNTRY.get(inv["BillingCountry"], DEFAULT_STORE)
        store_counters[store] = store_counters.get(store, 0) + 1
        invoice_key[inv["InvoiceId"]] = (store, store_counters[store])

    payload: list[tuple[Any, ...]] = []
    discount_codes = ["SUMMER10", "LOYAL5", "WINBACK20", "STAFF15"]

    for inv in sorted(invoices, key=lambda x: x["InvoiceId"]):
        store, ord_no = invoice_key[inv["InvoiceId"]]
        dt = as_datetime(inv["InvoiceDate"])
        dt_txt = yyyymmdd(inv["InvoiceDate"])
        total = float(inv["Total"])
        # Anti-pattern #6: dollars before the cutover, integer cents on and after it.
        pre_cutover = dt_txt < CENTS_CUTOVER
        notes = None
        if r.random() < 0.18:
            notes = f"DSC:{r.choice(discount_codes)}"
        elif r.random() < 0.10:
            notes = "reprint requested"

        payload.append(
            (
                "I", store, ord_no,
                zero_pad(inv["CustomerId"], 10),          # #13: zero-padded string key
                dt_txt,
                dt.strftime("%d/%m/%Y %H:%M") if dt else None,   # a third date format
                round(total, 2) if pre_cutover else None,
                None if pre_cutover else to_cents(total),
                r.choice([2, 3, 3, 4, 4, 4, 6]),          # #11: magic status numbers
                inv["CustomerId"], "C",                   # #9: REF_ID/REF_TYP overloading
                notes,
                inv["BillingAddress"],
                _mixed_case(inv["BillingCity"], r),
                _null_marker(inv["BillingState"], r),
                _mangle_country(inv["BillingCountry"], r),
                inv["BillingPostalCode"],
                None, None, None, None, None,
            )
            + filler_nulls
        )

    # Payment rows: same table, different discriminator, a disjoint set of populated columns.
    for inv in sorted(invoices, key=lambda x: x["InvoiceId"]):
        if r.random() > 0.92:
            continue
        store, ord_no = invoice_key[inv["InvoiceId"]]
        dt = as_datetime(inv["InvoiceDate"])
        pay_dt = (dt + timedelta(days=r.randint(0, 21))) if dt else None
        payload.append(
            (
                "P", store, ord_no, zero_pad(inv["CustomerId"], 10),
                yyyymmdd(pay_dt), None, None, None, None,
                r.choice(employees), "E",                 # #9: now REF_ID means an employee
                None, None, None, None, None, None,
                round(float(inv["Total"]), 2), yyyymmdd(pay_dt),
                r.choice(list(PMT_MTHD_ENUM)),
                None, None,
            )
            + filler_nulls
        )

    # Refund rows.
    for inv in sorted(invoices, key=lambda x: x["InvoiceId"])[:40]:
        if r.random() > 0.7:
            continue
        store, ord_no = invoice_key[inv["InvoiceId"]]
        dt = as_datetime(inv["InvoiceDate"])
        payload.append(
            (
                "R", store, ord_no, zero_pad(inv["CustomerId"], 10),
                yyyymmdd(dt), None, None, None, 5,
                inv["CustomerId"], "C", "return processed",
                None, None, None, None, None, None, None, None,
                round(float(inv["Total"]) / 2, 2), r.choice(list(RTN_RSN_ENUM)),
            )
            + filler_nulls
        )

    # Anti-pattern #12: cancelled invoices are soft deletes — extra rows, status 9.
    injected = 0
    for i in range(18):
        store = ["US1", "CA1", "EU1", "BR1"][i % 4]
        store_counters[store] = store_counters.get(store, 0) + 1
        payload.append(
            (
                "I", store, store_counters[store], zero_pad(1 + (i % 59), 10),
                "20230115", "15/01/2023 09:00", None, 999, 9,
                1 + (i % 59), "C", "cancelled by customer",
                "1 Nowhere St", "Nowhere", "N/A", "US", "00000",
                None, None, None, None, None,
            )
            + filler_nulls
        )
        injected += 1

    db.insert("TRANS_MSTR", col_names, payload)

    # --- order lines, keyed only by convention
    db.create(
        "SO_LN",
        [
            ("STORE_CD", "CHAR(3)"),
            ("ORD_NO", "INTEGER"),
            ("LN_SEQ", "INTEGER"),
            ("PRD_ID", "INTEGER"),
            ("UNT_PRC", "FLOAT"),
            ("QTY", "INTEGER"),
        ],
    )  # deliberately no PRIMARY KEY
    seq: dict[int, int] = {}
    line_rows = []
    for ln in sorted(lines, key=lambda x: x["InvoiceLineId"]):
        inv_id = ln["InvoiceId"]
        seq[inv_id] = seq.get(inv_id, 0) + 1
        store, ord_no = invoice_key[inv_id]
        line_rows.append(
            (store, ord_no, seq[inv_id], ln["TrackId"], float(ln["UnitPrice"]), ln["Quantity"])
        )
    db.insert("SO_LN", ["STORE_CD", "ORD_NO", "LN_SEQ", "PRD_ID", "UNT_PRC", "QTY"], line_rows)

    # ---------------------------------------------------------------- manifest entries
    m.rename("TRANS_MSTR", "order")
    m.rename("SO_LN", "order_line")
    m.classify("TRANS_MSTR", "core")
    m.classify("SO_LN", "core")
    m.record_god_table(
        table="TRANS_MSTR",
        discriminator="TRANS_TYP_CD",
        column_count=len(all_cols),
        entities={
            "order": "TRANS_TYP_CD = 'I'",
            "payment": "TRANS_TYP_CD = 'P'",
            "refund": "TRANS_TYP_CD = 'R'",
        },
    )
    m.enum("TRANS_MSTR", "ORD_STS", ORD_STS_ENUM)
    m.enum("TRANS_MSTR", "TRANS_TYP_CD", {"I": "invoice", "P": "payment", "R": "refund"})
    m.enum("TRANS_MSTR", "PMT_MTHD_CD", PMT_MTHD_ENUM)
    m.enum("TRANS_MSTR", "RTN_RSN_CD", RTN_RSN_ENUM)
    m.enum("TRANS_MSTR", "REF_TYP", {"C": "customer", "E": "employee"})
    m.soft_delete(
        "TRANS_MSTR", style="status_code", predicate="ORD_STS = 9",
        live_predicate="ORD_STS <> 9 OR ORD_STS IS NULL", injected_rows=injected,
    )
    m.key("TRANS_MSTR", ["STORE_CD", "ORD_NO", "TRANS_TYP_CD"])
    m.scale_notes["TRANS_MSTR_key"] = (
        "TRANS_MSTR carries no surrogate key on purpose: identity is (STORE_CD, ORD_NO) "
        "within a transaction type, and nothing in the schema declares it."
    )
    m.key("SO_LN", ["STORE_CD", "ORD_NO", "LN_SEQ"])
    m.record_drift(
        table="TRANS_MSTR", kind="unit_change", old="TOT_AMT (dollars)",
        new="TOT_AMT_C (integer cents)",
        resolution=(
            f"CASE WHEN ORD_DT_TXT >= '{CENTS_CUTOVER}' "
            "THEN TOT_AMT_C / 100.0 ELSE TOT_AMT END"
        ),
        boundary=f"ORD_DT_TXT >= '{CENTS_CUTOVER}'",
    )
    m.record_overload(
        table="TRANS_MSTR", column="REF_ID", discriminator="REF_TYP",
        meanings={"C": "CUST_MSTR.CUST_ID", "E": "EMP_MSTR.EMP_ID"},
    )
    m.record_overload(
        table="TRANS_MSTR", column="NOTES", pattern="DSC:<code>",
        meaning="free-text note that also carries a discount code",
    )
    m.record_type_mismatch(
        column="TRANS_MSTR.CUST_NO", stored="VARCHAR(10) zero-padded",
        matches="CUST_MSTR.CUST_ID INTEGER", fix="CAST(CUST_NO AS INTEGER)",
    )
    m.record_type_mismatch(
        column="TRANS_MSTR.ORD_DT_TXT", stored="CHAR(8) YYYYMMDD", matches="a date",
        fix="DATE(SUBSTR(ORD_DT_TXT,1,4)||'-'||SUBSTR(ORD_DT_TXT,5,2)||'-'||SUBSTR(ORD_DT_TXT,7,2))",
    )
    m.join(
        child="TRANS_MSTR", child_cols=["CUST_NO"], parent="CUST_MSTR", parent_cols=["CUST_ID"],
        cast="CAST(TRANS_MSTR.CUST_NO AS INTEGER) = CUST_MSTR.CUST_ID",
        note="zero-padded string against an integer key",
    )
    m.join(
        child="TRANS_MSTR", child_cols=["REF_ID"], parent="CUST_MSTR", parent_cols=["CUST_ID"],
        condition="TRANS_MSTR.REF_TYP = 'C'", note="overloaded reference column",
    )
    m.join(
        child="TRANS_MSTR", child_cols=["REF_ID"], parent="EMP_MSTR", parent_cols=["EMP_ID"],
        condition="TRANS_MSTR.REF_TYP = 'E'", note="overloaded reference column",
    )
    m.join(
        child="SO_LN", child_cols=["STORE_CD", "ORD_NO"], parent="TRANS_MSTR",
        parent_cols=["STORE_CD", "ORD_NO"],
        condition="TRANS_MSTR.TRANS_TYP_CD = 'I'",
        note="composite join; order numbers restart per store",
    )
    m.join(child="SO_LN", child_cols=["PRD_ID"], parent="tblPrd", parent_cols=["PRD_ID"])


def build_sprawl(db: LegacyDB, m: Manifest) -> None:
    """Anti-patterns #4 and #5: hundreds of tables nobody has deleted."""
    r = rng("sprawl")

    # Yearly archive slices of the god table.
    for year in range(2021, 2026):
        name = f"SO_HDR_{year}"
        db.exec(
            f"CREATE TABLE [{name}] AS SELECT STORE_CD, ORD_NO, CUST_NO, "
            f"ORD_DT_TXT, TOT_AMT, TOT_AMT_C, ORD_STS FROM TRANS_MSTR "
            f"WHERE TRANS_TYP_CD = 'I' AND SUBSTR(ORD_DT_TXT,1,4) = '{year}'"
        )
        m.classify(name, "archive")

    # Staging tables: near-duplicate schemas, partial or stale data.
    db.exec(
        "CREATE TABLE [STG_CUST_LOAD] AS SELECT CUST_ID, CFNM1, CLNM1, CO_NM, REP_NO "
        "FROM CUST_MSTR WHERE CUST_ID <= 20"
    )
    db.exec(
        "CREATE TABLE [STG_TRANS_IN] AS SELECT STORE_CD, ORD_NO, CUST_NO, "
        "ORD_DT_TXT FROM TRANS_MSTR WHERE 1 = 0"
    )
    db.exec("CREATE TABLE [STG_PRD_FEED] AS SELECT PRD_ID, strNm, fltPrc FROM tblPrd LIMIT 50")
    for name in ("STG_CUST_LOAD", "STG_TRANS_IN", "STG_PRD_FEED"):
        m.classify(name, "staging")

    # Report snapshots.
    db.exec(
        "CREATE TABLE [RPT_SALES_SNAP_2024] AS SELECT STORE_CD, "
        "COUNT(*) AS ORD_CNT, SUM(COALESCE(TOT_AMT, TOT_AMT_C/100.0)) AS AMT "
        "FROM TRANS_MSTR WHERE TRANS_TYP_CD='I' AND SUBSTR(ORD_DT_TXT,1,4)='2024' "
        "GROUP BY STORE_CD"
    )
    db.exec(
        "CREATE TABLE [RPT_TOP_ARTST] AS SELECT a.ARTST_ID, a.ARTST_NM, COUNT(*) AS N "
        "FROM ARTST a JOIN ALBM b ON b.ARTST_ID = a.ARTST_ID GROUP BY a.ARTST_ID, a.ARTST_NM"
    )
    for name in ("RPT_SALES_SNAP_2024", "RPT_TOP_ARTST"):
        m.classify(name, "archive")

    # Temp tables somebody left behind.
    for name in ("tmp_fix_cust", "tmp_dedupe_1", "tmp_jb_20230104"):
        db.exec(f"CREATE TABLE [{name}] (ID INTEGER, VAL VARCHAR(80), NOTE VARCHAR(200))")
        m.classify(name, "dead")

    # Backup copies: one stale-but-populated, one empty.
    db.exec(
        "CREATE TABLE [CUST_MSTR_BAK_20240312] AS SELECT CUST_ID, CFNM1, CLNM1, CO_NM, "
        "PHONE, EML, DEL_FLG FROM CUST_MSTR"
    )
    db.exec("UPDATE [CUST_MSTR_BAK_20240312] SET CO_NM = 'N/A' WHERE CUST_ID % 4 = 0")
    db.exec("DELETE FROM [CUST_MSTR_BAK_20240312] WHERE CUST_ID > 50")
    db.exec(
        "CREATE TABLE [CUST_MSTR_OLD] AS SELECT CUST_ID, CFNM1, CLNM1 FROM CUST_MSTR WHERE 1=0"
    )
    m.classify("CUST_MSTR_BAK_20240312", "backup")
    m.classify("CUST_MSTR_OLD", "backup")

    # Empty tables that were created and never used.
    for name in ("XREF_UNUSED", "LOOKUP_TBD", "WRK_HOLD", "CONV_MAP_V2", "ZZ_SCRATCH"):
        db.exec(f"CREATE TABLE [{name}] (ID INTEGER, CD VARCHAR(10), DESCR VARCHAR(120))")
        m.classify(name, "dead")

    # Bulk filler so retrieval has to actually discriminate.
    prefixes = [
        ("STG_", "staging"), ("RPT_", "archive"), ("tmp_", "dead"),
        ("WRK_", "dead"), ("BAK_", "backup"), ("HIST_", "archive"),
        ("IFC_", "staging"), ("CNV_", "dead"),
    ]
    for i in range(96):
        prefix, klass = prefixes[i % len(prefixes)]
        name = f"{prefix}{['CUST','ORD','PRD','INV','GL','AR','AP','HR'][i % 8]}_{i:03d}"
        ncols = r.randint(3, 9)
        cols = ", ".join(f"[F{j:02d}] VARCHAR(40)" for j in range(ncols))
        db.exec(f"CREATE TABLE [{name}] ([ID] INTEGER, {cols})")
        if r.random() < 0.25:
            db.exec(
                f"INSERT INTO [{name}] ([ID]) VALUES "
                + ", ".join(f"({k})" for k in range(r.randint(1, 5)))
            )
        m.classify(name, klass)


def build_views(db: LegacyDB, m: Manifest) -> None:
    """Anti-pattern #3's consolation prize.

    Nobody documented the schema, but somebody wrote views. Those views encode the
    god-table discriminator, the cast join, the status enum and the soft-delete rule —
    which is exactly what `procmine.py` is there to harvest.
    """
    db.exec(
        """
        CREATE VIEW V_ORD_SUMMARY AS
        SELECT t.STORE_CD, t.ORD_NO, t.ORD_DT_TXT,
               CASE t.ORD_STS
                    WHEN 1 THEN 'Pending'   WHEN 2 THEN 'Approved'
                    WHEN 3 THEN 'Shipped'   WHEN 4 THEN 'Delivered'
                    WHEN 5 THEN 'Returned'  WHEN 6 THEN 'Closed'
                    WHEN 9 THEN 'Cancelled' ELSE 'Unknown' END AS STS_DESC,
               RTRIM(c.CFNM1) AS FIRST_NM, RTRIM(c.CLNM1) AS LAST_NM
        FROM TRANS_MSTR t
        JOIN CUST_MSTR c ON c.CUST_ID = CAST(t.CUST_NO AS INTEGER)
        WHERE t.TRANS_TYP_CD = 'I' AND t.ORD_STS <> 9
        """
    )
    db.exec(
        """
        CREATE VIEW V_CUST_ACTIVE AS
        SELECT CUST_ID, RTRIM(CFNM1) AS FIRST_NM, RTRIM(CLNM1) AS LAST_NM,
               COALESCE(PHN_NBR, PHONE) AS PHONE_NBR
        FROM CUST_MSTR
        WHERE COALESCE(DEL_FLG, 'N') <> 'Y'
        """
    )
    db.exec(
        """
        CREATE VIEW V_PRD_LIVE AS
        SELECT PRD_ID, strNm AS PRD_NM, fltPrc AS PRC
        FROM tblPrd
        WHERE DLT_DT IS NULL
        """
    )
    db.exec(
        """
        CREATE VIEW V_ORD_LINE_EXT AS
        SELECT l.STORE_CD, l.ORD_NO, l.LN_SEQ, p.strNm AS PRD_NM,
               l.UNT_PRC * l.QTY AS LN_AMT
        FROM SO_LN l
        JOIN tblPrd p ON p.PRD_ID = l.PRD_ID
        """
    )
    # Anti-pattern #5: a view referencing a column that no longer exists.
    db.exec(
        "CREATE VIEW V_CUST_ACTIVE_OLD AS "
        "SELECT CUST_ID, CUST_STATUS_CD, LEGACY_SEGMENT FROM CUST_MSTR WHERE DEL_FLG <> 'Y'"
    )
    for name in ("V_ORD_SUMMARY", "V_CUST_ACTIVE", "V_PRD_LIVE", "V_ORD_LINE_EXT"):
        m.classify(name, "core")
    m.classify("V_CUST_ACTIVE_OLD", "dead")
    m.scale_notes["broken_views"] = "V_CUST_ACTIVE_OLD references dropped columns and cannot run"


def build_audit_log(db: LegacyDB, m: Manifest) -> None:
    """Anti-pattern #16: a big table with no index on the column everyone filters by."""
    r = rng("audit")
    db.create(
        "AUDIT_LOG",
        [
            ("LOG_ID", "INTEGER"),
            ("EVT_DT", "VARCHAR(19)"),
            ("USR_CD", "VARCHAR(12)"),
            ("TBL_NM", "VARCHAR(30)"),
            ("ACTN_CD", "CHAR(1)"),
            ("REC_ID", "INTEGER"),
            ("OLD_VAL", "VARCHAR(60)"),
            ("NEW_VAL", "VARCHAR(60)"),
        ],
    )
    users = [f"USR{i:03d}" for i in range(40)]
    tables = ["CUST_MSTR", "TRANS_MSTR", "tblPrd", "SO_LN", "EMP_MSTR"]
    actions = ["I", "U", "D"]
    batch = []
    for i in range(1, AUDIT_LOG_ROWS + 1):
        day = 1 + (i % 1800)
        year = 2021 + day // 365
        month = 1 + (day % 12)
        dom = 1 + (day % 28)
        batch.append(
            (
                i,
                f"{year:04d}-{month:02d}-{dom:02d} {i % 24:02d}:{i % 60:02d}:00",
                users[i % len(users)],
                tables[i % len(tables)],
                actions[i % 3],
                i % 5000,
                f"v{i % 997}",
                f"v{(i + 1) % 997}",
            )
        )
        if len(batch) >= 20000:
            db.insert(
                "AUDIT_LOG",
                ["LOG_ID", "EVT_DT", "USR_CD", "TBL_NM", "ACTN_CD", "REC_ID", "OLD_VAL", "NEW_VAL"],
                batch,
            )
            batch.clear()
    if batch:
        db.insert(
            "AUDIT_LOG",
            ["LOG_ID", "EVT_DT", "USR_CD", "TBL_NM", "ACTN_CD", "REC_ID", "OLD_VAL", "NEW_VAL"],
            batch,
        )
    del r
    m.classify("AUDIT_LOG", "core")
    m.key("AUDIT_LOG", ["LOG_ID"])
    m.record_perf_trap(
        table="AUDIT_LOG", rows=AUDIT_LOG_ROWS, missing_index_on="EVT_DT",
        note="any date-bounded question over this table is a full scan",
    )
    m.scale_notes["AUDIT_LOG"] = (
        f"{AUDIT_LOG_ROWS:,} rows rather than the 8M specified for HALCYON, so that the "
        "SQLite build stays under a minute and the file under ~25 MB. The cost guard is "
        "exercised the same way: no index exists on EVT_DT."
    )


# ------------------------------------------------------------------------------ driver


def legacify(source: Path, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_copy = out_dir / "chinook_clean.sqlite"
    shutil.copyfile(source, clean_copy)

    legacy_path = out_dir / "chinook_legacy.sqlite"
    manifest_path = out_dir / "chinook_legacy.manifest.json"

    src = sqlite3.connect(clean_copy)
    db = LegacyDB(legacy_path)
    m = Manifest(database="chinook_legacy", dialect="sqlite", source="Chinook")
    m.scale_notes["treatment"] = (
        "Heavier than the 'light' treatment in PLAN.md 3.1: this is the only gauntlet "
        "database that builds without Docker, so it carries 14 of the 16 anti-patterns "
        "in order to give phases 2-5 a runnable acceptance check."
    )

    build_customers(src, db, m)
    employees = build_employees(src, db, m)
    build_catalog(src, db, m)
    build_transactions(src, db, m, employees)
    build_sprawl(db, m)
    build_views(db, m)
    build_audit_log(db, m)

    db.finish()
    src.close()
    m.write(manifest_path)
    return legacy_path, manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacify the Chinook database.")
    here = Path(__file__).resolve().parents[1]
    parser.add_argument("--source", type=Path, default=here / "sources" / "chinook_clean.sqlite")
    parser.add_argument("--out", type=Path, default=here / "build")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(
            f"clean source not found at {args.source}\n"
            "run: python -m gauntlet.sources.fetch chinook"
        )
    legacy, manifest = legacify(args.source, args.out)
    size_mb = legacy.stat().st_size / 1e6
    conn = sqlite3.connect(legacy)
    tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    views = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0]
    conn.close()
    print(f"wrote {legacy}  ({size_mb:.1f} MB, {tables} tables, {views} views)")
    print(f"wrote {manifest}")


if __name__ == "__main__":
    main()
