"""Build the tiny fixture database the detector unit tests run against.

Deliberately small enough to reason about by hand: eleven rows of customers, six
orders, and one of each anti-pattern. The gauntlet measures accuracy; these fixtures
pin behaviour, so every value here is chosen to make exactly one detector fire.

    python -m tests.fixtures.build_fixture
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "tiny.sqlite"


def build(path: Path = FIXTURE) -> Path:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)

    # --- CUST_MSTR: cryptic names, CHAR padding, a soft-delete flag, XML, null markers
    conn.execute(
        """
        CREATE TABLE CUST_MSTR (
            CUST_ID   INTEGER PRIMARY KEY,
            CFNM1     CHAR(10),
            CO_NM     VARCHAR(40),
            ADDR_XML  TEXT,
            PHONE     VARCHAR(20),
            PHN_NBR   VARCHAR(20),
            REP_NO    VARCHAR(8),
            DEL_FLG   CHAR(1)
        )
        """
    )
    customers = [
        # live rows 1-8, with PHONE on the old half and PHN_NBR on the new half
        (1, "Ana       ", "Acme",    "<a><c>Berlin</c><cy>DE</cy></a>", "111", None, "00000001", "N"),
        (2, "Raj       ", "N/A",     "<a><c>Berlin</c><cy>Germany</cy></a>", "222", None, "00000001", None),
        (3, "Mei       ", "",        "<a><c>Paris</c><cy>FR</cy></a>", "333", None, "00000002", "n"),
        (4, "Tom       ", "Globex",  "<a><c>paris</c><cy>France</cy></a>", "444", None, "00000002", "N"),
        (5, "Ida       ", "UNKNOWN", "<a><c>Lisbon</c><cy>PT</cy></a>", None, "555", "00000001", "N"),
        (6, "Ken       ", "Initech", "<a><c>Lisbon</c><cy>PT</cy></a>", None, "666", "00000003", None),
        (7, "Lea       ", "-",       "<a><c>Oslo</c><cy>NO</cy></a>", None, "777", "00000003", "N"),
        (8, "Sam       ", "Umbrella", "<a><c>Oslo</c><cy>NO</cy></a>", None, "888", "00000002", "n"),
        # soft-deleted extras, which is what makes the rule recoverable
        (90, "Gone      ", "N/A", "<a><c>Nowhere</c><cy>XX</cy></a>", None, "000", "00000001", "Y"),
        (91, "Gone      ", "N/A", "<a><c>Nowhere</c><cy>XX</cy></a>", None, "000", "00000001", "Y"),
    ]
    conn.executemany(
        "INSERT INTO CUST_MSTR VALUES (?,?,?,?,?,?,?,?)", customers
    )

    # --- EMP_MSTR: an integer key the customer table references as padded text
    conn.execute(
        "CREATE TABLE EMP_MSTR (EMP_ID INTEGER PRIMARY KEY, ENM VARCHAR(20), "
        "MGR_ID INTEGER, STS INTEGER)"
    )
    conn.executemany(
        "INSERT INTO EMP_MSTR VALUES (?,?,?,?)",
        [(1, "Rose", None, 1), (2, "Dev", 1, 1), (3, "Kim", 1, 1), (9, "Old", 1, 9)],
    )

    # --- TRANS_MSTR: a god table, a composite key, unit drift, magic numbers
    conn.execute(
        """
        CREATE TABLE TRANS_MSTR (
            TRANS_TYP_CD CHAR(1),
            STORE_CD     CHAR(3),
            ORD_NO       INTEGER,
            CUST_NO      VARCHAR(10),
            ORD_DT_TXT   CHAR(8),
            TOT_AMT      FLOAT,
            TOT_AMT_C    INTEGER,
            ORD_STS      INTEGER,
            BILL_CTY     VARCHAR(30),
            BILL_CTRY    VARCHAR(30),
            SHIP_DT_TXT  CHAR(8),
            TRK_NBR      VARCHAR(20),
            PMT_AMT      FLOAT,
            PMT_DT_TXT   CHAR(8),
            PMT_MTHD_CD  VARCHAR(4),
            PMT_REF      VARCHAR(20)
        )
        """
    )
    # Order rows populate the billing and shipping columns and leave the payment ones
    # NULL; payment rows do the reverse. That disjointness is the god-table signal.
    orders = [
        # dollars before the 20230101 cutover, integer cents on and after it
        ("I", "US1", 1, "0000000001", "20220101", 10.0, None, 3,
         "Berlin", "DE", "20220103", "T1", None, None, None, None),
        ("I", "US1", 2, "0000000002", "20220615", 20.0, None, 4,
         "Paris", "FR", "20220617", "T2", None, None, None, None),
        ("I", "EU1", 1, "0000000003", "20221201", 30.0, None, 4,
         "Lisbon", "PT", "20221203", "T3", None, None, None, None),
        ("I", "US1", 3, "0000000004", "20230301", None, 4000, 3,
         "Oslo", "NO", "20230303", "T4", None, None, None, None),
        ("I", "EU1", 2, "0000000005", "20230801", None, 5000, 6,
         "Berlin", "DE", "20230803", "T5", None, None, None, None),
        ("I", "EU1", 3, "0000000006", "20240101", None, 6000, 4,
         "Paris", "FR", "20240103", "T6", None, None, None, None),
        # a cancelled order: an extra row, status 9
        ("I", "US1", 4, "0000000001", "20240201", None, 999, 9,
         "Nowhere", "XX", "20240203", "T7", None, None, None, None),
        # payments: the same table, a disjoint set of populated columns
        ("P", "US1", 1, "0000000001", "20220105", None, None, None,
         None, None, None, None, 10.0, "20220105", "CC", "R1"),
        ("P", "US1", 2, "0000000002", "20220620", None, None, None,
         None, None, None, None, 20.0, "20220620", "CH", "R2"),
        ("P", "EU1", 1, "0000000003", "20221205", None, None, None,
         None, None, None, None, 30.0, "20221205", "WT", "R3"),
        ("P", "US1", 3, "0000000004", "20230305", None, None, None,
         None, None, None, None, 40.0, "20230305", "CC", "R4"),
        ("P", "EU1", 2, "0000000005", "20230805", None, None, None,
         None, None, None, None, 50.0, "20230805", "WT", "R5"),
    ]
    conn.executemany(
        "INSERT INTO TRANS_MSTR VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", orders
    )

    # --- SO_LN: no declared key; identity is (STORE_CD, ORD_NO, LN_SEQ)
    conn.execute(
        "CREATE TABLE SO_LN (STORE_CD CHAR(3), ORD_NO INTEGER, LN_SEQ INTEGER, "
        "PRD_ID INTEGER, UNT_PRC FLOAT, QTY INTEGER)"
    )
    conn.executemany(
        "INSERT INTO SO_LN VALUES (?,?,?,?,?,?)",
        [
            ("US1", 1, 1, 1, 5.0, 1), ("US1", 1, 2, 2, 5.0, 1),
            ("US1", 2, 1, 1, 20.0, 1),
            ("EU1", 1, 1, 2, 30.0, 1),
            ("US1", 3, 1, 3, 40.0, 1),
            ("EU1", 2, 1, 3, 50.0, 1),
            ("EU1", 3, 1, 1, 60.0, 1),
        ],
    )

    # --- tblPrd: Hungarian naming, a k=v blob, a tombstone soft delete
    conn.execute(
        "CREATE TABLE tblPrd (PRD_ID INTEGER PRIMARY KEY, strNm VARCHAR(40), "
        "ATTRS TEXT, DLT_DT VARCHAR(19))"
    )
    conn.executemany(
        "INSERT INTO tblPrd VALUES (?,?,?,?)",
        [
            (1, "Widget", "color=Red|size=M", None),
            (2, "Gadget", "color=Blue|size=L", None),
            (3, "Doohickey", "color=Red|size=S", None),
            (4, "ZZ_Retired", "color=Grey|size=M", "2023-01-01 00:00:00"),
        ],
    )

    # --- sprawl: a backup, a staging table, an empty one, a yearly archive
    conn.execute("CREATE TABLE CUST_MSTR_BAK_20240101 AS SELECT * FROM CUST_MSTR")
    conn.execute("CREATE TABLE STG_CUST_LOAD AS SELECT CUST_ID, CFNM1 FROM CUST_MSTR LIMIT 3")
    conn.execute("CREATE TABLE tmp_scratch (ID INTEGER, VAL TEXT)")
    conn.execute(
        "CREATE TABLE SO_HDR_2022 AS SELECT * FROM TRANS_MSTR "
        "WHERE SUBSTR(ORD_DT_TXT,1,4) = '2022'"
    )

    # --- a view that documents the discriminator, the cast join and the enum
    conn.execute(
        """
        CREATE VIEW V_ORD AS
        SELECT t.STORE_CD, t.ORD_NO,
               CASE t.ORD_STS WHEN 3 THEN 'Shipped' WHEN 4 THEN 'Delivered'
                              WHEN 6 THEN 'Closed'  WHEN 9 THEN 'Cancelled'
                              ELSE 'Unknown' END AS STS_DESC,
               RTRIM(c.CFNM1) AS NM
        FROM TRANS_MSTR t
        JOIN CUST_MSTR c ON c.CUST_ID = CAST(t.CUST_NO AS INTEGER)
        WHERE t.TRANS_TYP_CD = 'I' AND t.ORD_STS <> 9
        """
    )
    # --- a view referencing a column that no longer exists
    conn.execute("CREATE VIEW V_BROKEN AS SELECT CUST_ID, MISSING_COL FROM CUST_MSTR")

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return path


if __name__ == "__main__":
    print(f"wrote {build()}")
