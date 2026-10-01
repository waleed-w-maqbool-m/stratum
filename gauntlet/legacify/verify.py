"""Reversibility check for a legacified database.

The gauntlet only works if every injection can be undone: gold SQL runs on the clean
copy, the system's SQL runs on the legacy copy, and the two must be able to agree.
This asserts that a hand-written "perfect semantic model" recovers the clean facts
exactly. If this fails, the legacifier destroyed information and the suite is invalid.

This file is part of the gauntlet, not the engine. It is allowed to know the mapping;
engine code is not.

    python -m gauntlet.legacify.verify
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .common import COUNTRY_VARIANTS


def _country_case(column: str) -> str:
    """Build the canonicalising CASE from the same table the legacifier mangles with,
    so the check can never drift out of sync with the injection."""
    whens = []
    for canonical, variants in COUNTRY_VARIANTS.items():
        for variant in variants:
            whens.append(f"WHEN '{variant.upper()}' THEN '{canonical}'")
    return f"CASE UPPER(TRIM({column})) {' '.join(whens)} ELSE {column} END"


# Each check is (label, sql_on_clean, sql_on_legacy). The two must return the same rows.
CHECKS: list[tuple[str, str, str]] = [
    (
        "customer count",
        "SELECT COUNT(*) FROM Customer",
        "SELECT COUNT(*) FROM CUST_MSTR WHERE UPPER(COALESCE(DEL_FLG,'N')) <> 'Y'",
    ),
    (
        "customer names",
        "SELECT FirstName, LastName FROM Customer ORDER BY CustomerId",
        "SELECT RTRIM(CFNM1), RTRIM(CLNM1) FROM CUST_MSTR "
        "WHERE UPPER(COALESCE(DEL_FLG,'N')) <> 'Y' ORDER BY CUST_ID",
    ),
    (
        "customer phone (column drift)",
        "SELECT CustomerId, Phone FROM Customer ORDER BY CustomerId",
        "SELECT CUST_ID, COALESCE(PHN_NBR, PHONE) FROM CUST_MSTR "
        "WHERE UPPER(COALESCE(DEL_FLG,'N')) <> 'Y' ORDER BY CUST_ID",
    ),
    (
        "customer email (table relocation)",
        "SELECT CustomerId, Email FROM Customer ORDER BY CustomerId",
        "SELECT c.CUST_ID, COALESCE(c.EML, (SELECT x.CNTCT_VAL FROM CUST_CNTCT x "
        "  WHERE x.CUST_ID = c.CUST_ID AND x.CNTCT_TYP = 'EMAIL')) "
        "FROM CUST_MSTR c WHERE UPPER(COALESCE(c.DEL_FLG,'N')) <> 'Y' ORDER BY c.CUST_ID",
    ),
    (
        "customer city (xml extraction)",
        "SELECT CustomerId, UPPER(City) FROM Customer ORDER BY CustomerId",
        "SELECT CUST_ID, UPPER(SUBSTR(ADDR_XML, INSTR(ADDR_XML,'<c>')+3, "
        "  INSTR(ADDR_XML,'</c>') - INSTR(ADDR_XML,'<c>') - 3)) "
        "FROM CUST_MSTR WHERE UPPER(COALESCE(DEL_FLG,'N')) <> 'Y' ORDER BY CUST_ID",
    ),
    (
        "customer support rep (cast join)",
        "SELECT c.CustomerId, e.LastName FROM Customer c "
        "JOIN Employee e ON e.EmployeeId = c.SupportRepId ORDER BY c.CustomerId",
        "SELECT c.CUST_ID, RTRIM(e.ELNM) FROM CUST_MSTR c "
        "JOIN EMP_MSTR e ON e.EMP_ID = CAST(c.REP_NO AS INTEGER) "
        "WHERE UPPER(COALESCE(c.DEL_FLG,'N')) <> 'Y' AND e.STS <> 9 ORDER BY c.CUST_ID",
    ),
    (
        "employee count",
        "SELECT COUNT(*) FROM Employee",
        "SELECT COUNT(*) FROM EMP_MSTR WHERE STS <> 9",
    ),
    (
        "employee hire dates (dd/mm/yyyy)",
        "SELECT EmployeeId, DATE(HireDate) FROM Employee ORDER BY EmployeeId",
        "SELECT EMP_ID, SUBSTR(HIRE_DT,7,4)||'-'||SUBSTR(HIRE_DT,4,2)||'-'||SUBSTR(HIRE_DT,1,2) "
        "FROM EMP_MSTR WHERE STS <> 9 ORDER BY EMP_ID",
    ),
    (
        "track count",
        "SELECT COUNT(*) FROM Track",
        "SELECT COUNT(*) FROM tblPrd WHERE DLT_DT IS NULL",
    ),
    (
        "track revenue basis",
        "SELECT ROUND(SUM(UnitPrice),2) FROM Track",
        "SELECT ROUND(SUM(fltPrc),2) FROM tblPrd WHERE DLT_DT IS NULL",
    ),
    (
        "track genre (kv extraction)",
        "SELECT t.TrackId, g.Name FROM Track t JOIN Genre g ON g.GenreId = t.GenreId "
        "ORDER BY t.TrackId LIMIT 200",
        "SELECT PRD_ID, SUBSTR(ATTRS, 7, INSTR(ATTRS,'|media=') - 7) FROM tblPrd "
        "WHERE DLT_DT IS NULL AND GNR_ID IS NOT NULL ORDER BY PRD_ID LIMIT 200",
    ),
    (
        "invoice count (god table + soft delete)",
        "SELECT COUNT(*) FROM Invoice",
        "SELECT COUNT(*) FROM TRANS_MSTR WHERE TRANS_TYP_CD = 'I' AND ORD_STS <> 9",
    ),
    (
        "invoice total (unit drift)",
        "SELECT ROUND(SUM(Total),2) FROM Invoice",
        "SELECT ROUND(SUM(CASE WHEN ORD_DT_TXT >= '20230101' "
        "  THEN TOT_AMT_C / 100.0 ELSE TOT_AMT END),2) "
        "FROM TRANS_MSTR WHERE TRANS_TYP_CD = 'I' AND ORD_STS <> 9",
    ),
    (
        "invoices per customer (cast join)",
        "SELECT c.CustomerId, COUNT(*) FROM Customer c JOIN Invoice i "
        "  ON i.CustomerId = c.CustomerId GROUP BY c.CustomerId ORDER BY c.CustomerId",
        "SELECT c.CUST_ID, COUNT(*) FROM CUST_MSTR c JOIN TRANS_MSTR t "
        "  ON CAST(t.CUST_NO AS INTEGER) = c.CUST_ID "
        "WHERE t.TRANS_TYP_CD = 'I' AND t.ORD_STS <> 9 "
        "  AND UPPER(COALESCE(c.DEL_FLG,'N')) <> 'Y' "
        "GROUP BY c.CUST_ID ORDER BY c.CUST_ID",
    ),
    (
        "invoice dates (char(8) yyyymmdd)",
        "SELECT DATE(InvoiceDate), COUNT(*) FROM Invoice GROUP BY DATE(InvoiceDate) "
        "ORDER BY 1",
        "SELECT SUBSTR(ORD_DT_TXT,1,4)||'-'||SUBSTR(ORD_DT_TXT,5,2)||'-'"
        "||SUBSTR(ORD_DT_TXT,7,2), COUNT(*) FROM TRANS_MSTR "
        "WHERE TRANS_TYP_CD = 'I' AND ORD_STS <> 9 GROUP BY 1 ORDER BY 1",
    ),
    (
        "order line count (composite key)",
        "SELECT COUNT(*) FROM InvoiceLine",
        "SELECT COUNT(*) FROM SO_LN",
    ),
    (
        "order line revenue",
        "SELECT ROUND(SUM(UnitPrice * Quantity),2) FROM InvoiceLine",
        "SELECT ROUND(SUM(UNT_PRC * QTY),2) FROM SO_LN",
    ),
    (
        "lines joined to invoices (composite join)",
        "SELECT COUNT(*), ROUND(SUM(l.UnitPrice * l.Quantity),2) FROM InvoiceLine l "
        "JOIN Invoice i ON i.InvoiceId = l.InvoiceId",
        "SELECT COUNT(*), ROUND(SUM(l.UNT_PRC * l.QTY),2) FROM SO_LN l "
        "JOIN TRANS_MSTR t ON t.STORE_CD = l.STORE_CD AND t.ORD_NO = l.ORD_NO "
        "  AND t.TRANS_TYP_CD = 'I' WHERE t.ORD_STS <> 9",
    ),
    (
        "revenue by country (value canonicalisation)",
        "SELECT BillingCountry, ROUND(SUM(Total),2) FROM Invoice "
        "GROUP BY BillingCountry ORDER BY 1",
        "SELECT {country_case}, "
        "  ROUND(SUM(CASE WHEN ORD_DT_TXT >= '20230101' "
        "    THEN TOT_AMT_C / 100.0 ELSE TOT_AMT END),2) "
        "FROM TRANS_MSTR WHERE TRANS_TYP_CD = 'I' AND ORD_STS <> 9 GROUP BY 1 ORDER BY 1",
    ),
    (
        "playlist track count",
        "SELECT COUNT(*) FROM PlaylistTrack",
        "SELECT COUNT(*) FROM PLST_TRK",
    ),
    (
        "album count per artist",
        "SELECT ar.Name, COUNT(*) FROM Artist ar JOIN Album al ON al.ArtistId = ar.ArtistId "
        "GROUP BY ar.Name ORDER BY 1",
        "SELECT a.ARTST_NM, COUNT(*) FROM ARTST a JOIN ALBM b ON b.ARTST_ID = a.ARTST_ID "
        "GROUP BY a.ARTST_NM ORDER BY 1",
    ),
]


def _norm(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def _run(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(_norm(v) for v in row) for row in conn.execute(sql)]


def verify(clean: Path, legacy: Path, verbose: bool = False) -> int:
    cc = sqlite3.connect(clean)
    lc = sqlite3.connect(legacy)
    failures = 0
    for label, clean_sql, legacy_sql in CHECKS:
        legacy_sql = legacy_sql.replace("{country_case}", _country_case("BILL_CTRY"))
        try:
            expected = _run(cc, clean_sql)
            actual = _run(lc, legacy_sql)
        except sqlite3.Error as exc:  # noqa: PERF203 - one message per failing check
            print(f"  ERROR  {label}: {exc}")
            failures += 1
            continue
        if expected == actual:
            print(f"  ok     {label}  ({len(expected)} rows)")
        else:
            failures += 1
            print(f"  FAIL   {label}")
            diff = [(e, a) for e, a in zip(expected, actual, strict=False) if e != a]
            print(f"         {len(expected)} expected rows vs {len(actual)} actual")
            for e, a in diff[:5]:
                print(f"         clean={e!r}  legacy={a!r}")
            if verbose:
                print(f"         clean sql:  {clean_sql}")
                print(f"         legacy sql: {legacy_sql}")
    cc.close()
    lc.close()
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} reversibility checks passed")
    return failures


def main() -> int:
    here = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", type=Path, default=here / "build" / "chinook_clean.sqlite")
    parser.add_argument("--legacy", type=Path, default=here / "build" / "chinook_legacy.sqlite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return 1 if verify(args.clean, args.legacy, args.verbose) else 0


if __name__ == "__main__":
    sys.exit(main())
