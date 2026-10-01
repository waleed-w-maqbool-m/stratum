"""Reversibility check for `pagila_legacy`.

The same contract as `verify.py` for Chinook: every injected anti-pattern must be
undoable, or the gold answers computed on `pagila_clean` are not answers to anything
the legacy copy can be asked. Each check pairs a query on the clean database with the
query a *perfect* semantic model would compile for the legacy one, and the two must
return identical rows.

Part of the gauntlet, not the engine. It is allowed to know the mapping.

    python -m gauntlet.legacify.verify_pagila
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any

LIVE_CUSTOMER = "sts_cd <> 'X'"
LIVE_FILM = '"dtmDltd" IS NULL'
AMOUNT = "CASE WHEN txn_ts >= '2022-04-01' THEN amt_c / 100.0 ELSE amt END"
RENTAL_DAY = "TO_DATE(rntl_dt, 'YYYYMMDD')"
RETURN_DAY = "COALESCE(rtn_ts::date, TO_DATE(rtn_dt_txt, 'DD/MM/YYYY'))"
NULL_MARKED = "NULLIF(NULLIF(NULLIF(NULLIF(TRIM({c}), ''), 'N/A'), '-'), 'UNKNOWN')"
RATING = ("CASE \"bytRtg\" WHEN 1 THEN 'G' WHEN 2 THEN 'PG' WHEN 3 THEN 'PG-13' "
          "WHEN 4 THEN 'R' WHEN 5 THEN 'NC-17' END")

# (label, sql on pagila_clean, sql on pagila_legacy)
CHECKS: list[tuple[str, str, str]] = [
    ("customer count (status-code soft delete)",
     "SELECT COUNT(*) FROM customer",
     f"SELECT COUNT(*) FROM cstmr WHERE {LIVE_CUSTOMER}"),
    ("active customers (magic status code)",
     "SELECT COUNT(*) FROM customer WHERE active = 1",
     "SELECT COUNT(*) FROM cstmr WHERE sts_cd = 'A'"),
    ("customer names (padding in varchar)",
     "SELECT customer_id, first_name, last_name FROM customer ORDER BY 1",
     f"SELECT cstmr_id, RTRIM(cfnm), RTRIM(clnm) FROM cstmr WHERE {LIVE_CUSTOMER} ORDER BY 1"),
    ("customer email (relocated into JSON)",
     "SELECT customer_id, email FROM customer ORDER BY 1",
     f"SELECT cstmr_id, COALESCE(eml, prfl::json ->> 'eml') FROM cstmr "
     f"WHERE {LIVE_CUSTOMER} ORDER BY 1"),
    ("customer create date (DD/MM/YYYY text)",
     "SELECT customer_id, create_date FROM customer ORDER BY 1",
     f"SELECT cstmr_id, TO_DATE(crt_dt, 'DD/MM/YYYY') FROM cstmr "
     f"WHERE {LIVE_CUSTOMER} ORDER BY 1"),
    ("payment count (god table discriminator)",
     "SELECT COUNT(*) FROM payment",
     "SELECT COUNT(*) FROM fin_txn WHERE txn_typ = 'P'"),
    ("total revenue (dollars then cents)",
     "SELECT ROUND(SUM(amount), 2) FROM payment",
     f"SELECT ROUND(SUM({AMOUNT}), 2) FROM fin_txn WHERE txn_typ = 'P'"),
    ("revenue per month",
     "SELECT TO_CHAR(payment_date, 'YYYY-MM'), ROUND(SUM(amount), 2) FROM payment "
     "GROUP BY 1 ORDER BY 1",
     f"SELECT TO_CHAR(txn_ts, 'YYYY-MM'), ROUND(SUM({AMOUNT}), 2) FROM fin_txn "
     f"WHERE txn_typ = 'P' GROUP BY 1 ORDER BY 1"),
    ("revenue per store (through staff)",
     "SELECT s.store_id, ROUND(SUM(p.amount), 2) FROM payment p "
     "JOIN staff s USING (staff_id) GROUP BY 1 ORDER BY 1",
     f"SELECT s.str_no, ROUND(SUM({AMOUNT}), 2) FROM fin_txn f "
     f"JOIN stf s ON s.stf_id = f.stf_no WHERE f.txn_typ = 'P' GROUP BY 1 ORDER BY 1"),
    ("revenue per customer",
     "SELECT customer_id, ROUND(SUM(amount), 2) FROM payment GROUP BY 1 ORDER BY 1",
     f"SELECT cstmr_no, ROUND(SUM({AMOUNT}), 2) FROM fin_txn WHERE txn_typ = 'P' "
     f"GROUP BY 1 ORDER BY 1"),
    ("payment to rental (overloaded ref_no)",
     "SELECT payment_id IS NOT NULL, COUNT(*) FROM payment p "
     "JOIN rental r USING (rental_id) GROUP BY 1",
     "SELECT TRUE, COUNT(*) FROM fin_txn f JOIN rntl_hdr r ON r.rntl_id = f.ref_no "
     "WHERE f.txn_typ = 'P'"),
    ("rental count",
     "SELECT COUNT(*) FROM rental",
     "SELECT COUNT(*) FROM rntl_hdr"),
    ("rentals per month (YYYYMMDD text)",
     "SELECT TO_CHAR(rental_date, 'YYYY-MM'), COUNT(*) FROM rental GROUP BY 1 ORDER BY 1",
     f"SELECT TO_CHAR({RENTAL_DAY}, 'YYYY-MM'), COUNT(*) FROM rntl_hdr "
     f"GROUP BY 1 ORDER BY 1"),
    ("return dates (text then timestamp)",
     "SELECT rental_id, return_date::date FROM rental ORDER BY 1",
     f"SELECT rntl_id, {RETURN_DAY} FROM rntl_hdr ORDER BY 1"),
    ("rentals per film (zero-padded cast join)",
     "SELECT f.title, COUNT(*) FROM rental r JOIN inventory i USING (inventory_id) "
     "JOIN film f USING (film_id) GROUP BY 1 ORDER BY 1",
     'SELECT f."strTitle", COUNT(*) FROM rntl_hdr r '
     "JOIN invtry i ON CAST(r.inv_no AS INTEGER) = i.inv_id "
     'JOIN "tblFilm" f ON f."intFilmID" = i.film_no GROUP BY 1 ORDER BY 1'),
    ("film count (tombstone)",
     "SELECT COUNT(*) FROM film",
     f'SELECT COUNT(*) FROM "tblFilm" WHERE {LIVE_FILM}'),
    ("films per rating (magic numbers)",
     "SELECT rating::text, COUNT(*) FROM film GROUP BY 1 ORDER BY 1",
     f'SELECT {RATING}, COUNT(*) FROM "tblFilm" WHERE {LIVE_FILM} GROUP BY 1 ORDER BY 1'),
    ("films per actor (tombstoned cast rows)",
     "SELECT actor_id, COUNT(*) FROM film_actor GROUP BY 1 ORDER BY 1",
     'SELECT fa.actr_no, COUNT(*) FROM film_actr fa JOIN "tblFilm" f '
     f'ON f."intFilmID" = fa.film_no WHERE f.{LIVE_FILM} GROUP BY 1 ORDER BY 1'),
    ("actor names (padding)",
     "SELECT actor_id, first_name, last_name FROM actor ORDER BY 1",
     "SELECT actr_id, RTRIM(actr_fnm), RTRIM(actr_lnm) FROM actr ORDER BY 1"),
    ("films per category (composite junction)",
     "SELECT c.name, COUNT(*) FROM film_category fc JOIN category c USING (category_id) "
     "GROUP BY 1 ORDER BY 1",
     'SELECT c.ctgry_nm, COUNT(*) FROM film_ctgry fc JOIN ctgry c ON c.ctgry_id = fc.ctgry_no '
     f'JOIN "tblFilm" f ON f."intFilmID" = fc.film_no WHERE f.{LIVE_FILM} '
     "GROUP BY 1 ORDER BY 1"),
    ("special features (delimited list)",
     "SELECT film_id, ARRAY_TO_STRING(special_features, '|') FROM film ORDER BY 1",
     f'SELECT "intFilmID", "strFeatures" FROM "tblFilm" WHERE {LIVE_FILM} ORDER BY 1'),
    ("film language",
     "SELECT l.name, COUNT(*) FROM film f JOIN language l USING (language_id) GROUP BY 1",
     f'SELECT l.lang_nm, COUNT(*) FROM "tblFilm" f JOIN lang l ON l.lang_id = f."intLangID" '
     f"WHERE f.{LIVE_FILM} GROUP BY 1"),
    ("customers per country (three-hop join)",
     "SELECT co.country, COUNT(*) FROM customer cu JOIN address a USING (address_id) "
     "JOIN city ci USING (city_id) JOIN country co USING (country_id) GROUP BY 1 ORDER BY 1",
     "SELECT co.cntry_nm, COUNT(*) FROM cstmr cu JOIN addr a ON a.addr_id = cu.addr_id "
     "JOIN city_mstr ci ON ci.city_id = a.city_id JOIN cntry co ON co.cntry_id = ci.cntry_id "
     f"WHERE cu.{LIVE_CUSTOMER} GROUP BY 1 ORDER BY 1"),
    ("city names (mixed case)",
     "SELECT city_id, LOWER(city) FROM city ORDER BY 1",
     "SELECT city_id, LOWER(city_nm) FROM city_mstr ORDER BY 1"),
    ("address2 (null markers)",
     "SELECT address_id, NULLIF(TRIM(address2), '') FROM address ORDER BY 1",
     f"SELECT addr_id, {NULL_MARKED.format(c='addr2')} FROM addr ORDER BY 1"),
    ("staff active (four spellings of a boolean)",
     "SELECT staff_id, active FROM staff ORDER BY 1",
     "SELECT stf_id, actv IN ('Y', '1') FROM stf ORDER BY 1"),
    ("inventory per store",
     "SELECT store_id, COUNT(*) FROM inventory GROUP BY 1 ORDER BY 1",
     "SELECT str_no, COUNT(*) FROM invtry GROUP BY 1 ORDER BY 1"),
]


def _norm(value: Any) -> Any:
    if isinstance(value, (float, Decimal)):
        return round(float(value), 2)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def verify(verbose: bool = False) -> int:
    import psycopg

    from gauntlet import pg

    clean = psycopg.connect(pg.uri("pagila_clean"))
    legacy = psycopg.connect(pg.uri("pagila_legacy"))
    failures = 0
    for label, clean_sql, legacy_sql in CHECKS:
        try:
            expected = [tuple(_norm(v) for v in r) for r in clean.execute(clean_sql)]
            actual = [tuple(_norm(v) for v in r) for r in legacy.execute(legacy_sql)]
        except psycopg.Error as exc:  # noqa: PERF203 - one message per failing check
            clean.rollback()
            legacy.rollback()
            print(f"  ERROR  {label}: {str(exc).splitlines()[0]}")
            failures += 1
            continue
        if expected == actual:
            print(f"  ok     {label}  ({len(expected)} rows)")
        else:
            failures += 1
            print(f"  FAIL   {label}")
            print(f"         {len(expected)} expected rows vs {len(actual)} actual")
            diff = [(e, a) for e, a in zip(expected, actual, strict=False) if e != a]
            for e, a in diff[:4]:
                print(f"         clean={e!r}  legacy={a!r}")
            if verbose:
                print(f"         clean sql:  {clean_sql}")
                print(f"         legacy sql: {legacy_sql}")
    clean.close()
    legacy.close()
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} reversibility checks passed")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return 1 if verify(args.verbose) else 0


if __name__ == "__main__":
    sys.exit(main())
