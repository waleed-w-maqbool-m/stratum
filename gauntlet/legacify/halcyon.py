"""Legacify AdventureWorks into HALCYON (SQL Server).

Reads the pristine `AW_CLEAN` (AdventureWorks 2019 restored as-is, plus an indexed
audit log) and writes `HALCYON`: the same sales, product, purchasing and inventory
facts under all sixteen anti-patterns of PLAN.md 3.2, plus the `manifest.json` answer
key that only the scorer may read.

Everything is set-based T-SQL run inside the server, reading AW_CLEAN across
databases. Randomness would make two builds differ, so every "random" choice is a hash
of the row's own key: `ABS(CHECKSUM(key, 'salt')) % n`.

Invariant that keeps the gold answers valid: **every transformation is reversible.**
Deleted customers, cancelled orders, departed salespeople and discontinued products
are *extra* rows that do not exist in AW_CLEAN, so filtering them out reproduces the
clean set exactly. `verify_halcyon.py` checks this before any question is scored.

Deviations from the plan, recorded in the manifest's scale notes:
* ~80 sprawl tables rather than ~300, so the naive baseline's whole-schema prompt fits
  in the free tier's per-request token limit. A baseline that cannot be run is not a
  baseline.
* The audit log has 2M rows rather than ~8M; it only has to be big enough that a full
  scan is refused, and the build stays under a few minutes.
* The god table is AdventureWorks' own inventory ledger (`TransactionHistory`), whose
  sales, purchase and work-order rows really are told apart by a type code, rather
  than synthetic orders/returns/payments.

Usage:
    python -m gauntlet.legacify.halcyon [--out DIR] [--skip-restore]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .common import Manifest

CLEAN = "AW_CLEAN"
LEGACY = "HALCYON"

# Money moved from dollars to cents on SO_HDR for orders on or after this date (#6).
CENTS_CUTOVER = "20130701"
# Customers above this id have their email only in CUST_CNTCT (#6).
EMAIL_CUTOVER_ID = 25000
# Customers from this id have their phone in PHN_NBR; earlier ones in PHONE (#6).
PHONE_CUTOVER_ID = 20000

AUDIT_ROWS = 2_000_000

ORDER_STATUS = {1: "in process", 2: "approved", 3: "backordered", 4: "rejected",
                5: "shipped", 6: "cancelled", 9: "deleted"}
SHIP_METHODS = {1: "XRQ - TRUCK GROUND", 2: "ZY - EXPRESS", 3: "OVERSEAS - DELUXE",
                4: "OVERNIGHT J-FAST", 5: "CARGO TRANSPORT 5"}
PRODUCT_LINES = {"R": "Road", "M": "Mountain", "T": "Touring", "S": "Standard"}
PO_STATUS = {1: "pending", 2: "approved", 3: "rejected", 4: "complete"}
CREDIT_RATING = {1: "superior", 2: "excellent", 3: "above average", 4: "average",
                 5: "below average"}
TRANS_TYPES = {"S": "sales order line", "P": "purchase order line", "W": "work order"}

GOD_FILLER = [
    "BATCH_NO INT", "SRC_SYS_CD VARCHAR(6)", "CRNCY_CD CHAR(3)", "FX_RT DECIMAL(10,6)",
    "GL_ACCT VARCHAR(12)", "CC_CD VARCHAR(8)", "RECON_FLG CHAR(1)", "RECON_DT_TXT CHAR(8)",
    "XPORT_FLG CHAR(1)", "XPORT_DT_TXT CHAR(8)", "HOLD_FLG CHAR(1)", "HOLD_RSN VARCHAR(40)",
    "APPRV_BY VARCHAR(12)", "APPRV_DT_TXT CHAR(8)", "LOT_NO VARCHAR(12)", "BIN_CD VARCHAR(8)",
    "SHELF_CD VARCHAR(4)", "LOC_ID SMALLINT", "UOM_CD CHAR(3)", "WT_KG DECIMAL(9,3)",
    "VOL_M3 DECIMAL(9,4)", "DIM_TXT VARCHAR(30)", "QA_FLG CHAR(1)", "QA_BY VARCHAR(12)",
    "QA_DT_TXT CHAR(8)", "RMA_NO VARCHAR(12)", "RMA_RSN VARCHAR(40)", "CARR_CD VARCHAR(6)",
    "TRK_NBR VARCHAR(30)", "SHIP_DT_TXT CHAR(8)", "INV_NO VARCHAR(12)", "INV_DT_TXT CHAR(8)",
    "TERMS_CD VARCHAR(6)", "DUE_DT_TXT CHAR(8)", "PMT_REF VARCHAR(20)", "TAX_CD VARCHAR(6)",
    "TAX_AMT DECIMAL(12,4)", "DSC_CD VARCHAR(8)", "DSC_AMT DECIMAL(12,4)", "PRJ_CD VARCHAR(10)",
    "CST_CTR VARCHAR(8)", "DEPT_CD VARCHAR(6)", "EMP_NO INT", "MCH_ID VARCHAR(10)",
    "SHFT_CD CHAR(1)", "LBR_HRS DECIMAL(7,2)", "OVHD_AMT DECIMAL(12,4)", "MAT_AMT DECIMAL(12,4)",
    "STD_CST DECIMAL(12,4)", "VAR_AMT DECIMAL(12,4)", "VAR_RSN VARCHAR(40)", "REV_NO TINYINT",
    "PRIORITY_NO TINYINT", "EXT_REF VARCHAR(20)", "IMPT_ID INT", "IMPT_DT_TXT CHAR(8)",
    "NOTE_TXT VARCHAR(200)", "USR_FLD1 VARCHAR(40)", "USR_FLD2 VARCHAR(40)",
    "USR_FLD3 VARCHAR(40)", "USR_NUM1 DECIMAL(12,4)", "USR_NUM2 DECIMAL(12,4)",
    "LST_UPD_BY VARCHAR(12)", "LST_UPD_TS VARCHAR(20)",
]


def h(key: str, salt: str, n: int) -> str:
    """A deterministic pseudo-random bucket in [0, n) for a row."""
    return f"(ABS(CHECKSUM({key}, '{salt}')) % {n})"


class Builder:
    def __init__(self) -> None:
        from gauntlet import mssql

        self.conn = mssql.connect(LEGACY)
        self.m = Manifest(database=LEGACY, dialect="mssql", source="AdventureWorks 2019 OLTP")

    def run(self, sql: str) -> None:
        cur = self.conn.cursor()
        cur.execute(sql)
        while cur.nextset():
            pass

    def scalar(self, sql: str):
        cur = self.conn.cursor()
        cur.execute(sql)
        return cur.fetchone()[0]

    def describe(self, table: str, column: str | None, text: str) -> None:
        """Anti-pattern #3: extended properties, some of them wrong."""
        literal = text.replace("'", "''")
        level = (
            f", @level2type = N'COLUMN', @level2name = N'{column}'" if column else ""
        )
        self.run(
            f"EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'{literal}', "
            f"@level0type = N'SCHEMA', @level0name = N'dbo', "
            f"@level1type = N'TABLE', @level1name = N'{table}'{level}"
        )

    # -------------------------------------------------------------------- reference
    def territories_and_people(self) -> None:
        m = self.m
        self.run(
            "CREATE TABLE TERR (TERR_ID INT NOT NULL PRIMARY KEY, TERR_NM VARCHAR(50), "
            "CTRY_CD VARCHAR(3), GRP VARCHAR(50))"
        )
        self.run(
            f"INSERT INTO TERR SELECT TerritoryID, Name, CountryRegionCode, [Group] "
            f"FROM {CLEAN}.Sales.SalesTerritory"
        )
        m.rename("TERR", "sales_territory")
        m.key("TERR", ["TERR_ID"])
        for col, logical in [("TERR_ID", "id"), ("TERR_NM", "name"),
                             ("CTRY_CD", "country_code"), ("GRP", "group")]:
            m.col("TERR", col, "sales_territory", logical)

        # Salespeople: hire date as DD/MM/YYYY text (#13); departed staff kept with a
        # deletion date (#12, the third soft-delete style).
        self.run(
            "CREATE TABLE SLSPRSN (SP_ID INT NOT NULL, SP_FNM VARCHAR(50), SP_LNM VARCHAR(50), "
            "TERR_ID INT NULL, QUOTA MONEY NULL, BONUS MONEY, COMM_PCT SMALLMONEY, "
            "HIRE_DT VARCHAR(10), DLT_DT DATETIME NULL)"
        )
        self.run(
            f"INSERT INTO SLSPRSN SELECT sp.BusinessEntityID, p.FirstName, p.LastName, "
            f"sp.TerritoryID, sp.SalesQuota, sp.Bonus, sp.CommissionPct, "
            f"CONVERT(VARCHAR(10), e.HireDate, 103), NULL "
            f"FROM {CLEAN}.Sales.SalesPerson sp "
            f"JOIN {CLEAN}.Person.Person p ON p.BusinessEntityID = sp.BusinessEntityID "
            f"JOIN {CLEAN}.HumanResources.Employee e ON e.BusinessEntityID = sp.BusinessEntityID"
        )
        self.run(
            f"INSERT INTO SLSPRSN SELECT 900 + ROW_NUMBER() OVER (ORDER BY p.BusinessEntityID), "
            f"p.FirstName, p.LastName, 1 + {h('p.BusinessEntityID', 'sp-terr', 10)}, 250000, 0, "
            f"0.01, '15/03/2011', DATEADD(DAY, {h('p.BusinessEntityID', 'sp-dlt', 300)}, '2012-06-01') "
            f"FROM (SELECT TOP 3 BusinessEntityID, FirstName, LastName "
            f"FROM {CLEAN}.Person.Person WHERE PersonType = 'EM' "
            f"AND BusinessEntityID NOT IN (SELECT BusinessEntityID FROM {CLEAN}.Sales.SalesPerson) "
            f"ORDER BY BusinessEntityID DESC) p"
        )
        m.rename("SLSPRSN", "salesperson")
        m.key("SLSPRSN", ["SP_ID"])
        for col, logical in [("SP_ID", "id"), ("SP_FNM", "first_name"), ("SP_LNM", "last_name"),
                             ("TERR_ID", "territory_id"), ("QUOTA", "sales_quota"),
                             ("BONUS", "bonus"), ("COMM_PCT", "commission_pct"),
                             ("HIRE_DT", "hire_date")]:
            m.col("SLSPRSN", col, "salesperson", logical)
        m.soft_delete("SLSPRSN", style="tombstone_date", predicate="DLT_DT IS NOT NULL",
                      live_predicate="DLT_DT IS NULL", injected_rows=3)
        m.record_type_mismatch(table="SLSPRSN", column="HIRE_DT", stored_as="varchar DD/MM/YYYY",
                               meaning="date")
        m.join(child="SLSPRSN", child_cols=["TERR_ID"], parent="TERR", parent_cols=["TERR_ID"])

        self.run(
            "CREATE TABLE STR_MSTR (STR_ID INT NOT NULL, STR_NM VARCHAR(60), SP_ID INT NULL)"
        )
        self.run(
            f"INSERT INTO STR_MSTR SELECT BusinessEntityID, Name, SalesPersonID "
            f"FROM {CLEAN}.Sales.Store"
        )
        m.rename("STR_MSTR", "store")
        m.key("STR_MSTR", ["STR_ID"])
        for col, logical in [("STR_ID", "id"), ("STR_NM", "name"), ("SP_ID", "salesperson_id")]:
            m.col("STR_MSTR", col, "store", logical)
        m.join(child="STR_MSTR", child_cols=["SP_ID"], parent="SLSPRSN", parent_cols=["SP_ID"])

    # -------------------------------------------------------------------- customers
    def customers(self) -> None:
        m = self.m
        self.run(
            "CREATE TABLE CUST_MSTR ("
            " CUST_ID INT NOT NULL PRIMARY KEY, ACCT_NO VARCHAR(10), CFNM1 CHAR(30) NULL,"
            " CMNM1 VARCHAR(30) NULL, CLNM1 CHAR(40) NULL, STR_ID INT NULL, TERR_ID INT NULL,"
            " EML VARCHAR(80) NULL, PHONE VARCHAR(25) NULL, PHN_NBR VARCHAR(25) NULL,"
            " CITY VARCHAR(30) COLLATE Latin1_General_CS_AS NULL, CTRY VARCHAR(20) NULL,"
            " ADDR_XML NVARCHAR(400) NULL, PREFS NVARCHAR(200) NULL, DEL_FLG CHAR(1) NULL)"
        )
        # One home address per person: AdventureWorks individuals have exactly one.
        address = (
            f"OUTER APPLY (SELECT TOP 1 a.AddressLine1, a.City, a.PostalCode, "
            f"RTRIM(sp.StateProvinceCode) AS st, sp.CountryRegionCode AS ctry "
            f"FROM {CLEAN}.Person.BusinessEntityAddress bea "
            f"JOIN {CLEAN}.Person.Address a ON a.AddressID = bea.AddressID "
            f"JOIN {CLEAN}.Person.StateProvince sp ON sp.StateProvinceID = a.StateProvinceID "
            f"WHERE bea.BusinessEntityID = c.PersonID ORDER BY bea.AddressTypeID, a.AddressID) ad"
        )
        city = (
            f"CASE {h('c.CustomerID', 'city', 4)} WHEN 0 THEN UPPER(ad.City) "
            f"WHEN 1 THEN LOWER(ad.City) ELSE ad.City END"
        )
        country = (
            f"CASE ad.ctry "
            f"WHEN 'US' THEN CASE {h('c.CustomerID', 'ctry', 3)} WHEN 0 THEN 'US' WHEN 1 THEN 'USA' ELSE 'United States' END "
            f"WHEN 'GB' THEN CASE {h('c.CustomerID', 'ctry', 3)} WHEN 0 THEN 'GB' WHEN 1 THEN 'UK' ELSE 'United Kingdom' END "
            f"WHEN 'CA' THEN CASE {h('c.CustomerID', 'ctry', 2)} WHEN 0 THEN 'CA' ELSE 'Canada' END "
            f"WHEN 'DE' THEN CASE {h('c.CustomerID', 'ctry', 2)} WHEN 0 THEN 'DE' ELSE 'Germany' END "
            f"WHEN 'FR' THEN CASE {h('c.CustomerID', 'ctry', 2)} WHEN 0 THEN 'FR' ELSE 'France' END "
            f"WHEN 'AU' THEN CASE {h('c.CustomerID', 'ctry', 2)} WHEN 0 THEN 'AU' ELSE 'Australia' END "
            f"ELSE ad.ctry END"
        )
        # FOR XML PATH escapes &, < and > in the address line for us.
        xml = (
            "(SELECT ad.AddressLine1 AS ln1, ad.st AS st, ad.PostalCode AS zip "
            "FOR XML PATH('addr'))"
        )
        middle = (
            f"CASE WHEN c.PersonID IS NULL THEN NULL WHEN p.MiddleName IS NOT NULL THEN p.MiddleName "
            f"ELSE CASE {h('c.CustomerID', 'mid', 6)} WHEN 0 THEN 'N/A' WHEN 1 THEN '' "
            f"WHEN 2 THEN '-' WHEN 3 THEN 'UNKNOWN' ELSE NULL END END"
        )
        live_flag = (
            f"CASE {h('c.CustomerID', 'del', 10)} WHEN 0 THEN NULL WHEN 1 THEN NULL "
            f"WHEN 2 THEN 'n' ELSE 'N' END"
        )
        select = (
            f"SELECT __ID__, c.AccountNumber, p.FirstName, {middle}, p.LastName, c.StoreID, "
            f"c.TerritoryID, "
            f"CASE WHEN c.CustomerID <= {EMAIL_CUTOVER_ID} THEN e.EmailAddress END, "
            f"CASE WHEN c.CustomerID < {PHONE_CUTOVER_ID} THEN ph.PhoneNumber END, "
            f"CASE WHEN c.CustomerID >= {PHONE_CUTOVER_ID} THEN ph.PhoneNumber END, "
            f"{city}, {country}, "
            f"CASE WHEN ad.City IS NOT NULL THEN {xml} END, "
            f"CASE WHEN p.BusinessEntityID IS NOT NULL THEN "
            f"N'{{\"promo\":' + CAST(p.EmailPromotion AS NVARCHAR(2)) + N',\"src\":\"web\"}}' END, "
            f"__FLAG__ "
            f"FROM {CLEAN}.Sales.Customer c "
            f"LEFT JOIN {CLEAN}.Person.Person p ON p.BusinessEntityID = c.PersonID "
            f"LEFT JOIN (SELECT BusinessEntityID, MIN(EmailAddress) AS EmailAddress "
            f"FROM {CLEAN}.Person.EmailAddress GROUP BY BusinessEntityID) e "
            f"ON e.BusinessEntityID = c.PersonID "
            f"LEFT JOIN (SELECT BusinessEntityID, MIN(PhoneNumber) AS PhoneNumber "
            f"FROM {CLEAN}.Person.PersonPhone GROUP BY BusinessEntityID) ph "
            f"ON ph.BusinessEntityID = c.PersonID "
            f"{address}"
        )
        self.run(
            "INSERT INTO CUST_MSTR " + select.replace("__ID__", "c.CustomerID").replace("__FLAG__", live_flag)
        )
        # Deleted customers: extra rows that never existed in AW_CLEAN (#12).
        self.run(
            "INSERT INTO CUST_MSTR "
            + select.replace("__ID__", "40000 + c.CustomerID").replace("__FLAG__", "'Y'")
            + f" WHERE c.PersonID IS NOT NULL AND {h('c.CustomerID', 'deleted', 40)} = 0"
        )
        deleted = self.scalar("SELECT COUNT(*) FROM CUST_MSTR WHERE DEL_FLG = 'Y'")

        # Email moved to a contact table partway through (#6).
        self.run(
            "CREATE TABLE CUST_CNTCT (CUST_ID INT NOT NULL, CNTCT_TYP CHAR(1) NOT NULL, "
            "CNTCT_VAL VARCHAR(80), UPD_DT CHAR(8))"
        )
        self.run(
            f"INSERT INTO CUST_CNTCT SELECT c.CustomerID, 'E', e.EmailAddress, '20140912' "
            f"FROM {CLEAN}.Sales.Customer c "
            f"JOIN (SELECT BusinessEntityID, MIN(EmailAddress) AS EmailAddress "
            f"FROM {CLEAN}.Person.EmailAddress GROUP BY BusinessEntityID) e "
            f"ON e.BusinessEntityID = c.PersonID WHERE c.CustomerID > {EMAIL_CUTOVER_ID}"
        )

        m.rename("CUST_MSTR", "customer")
        m.key("CUST_MSTR", ["CUST_ID"])
        for col, logical in [("CUST_ID", "id"), ("ACCT_NO", "account_number"),
                             ("CFNM1", "first_name"), ("CMNM1", "middle_name"),
                             ("CLNM1", "last_name"), ("STR_ID", "store_id"),
                             ("TERR_ID", "territory_id"), ("EML", "email"),
                             ("PHONE", "phone"), ("PHN_NBR", "phone"), ("CITY", "city"),
                             ("CTRY", "country"), ("ADDR_XML", "address"),
                             ("PREFS", "preferences"), ("DEL_FLG", "deleted")]:
            m.col("CUST_MSTR", col, "customer", logical)
        m.rename("CUST_CNTCT", "customer_contact")
        m.key("CUST_CNTCT", ["CUST_ID", "CNTCT_TYP"])
        m.join(child="CUST_MSTR", child_cols=["STR_ID"], parent="STR_MSTR", parent_cols=["STR_ID"])
        m.join(child="CUST_MSTR", child_cols=["TERR_ID"], parent="TERR", parent_cols=["TERR_ID"])
        m.join(child="CUST_CNTCT", child_cols=["CUST_ID"], parent="CUST_MSTR", parent_cols=["CUST_ID"])
        m.soft_delete("CUST_MSTR", style="flag", predicate="UPPER(DEL_FLG) = 'Y'",
                      live_predicate="(UPPER(DEL_FLG) <> 'Y' OR DEL_FLG IS NULL)",
                      injected_rows=int(deleted))
        m.record_drift(table="CUST_MSTR", kind="column_relocation", old="CUST_MSTR.EML",
                       new="CUST_CNTCT.CNTCT_VAL", boundary=f"CUST_ID > {EMAIL_CUTOVER_ID}")
        m.record_drift(table="CUST_MSTR", kind="column_replacement", old="PHONE", new="PHN_NBR",
                       boundary=f"CUST_ID >= {PHONE_CUTOVER_ID}")
        m.record_format(table="CUST_MSTR", column="ADDR_XML", format="xml",
                        paths={"ln1": "address_line1", "st": "state_code", "zip": "postal_code"})
        m.record_format(table="CUST_MSTR", column="PREFS", format="json",
                        paths={"promo": "email_promotion"})
        m.record_quality(table="CUST_MSTR", column="CITY", issue="mixed case under a case-sensitive collation")
        m.record_quality(table="CUST_MSTR", column="CTRY", issue="synonyms",
                         examples=["US", "USA", "United States"])
        m.record_quality(table="CUST_MSTR", column="CMNM1", issue="null markers",
                         markers=["N/A", "", "-", "UNKNOWN"])
        m.record_quality(table="CUST_MSTR", column="CFNM1", issue="CHAR padding")

    # ----------------------------------------------------------------------- orders
    def orders(self) -> None:
        m = self.m
        self.run(
            "CREATE TABLE SO_HDR (BR_CD CHAR(3) NOT NULL, ORD_NO INT NOT NULL, SO_ID INT NOT NULL,"
            " CUST_NO VARCHAR(10), SP_ID INT NULL, ORD_DT CHAR(8), DUE_DT VARCHAR(10),"
            " SHIP_DT DATETIME NULL, ORD_STS TINYINT, ONLN_FLG CHAR(1), SHIP_VIA TINYINT,"
            " PO_REF VARCHAR(25) NULL, SUB_TOT DECIMAL(19,4), TAX_AMT DECIMAL(19,4),"
            " FRT_AMT DECIMAL(19,4), TOT_DUE DECIMAL(19,4), NOTES VARCHAR(100) NULL)"
        )
        money = (
            f"CASE WHEN h.OrderDate >= '{CENTS_CUTOVER}' THEN h.{{c}} * 100 ELSE h.{{c}} END"
        )
        online = (
            f"CASE WHEN h.OnlineOrderFlag = 1 THEN CASE {h('h.SalesOrderID', 'onl', 3)} "
            f"WHEN 0 THEN '1' ELSE 'Y' END ELSE CASE {h('h.SalesOrderID', 'onl', 3)} "
            f"WHEN 0 THEN '0' ELSE 'N' END END"
        )
        notes = (
            f"CASE WHEN d.offer IS NOT NULL THEN 'DSC:' + RIGHT('00' + CAST(d.offer AS VARCHAR(3)), 3) "
            f"WHEN {h('h.SalesOrderID', 'note', 9)} = 0 THEN 'call before delivery' END"
        )
        self.run(
            f"INSERT INTO SO_HDR SELECT 'B' + RIGHT('0' + CAST(h.TerritoryID AS VARCHAR(2)), 2), "
            f"ROW_NUMBER() OVER (PARTITION BY h.TerritoryID ORDER BY h.SalesOrderID), "
            f"h.SalesOrderID, RIGHT('0000000000' + CAST(h.CustomerID AS VARCHAR(10)), 10), "
            f"h.SalesPersonID, CONVERT(CHAR(8), h.OrderDate, 112), "
            f"CONVERT(VARCHAR(10), h.DueDate, 103), h.ShipDate, h.Status, {online}, "
            f"h.ShipMethodID, h.PurchaseOrderNumber, "
            f"{money.format(c='SubTotal')}, {money.format(c='TaxAmt')}, "
            f"{money.format(c='Freight')}, {money.format(c='TotalDue')}, {notes} "
            f"FROM {CLEAN}.Sales.SalesOrderHeader h "
            f"LEFT JOIN (SELECT SalesOrderID, MIN(SpecialOfferID) AS offer "
            f"FROM {CLEAN}.Sales.SalesOrderDetail WHERE UnitPriceDiscount > 0 "
            f"GROUP BY SalesOrderID) d ON d.SalesOrderID = h.SalesOrderID"
        )
        self.run(
            "CREATE TABLE SO_LN (BR_CD CHAR(3) NOT NULL, ORD_NO INT NOT NULL, LN_SEQ SMALLINT NOT NULL,"
            " PRD_ID INT, QTY SMALLINT, UNIT_PRC FLOAT, DSC_PCT FLOAT, LN_AMT FLOAT, OFR_ID INT)"
        )
        self.run(
            "INSERT INTO SO_LN SELECT o.BR_CD, o.ORD_NO, "
            "ROW_NUMBER() OVER (PARTITION BY d.SalesOrderID ORDER BY d.SalesOrderDetailID), "
            "d.ProductID, d.OrderQty, d.UnitPrice, d.UnitPriceDiscount, d.LineTotal, d.SpecialOfferID "
            f"FROM {CLEAN}.Sales.SalesOrderDetail d JOIN SO_HDR o ON o.SO_ID = d.SalesOrderID"
        )

        # Cancelled-and-deleted orders (#12, status 9): extra headers with their lines.
        self.run(
            "SELECT o.*, ROW_NUMBER() OVER (PARTITION BY o.BR_CD ORDER BY o.SO_ID) AS rn "
            "INTO #dead FROM SO_HDR o WHERE " + h("o.SO_ID", "cancel", 60) + " = 0"
        )
        self.run(
            "INSERT INTO SO_HDR SELECT d.BR_CD, mx.top_no + d.rn, d.SO_ID + 1000000, d.CUST_NO, "
            "d.SP_ID, d.ORD_DT, d.DUE_DT, NULL, 9, d.ONLN_FLG, d.SHIP_VIA, d.PO_REF, d.SUB_TOT, "
            "d.TAX_AMT, d.FRT_AMT, d.TOT_DUE, 'cancelled by customer' FROM #dead d "
            "JOIN (SELECT BR_CD, MAX(ORD_NO) AS top_no FROM SO_HDR GROUP BY BR_CD) mx "
            "ON mx.BR_CD = d.BR_CD"
        )
        self.run(
            "INSERT INTO SO_LN SELECT d.BR_CD, n.ORD_NO, l.LN_SEQ, l.PRD_ID, l.QTY, l.UNIT_PRC, "
            "l.DSC_PCT, l.LN_AMT, l.OFR_ID FROM #dead d "
            "JOIN SO_LN l ON l.BR_CD = d.BR_CD AND l.ORD_NO = d.ORD_NO "
            "JOIN SO_HDR n ON n.SO_ID = d.SO_ID + 1000000"
        )
        cancelled = self.scalar("SELECT COUNT(*) FROM SO_HDR WHERE ORD_STS = 9")

        m.rename("SO_HDR", "sales_order")
        m.key("SO_HDR", ["BR_CD", "ORD_NO"])
        for col, logical in [("SO_ID", "id"), ("CUST_NO", "customer_id"),
                             ("SP_ID", "salesperson_id"), ("ORD_DT", "order_date"),
                             ("DUE_DT", "due_date"), ("SHIP_DT", "ship_date"),
                             ("ORD_STS", "status"), ("ONLN_FLG", "online"),
                             ("SHIP_VIA", "ship_method"), ("SUB_TOT", "subtotal"),
                             ("TAX_AMT", "tax"), ("FRT_AMT", "freight"),
                             ("TOT_DUE", "total_due")]:
            m.col("SO_HDR", col, "sales_order", logical)
        m.rename("SO_LN", "sales_order_line")
        m.key("SO_LN", ["BR_CD", "ORD_NO", "LN_SEQ"])
        for col, logical in [("PRD_ID", "product_id"), ("QTY", "quantity"),
                             ("UNIT_PRC", "unit_price"), ("DSC_PCT", "discount"),
                             ("LN_AMT", "line_total")]:
            m.col("SO_LN", col, "sales_order_line", logical)
        m.join(child="SO_LN", child_cols=["BR_CD", "ORD_NO"], parent="SO_HDR",
               parent_cols=["BR_CD", "ORD_NO"], note="composite: order numbers restart per branch")
        m.join(child="SO_HDR", child_cols=["CUST_NO"], parent="CUST_MSTR", parent_cols=["CUST_ID"],
               cast="CAST(CUST_NO AS INT)", note="zero-padded varchar against int")
        m.join(child="SO_HDR", child_cols=["SP_ID"], parent="SLSPRSN", parent_cols=["SP_ID"])
        m.join(child="SO_LN", child_cols=["PRD_ID"], parent="tblPrd", parent_cols=["intPrdID"])
        m.enum("SO_HDR", "ORD_STS", ORDER_STATUS)
        m.enum("SO_HDR", "SHIP_VIA", SHIP_METHODS)
        m.enum("SO_HDR", "ONLN_FLG", {"Y": "online", "1": "online", "N": "in person", "0": "in person"})
        m.soft_delete("SO_HDR", style="status_code", predicate="ORD_STS = 9",
                      live_predicate="ORD_STS <> 9", injected_rows=int(cancelled))
        for column in ("SUB_TOT", "TAX_AMT", "FRT_AMT", "TOT_DUE"):
            m.record_drift(table="SO_HDR", kind="unit_change", column=column,
                           boundary_column="ORD_DT", boundary_value=CENTS_CUTOVER,
                           ratio=100, note="dollars before, cents from the cutover")
        m.record_type_mismatch(table="SO_HDR", column="ORD_DT", stored_as="char YYYYMMDD", meaning="date")
        m.record_type_mismatch(table="SO_HDR", column="DUE_DT", stored_as="varchar DD/MM/YYYY", meaning="date")
        m.record_type_mismatch(table="SO_HDR", column="CUST_NO", stored_as="zero-padded varchar",
                               meaning="customer id, int in CUST_MSTR")
        m.record_type_mismatch(table="SO_LN", column="LN_AMT", stored_as="float", meaning="money")
        m.record_overload(table="SO_HDR", column="NOTES",
                          meanings={"DSC:": "special offer applied, three-digit offer id",
                                    "": "free-text delivery notes"})

    # ---------------------------------------------------------------------- products
    def products(self) -> None:
        m = self.m
        self.run("CREATE TABLE PCTGRY (CAT_ID INT NOT NULL PRIMARY KEY, CAT_NM VARCHAR(50))")
        self.run(f"INSERT INTO PCTGRY SELECT ProductCategoryID, Name FROM {CLEAN}.Production.ProductCategory")
        self.run("CREATE TABLE PSCAT (SCAT_ID INT NOT NULL, CAT_ID INT, SCAT_NM VARCHAR(50))")
        self.run(
            f"INSERT INTO PSCAT SELECT ProductSubcategoryID, ProductCategoryID, Name "
            f"FROM {CLEAN}.Production.ProductSubcategory"
        )
        # Hungarian notation, a vendor import (#1); attributes packed as k=v (#8).
        self.run(
            "CREATE TABLE tblPrd (intPrdID INT NOT NULL PRIMARY KEY, strNm NVARCHAR(60),"
            " strPrdNo NVARCHAR(25), intSubCatID INT NULL, curLstPrc MONEY, fltStdCst FLOAT,"
            " chrLine CHAR(2) NULL, chrMakeFlg CHAR(1), ATTRS VARCHAR(120) NULL,"
            " dtSellStrt DATETIME, dtSellEnd DATETIME NULL)"
        )
        attrs = (
            "NULLIF(STUFF("
            "ISNULL('|color=' + Color, '') + ISNULL('|size=' + Size, '') "
            "+ ISNULL('|weight=' + CAST(Weight AS VARCHAR(12)), ''), 1, 1, ''), '')"
        )
        make = (
            f"CASE WHEN MakeFlag = 1 THEN CASE {h('ProductID', 'mk', 2)} WHEN 0 THEN 'Y' ELSE '1' END "
            f"ELSE CASE {h('ProductID', 'mk', 2)} WHEN 0 THEN 'N' ELSE '0' END END"
        )
        self.run(
            f"INSERT INTO tblPrd SELECT ProductID, Name, ProductNumber, ProductSubcategoryID, "
            f"ListPrice, StandardCost, ProductLine, {make}, {attrs}, SellStartDate, SellEndDate "
            f"FROM {CLEAN}.Production.Product"
        )
        # Discontinued products renamed with a ZZ_ prefix and never deleted (#12).
        self.run(
            f"INSERT INTO tblPrd SELECT 2000 + ProductID, LEFT('ZZ_' + Name, 60), "
            f"ProductNumber + '-X', ProductSubcategoryID, ListPrice, StandardCost, ProductLine, "
            f"{make}, {attrs}, SellStartDate, '2012-05-29' "
            f"FROM {CLEAN}.Production.Product WHERE ProductSubcategoryID IS NOT NULL "
            f"AND {h('ProductID', 'zz', 25)} = 0"
        )
        discontinued = self.scalar("SELECT COUNT(*) FROM tblPrd WHERE strNm LIKE 'ZZ[_]%'")

        m.rename("tblPrd", "product")
        m.key("tblPrd", ["intPrdID"])
        for col, logical in [("intPrdID", "id"), ("strNm", "name"), ("strPrdNo", "product_number"),
                             ("intSubCatID", "subcategory_id"), ("curLstPrc", "list_price"),
                             ("fltStdCst", "standard_cost"), ("chrLine", "product_line"),
                             ("chrMakeFlg", "made_in_house"), ("ATTRS", "attributes"),
                             ("dtSellStrt", "sell_start_date"), ("dtSellEnd", "sell_end_date")]:
            m.col("tblPrd", col, "product", logical)
        m.rename("PSCAT", "product_subcategory")
        m.key("PSCAT", ["SCAT_ID"])
        m.rename("PCTGRY", "product_category")
        m.key("PCTGRY", ["CAT_ID"])
        m.join(child="tblPrd", child_cols=["intSubCatID"], parent="PSCAT", parent_cols=["SCAT_ID"])
        m.join(child="PSCAT", child_cols=["CAT_ID"], parent="PCTGRY", parent_cols=["CAT_ID"])
        m.enum("tblPrd", "chrLine", PRODUCT_LINES)
        m.enum("tblPrd", "chrMakeFlg", {"Y": "yes", "1": "yes", "N": "no", "0": "no"})
        m.soft_delete("tblPrd", style="rename_prefix", predicate="strNm LIKE 'ZZ[_]%'",
                      live_predicate="strNm NOT LIKE 'ZZ[_]%'", injected_rows=int(discontinued))
        m.record_format(table="tblPrd", column="ATTRS", format="kv",
                        paths={"color": "color", "size": "size", "weight": "weight"})
        m.record_type_mismatch(table="tblPrd", column="fltStdCst", stored_as="float", meaning="money")

    # -------------------------------------------------------------------- purchasing
    def purchasing(self) -> None:
        m = self.m
        self.run(
            "CREATE TABLE VNDR (VNDR_ID INT NOT NULL, ACCT_NO VARCHAR(15), VNDR_NM VARCHAR(50),"
            " CR_RTG TINYINT, PREF_FLG CHAR(1), ACTV_FLG CHAR(1))"
        )
        self.run(
            f"INSERT INTO VNDR SELECT BusinessEntityID, AccountNumber, Name, CreditRating, "
            f"CASE WHEN PreferredVendorStatus = 1 THEN CASE {h('BusinessEntityID', 'pref', 2)} "
            f"WHEN 0 THEN 'Y' ELSE '1' END ELSE CASE {h('BusinessEntityID', 'pref', 2)} "
            f"WHEN 0 THEN 'N' ELSE '0' END END, "
            f"CASE WHEN ActiveFlag = 1 THEN 'Y' ELSE 'N' END FROM {CLEAN}.Purchasing.Vendor"
        )
        self.run(
            "CREATE TABLE PO_HDR (PO_ID INT NOT NULL PRIMARY KEY, VNDR_ID INT, EMP_ID INT,"
            " PO_STS TINYINT, PO_DT DATETIME, SHIP_DT CHAR(8) NULL, SUB_TOT FLOAT, TOT_DUE FLOAT)"
        )
        self.run(
            f"INSERT INTO PO_HDR SELECT PurchaseOrderID, VendorID, EmployeeID, Status, OrderDate, "
            f"CONVERT(CHAR(8), ShipDate, 112), SubTotal, TotalDue "
            f"FROM {CLEAN}.Purchasing.PurchaseOrderHeader"
        )
        self.run(
            "CREATE TABLE WO_HDR (WO_ID INT NOT NULL, PRD_ID INT, ORD_QTY INT, SCRP_QTY SMALLINT,"
            " STRT_DT CHAR(8), END_DT CHAR(8) NULL, DUE_DT DATETIME)"
        )
        self.run(
            f"INSERT INTO WO_HDR SELECT WorkOrderID, ProductID, OrderQty, ScrappedQty, "
            f"CONVERT(CHAR(8), StartDate, 112), CONVERT(CHAR(8), EndDate, 112), DueDate "
            f"FROM {CLEAN}.Production.WorkOrder"
        )
        m.rename("VNDR", "vendor")
        m.key("VNDR", ["VNDR_ID"])
        m.rename("PO_HDR", "purchase_order")
        m.key("PO_HDR", ["PO_ID"])
        m.rename("WO_HDR", "work_order")
        m.key("WO_HDR", ["WO_ID"])
        for col, logical in [("VNDR_NM", "name"), ("CR_RTG", "credit_rating"),
                             ("PREF_FLG", "preferred"), ("ACTV_FLG", "active")]:
            m.col("VNDR", col, "vendor", logical)
        for col, logical in [("PO_STS", "status"), ("PO_DT", "order_date"),
                             ("SHIP_DT", "ship_date"), ("TOT_DUE", "total_due")]:
            m.col("PO_HDR", col, "purchase_order", logical)
        m.join(child="PO_HDR", child_cols=["VNDR_ID"], parent="VNDR", parent_cols=["VNDR_ID"])
        m.join(child="WO_HDR", child_cols=["PRD_ID"], parent="tblPrd", parent_cols=["intPrdID"])
        m.enum("PO_HDR", "PO_STS", PO_STATUS)
        m.enum("VNDR", "CR_RTG", CREDIT_RATING)
        m.enum("VNDR", "PREF_FLG", {"Y": "preferred", "1": "preferred", "N": "not preferred",
                                    "0": "not preferred"})
        m.record_type_mismatch(table="PO_HDR", column="TOT_DUE", stored_as="float", meaning="money")
        m.record_type_mismatch(table="WO_HDR", column="STRT_DT", stored_as="char YYYYMMDD", meaning="date")

    # -------------------------------------------------------------------- god table
    def transactions(self) -> None:
        """#7 and #9: one ledger for three kinds of row, and a reference column whose
        target depends on the row's type."""
        m = self.m
        filler = ", ".join(GOD_FILLER)
        self.run(
            "CREATE TABLE TRANS_MSTR (TRANS_ID INT NOT NULL, TRANS_TYP_CD CHAR(1) NOT NULL,"
            " TRANS_DT DATETIME, PRD_ID INT, REF_ID INT, REF_LN INT, QTY INT, ACT_CST FLOAT,"
            " CUST_NO VARCHAR(10) NULL, BR_CD CHAR(3) NULL, VNDR_ID INT NULL, PO_STS TINYINT NULL,"
            f" SCRP_QTY SMALLINT NULL, WO_DUE_DT DATETIME NULL, {filler})"
        )
        self.run(
            f"INSERT INTO TRANS_MSTR (TRANS_ID, TRANS_TYP_CD, TRANS_DT, PRD_ID, REF_ID, REF_LN, QTY,"
            f" ACT_CST, CUST_NO, BR_CD, VNDR_ID, PO_STS, SCRP_QTY, WO_DUE_DT, SRC_SYS_CD, LST_UPD_BY,"
            f" LST_UPD_TS) "
            f"SELECT t.TransactionID, t.TransactionType, t.TransactionDate, t.ProductID, "
            f"t.ReferenceOrderID, t.ReferenceOrderLineID, t.Quantity, t.ActualCost, "
            f"CASE WHEN t.TransactionType = 'S' THEN o.CUST_NO END, "
            f"CASE WHEN t.TransactionType = 'S' THEN o.BR_CD END, "
            f"CASE WHEN t.TransactionType = 'P' THEN po.VendorID END, "
            f"CASE WHEN t.TransactionType = 'P' THEN po.Status END, "
            f"CASE WHEN t.TransactionType = 'W' THEN wo.ScrappedQty END, "
            f"CASE WHEN t.TransactionType = 'W' THEN wo.DueDate END, "
            f"CASE t.TransactionType WHEN 'S' THEN 'SOP' WHEN 'P' THEN 'POP' ELSE 'MFG' END, "
            f"'BATCH', CONVERT(VARCHAR(20), t.ModifiedDate, 120) "
            f"FROM {CLEAN}.Production.TransactionHistory t "
            f"LEFT JOIN SO_HDR o ON t.TransactionType = 'S' AND o.SO_ID = t.ReferenceOrderID "
            f"LEFT JOIN {CLEAN}.Purchasing.PurchaseOrderHeader po "
            f"ON t.TransactionType = 'P' AND po.PurchaseOrderID = t.ReferenceOrderID "
            f"LEFT JOIN {CLEAN}.Production.WorkOrder wo "
            f"ON t.TransactionType = 'W' AND wo.WorkOrderID = t.ReferenceOrderID"
        )
        m.rename("TRANS_MSTR", "inventory_transaction")
        m.key("TRANS_MSTR", ["TRANS_ID"])
        m.record_god_table(
            table="TRANS_MSTR", discriminator="TRANS_TYP_CD",
            entities={code: meaning for code, meaning in TRANS_TYPES.items()},
            columns=14 + len(GOD_FILLER),
        )
        m.enum("TRANS_MSTR", "TRANS_TYP_CD", TRANS_TYPES)
        m.record_overload(
            table="TRANS_MSTR", column="REF_ID", discriminator="TRANS_TYP_CD",
            meanings={"S": "SO_HDR.SO_ID", "P": "PO_HDR.PO_ID", "W": "WO_HDR.WO_ID"},
        )
        m.join(child="TRANS_MSTR", child_cols=["REF_ID"], parent="SO_HDR", parent_cols=["SO_ID"],
               condition="TRANS_TYP_CD = 'S'")
        m.join(child="TRANS_MSTR", child_cols=["REF_ID"], parent="PO_HDR", parent_cols=["PO_ID"],
               condition="TRANS_TYP_CD = 'P'")
        m.join(child="TRANS_MSTR", child_cols=["REF_ID"], parent="WO_HDR", parent_cols=["WO_ID"],
               condition="TRANS_TYP_CD = 'W'")
        m.join(child="TRANS_MSTR", child_cols=["PRD_ID"], parent="tblPrd", parent_cols=["intPrdID"])

    # -------------------------------------------------------------------- audit log
    def audit_log(self) -> None:
        """#16: a large log with no index on the column people filter by. The clean copy
        gets the identical rows with an index, so gold queries over it are cheap."""
        m = self.m
        from gauntlet import mssql

        clean = mssql.connect(CLEAN)
        cur = clean.cursor()
        cur.execute("IF OBJECT_ID('dbo.AuditLog') IS NOT NULL DROP TABLE dbo.AuditLog")
        cur.execute(
            "CREATE TABLE dbo.AuditLog (LogID BIGINT NOT NULL PRIMARY KEY, LogTs DATETIME NOT NULL,"
            " TableName VARCHAR(20), ActionCode CHAR(1), RowKey INT, UserName VARCHAR(20))"
        )
        cur.execute(
            f"WITH n AS (SELECT TOP ({AUDIT_ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g "
            f"FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c) "
            f"INSERT INTO dbo.AuditLog SELECT g, DATEADD(SECOND, CAST(g * 53 AS INT), '2011-01-01'), "
            f"CHOOSE(1 + g % 5, 'CUST_MSTR', 'SO_HDR', 'SO_LN', 'tblPrd', 'PO_HDR'), "
            f"CHOOSE(1 + g % 3, 'I', 'U', 'D'), CAST(g % 30000 AS INT), "
            f"'USR' + RIGHT('00' + CAST(g % 41 AS VARCHAR(3)), 3) FROM n"
        )
        cur.execute("CREATE INDEX IX_AuditLog_LogTs ON dbo.AuditLog (LogTs)")
        clean.close()

        self.run(
            "SELECT LogID AS LOG_ID, LogTs AS LOG_TS, TableName AS TBL_NM, ActionCode AS ACTN_CD,"
            " RowKey AS ROW_KEY, UserName AS USR_NM INTO AUDIT_LOG FROM AW_CLEAN.dbo.AuditLog"
        )
        m.rename("AUDIT_LOG", "audit_log")
        m.key("AUDIT_LOG", ["LOG_ID"])
        m.enum("AUDIT_LOG", "ACTN_CD", {"I": "insert", "U": "update", "D": "delete"})
        m.record_perf_trap(table="AUDIT_LOG", rows=AUDIT_ROWS, missing_index_on="LOG_TS",
                           note="a heap: any date-bounded question is a full scan")

    # ------------------------------------------------------------------- shipped SQL
    def shipped_sql(self) -> None:
        """Views and procedures: some document rules nobody wrote down elsewhere, one is
        broken, one is out of date."""
        status = " ".join(f"WHEN {k} THEN '{v}'" for k, v in ORDER_STATUS.items())
        ship = " ".join(f"WHEN {k} THEN '{v}'" for k, v in SHIP_METHODS.items())
        line = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in PRODUCT_LINES.items())
        self.run(
            "CREATE PROCEDURE usp_RptOrderStatus @from CHAR(8), @to CHAR(8) AS "
            f"SELECT h.BR_CD, h.ORD_NO, c.CFNM1, c.CLNM1, CASE h.ORD_STS {status} END AS STS_DESC, "
            "h.TOT_DUE FROM SO_HDR h JOIN CUST_MSTR c ON c.CUST_ID = CAST(h.CUST_NO AS INT) "
            "WHERE h.ORD_STS <> 9 AND h.ORD_DT BETWEEN @from AND @to"
        )
        self.run(
            "CREATE PROCEDURE usp_ShipSummary AS "
            f"SELECT CASE h.SHIP_VIA {ship} END AS SHIP_METHOD, COUNT(*) AS ORDERS "
            "FROM SO_HDR h WHERE h.ORD_STS <> 9 GROUP BY h.SHIP_VIA"
        )
        self.run(
            "CREATE PROCEDURE usp_LineSales AS "
            f"SELECT CASE p.chrLine {line} END AS PRODUCT_LINE, SUM(l.QTY) AS UNITS "
            "FROM SO_LN l JOIN SO_HDR h ON h.BR_CD = l.BR_CD AND h.ORD_NO = l.ORD_NO "
            "JOIN tblPrd p ON p.intPrdID = l.PRD_ID WHERE h.ORD_STS <> 9 GROUP BY p.chrLine"
        )
        self.run(
            "CREATE VIEW vw_ORD_LINES AS SELECT h.BR_CD, h.ORD_NO, h.SO_ID, l.LN_SEQ, l.PRD_ID, "
            "l.QTY, l.LN_AMT FROM SO_HDR h JOIN SO_LN l ON l.BR_CD = h.BR_CD AND l.ORD_NO = h.ORD_NO"
        )
        self.run(
            "CREATE VIEW vw_CUST_ACTIVE AS SELECT * FROM CUST_MSTR "
            "WHERE ISNULL(UPPER(DEL_FLG), 'N') <> 'Y'"
        )
        # Written before the cents cutover and never updated.
        self.run(
            "CREATE VIEW vw_CUST_REV AS SELECT CUST_NO, SUM(TOT_DUE) AS REV FROM SO_HDR "
            "WHERE ORD_STS <> 9 GROUP BY CUST_NO"
        )
        m = self.m
        for name in ("vw_ORD_LINES", "vw_CUST_ACTIVE"):
            m.classify(name, "core")
        m.classify("vw_CUST_REV", "dead")

    # ------------------------------------------------------------------ documentation
    def documentation(self) -> None:
        """#3: no comments on most things, and a few that are confidently wrong."""
        self.describe("CUST_MSTR", None, "Customer master")
        self.describe("CUST_MSTR", "CFNM1", "Customer first name")
        self.describe("CUST_MSTR", "EML", "Primary email address for every customer")  # wrong
        self.describe("SO_HDR", "TOT_DUE", "Order total in US dollars")                # wrong
        self.describe("SO_HDR", "SHIP_DT", "Date the order was placed")                # wrong
        self.describe("SO_HDR", "ORD_STS", "Order status, see usp_RptOrderStatus")
        self.describe("TRANS_MSTR", None, "Sales order transactions")                  # wrong
        self.describe("tblPrd", "fltStdCst", "Standard cost")
        self.m.scale_notes["misleading_docs"] = (
            "CUST_MSTR.EML (email moved to CUST_CNTCT), SO_HDR.TOT_DUE (cents after the "
            "cutover), SO_HDR.SHIP_DT (ship date, not order date), TRANS_MSTR (holds "
            "purchase and work-order rows too)."
        )

    # ------------------------------------------------------------------------ sprawl
    def sprawl(self) -> None:
        """#4 and #5: yearly archives, staging, snapshots, scratch and backups."""
        m = self.m
        for year in (2011, 2012):
            self.run(f"SELECT * INTO SO_HDR_{year} FROM SO_HDR WHERE ORD_DT LIKE '{year}%'")
            m.classify(f"SO_HDR_{year}", "archive")
        self.run("SELECT * INTO SO_LN_2011 FROM SO_LN WHERE 1 = 0")
        m.classify("SO_LN_2011", "archive")

        # A stale backup, slightly different from the live table.
        self.run(
            "SELECT CUST_ID, ACCT_NO, CFNM1, CLNM1, EML, PHONE, CITY, CTRY, DEL_FLG "
            "INTO CUST_MSTR_BAK_20140312 FROM CUST_MSTR WHERE CUST_ID < 25000"
        )
        self.run("UPDATE CUST_MSTR_BAK_20140312 SET CITY = UPPER(CITY) WHERE CUST_ID % 5 = 0")
        m.classify("CUST_MSTR_BAK_20140312", "backup")
        self.run(
            "SELECT CUST_ID, CFNM1, CLNM1, CAST(NULL AS VARCHAR(25)) AS FAX "
            "INTO CUST_MSTR_OLD FROM CUST_MSTR WHERE 1 = 0"
        )
        m.classify("CUST_MSTR_OLD", "backup")
        # A view over a column that was later dropped: broken, and still there.
        self.run("CREATE VIEW vw_CUST_FAX AS SELECT CUST_ID, FAX FROM CUST_MSTR_OLD")
        self.run("ALTER TABLE CUST_MSTR_OLD DROP COLUMN FAX")
        m.classify("vw_CUST_FAX", "dead")
        self.run("SELECT * INTO tblPrd_bak FROM tblPrd WHERE intPrdID < 700")
        m.classify("tblPrd_bak", "backup")

        self.run(
            "SELECT TOP 500 BR_CD, ORD_NO, CUST_NO, ORD_DT, TOT_DUE, CAST('IMP' AS VARCHAR(6)) "
            "AS LOAD_SRC, CAST(17 AS INT) AS LOAD_BATCH INTO STG_SO_IMPORT FROM SO_HDR ORDER BY SO_ID"
        )
        self.run("SELECT TOP 80 CUST_ID, CFNM1, CLNM1, EML INTO STG_CUST_LOAD FROM CUST_MSTR ORDER BY CUST_ID DESC")
        self.run("SELECT TOP 40 intPrdID, strNm, curLstPrc INTO STG_PRD_FEED FROM tblPrd")
        for name in ("STG_SO_IMPORT", "STG_CUST_LOAD", "STG_PRD_FEED"):
            m.classify(name, "staging")

        # A report snapshot from before the cents cutover was understood.
        self.run(
            "SELECT LEFT(ORD_DT, 6) AS MTH, SUM(TOT_DUE) AS REV INTO RPT_SLS_MTHLY "
            "FROM SO_HDR WHERE ORD_STS <> 9 GROUP BY LEFT(ORD_DT, 6)"
        )
        m.classify("RPT_SLS_MTHLY", "archive")

        for name in ("tmp_dedupe_cust", "tmp_fix_0412", "zz_scratch", "XX_UNUSED"):
            self.run(f"CREATE TABLE {name} (ID INT, VAL VARCHAR(80), NOTE VARCHAR(200))")
            m.classify(name, "dead")

        # A vendor add-on nobody uses any more, in its own schema.
        self.run("EXEC('CREATE SCHEMA acme')")
        for name in ("ACM_CFG", "ACM_SYNC_LOG", "ACM_ITEM_MAP"):
            self.run(f"CREATE TABLE acme.{name} (ID INT, K VARCHAR(40), V VARCHAR(200))")
            m.classify(f"acme.{name}", "dead")

        prefixes = [("STG_", "staging"), ("RPT_", "archive"), ("tmp_", "dead"),
                    ("WRK_", "dead"), ("BAK_", "backup"), ("HIST_", "archive")]
        subjects = ["CUST", "SO", "PRD", "INV", "GL", "AR", "PO", "VND"]
        for i in range(60):
            prefix, klass = prefixes[i % len(prefixes)]
            name = f"{prefix}{subjects[i % len(subjects)]}_{i:03d}"
            width = 3 + (i * 7) % 5
            cols = ", ".join(f"F{j:02d} VARCHAR(40)" for j in range(width))
            self.run(f"CREATE TABLE {name} (ID INT, {cols})")
            if i % 4 == 0:
                self.run(f"INSERT INTO {name} (ID) SELECT TOP {1 + i % 5} object_id FROM sys.objects")
            m.classify(name, klass)

    def classify_core(self) -> None:
        for name in ("TERR", "SLSPRSN", "STR_MSTR", "CUST_MSTR", "CUST_CNTCT", "SO_HDR", "SO_LN",
                     "PCTGRY", "PSCAT", "tblPrd", "VNDR", "PO_HDR", "WO_HDR", "TRANS_MSTR",
                     "AUDIT_LOG"):
            self.m.classify(name, "core")

    def finish(self) -> None:
        self.run("EXEC sp_updatestats")
        self.conn.close()


def legacify(out_dir: Path, *, restore: bool = True) -> Path:
    from gauntlet import mssql

    mssql.wait_until_ready()
    if restore or not mssql.exists(CLEAN):
        mssql.restore_clean(CLEAN)
    # Compatibility level 100 is SQL Server 2008: what the tsql2008 profile assumes.
    mssql.recreate(LEGACY, compatibility=100)

    b = Builder()
    b.m.scale_notes.update({
        "treatment": "Full treatment: all sixteen anti-patterns of PLAN.md 3.2.",
        "sprawl": "~80 extra tables rather than ~300, so the naive baseline's whole-schema "
                  "prompt fits the free tier's per-request token limit.",
        "audit_log": f"{AUDIT_ROWS:,} rows rather than ~8M.",
        "god_table": "TRANS_MSTR is AdventureWorks' TransactionHistory: sales, purchase and "
                     "work-order rows discriminated by TRANS_TYP_CD.",
    })
    b.territories_and_people()
    b.customers()
    b.orders()
    b.products()
    b.purchasing()
    b.transactions()
    b.audit_log()
    b.shipped_sql()
    b.documentation()
    b.sprawl()
    b.classify_core()
    b.finish()

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / f"{LEGACY}.manifest.json"
    b.m.write(manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacify AdventureWorks into HALCYON.")
    here = Path(__file__).resolve().parents[1]
    parser.add_argument("--out", type=Path, default=here / "build")
    parser.add_argument("--skip-restore", action="store_true",
                        help="reuse an existing AW_CLEAN instead of restoring it again")
    args = parser.parse_args()
    manifest = legacify(args.out, restore=not args.skip_restore)

    from gauntlet import mssql

    conn = mssql.connect(LEGACY)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sys.tables")
    tables = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sys.views")
    views = cur.fetchone()[0]
    conn.close()
    print(f"HALCYON: {tables} tables, {views} views; manifest at {manifest}")


if __name__ == "__main__":
    main()
