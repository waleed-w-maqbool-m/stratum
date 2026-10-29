"""A small legacy database with one of each gap, and no declared keys at all.

* `line.order_id -> orders.order_id`      clean and obvious: a key nothing enforces
* `orders.cust_id -> customer.cust_id`    25% of orders point at deleted customers
* `ship.carrier_id -> carrier.carrier_id` most carriers were never in the table
* `invoice.vendor_id -> vendor`           the live table lost its rows; the backup has them
* `ticket.stat_cd`                        codes decoded by a view's CASE, no lookup table
* `audit_blob`                            relates to nothing
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def build(path: Path) -> Path:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customer (cust_id INTEGER PRIMARY KEY, cust_name TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, cust_id INTEGER, placed TEXT);
        CREATE TABLE line (line_id INTEGER PRIMARY KEY, order_id INTEGER, qty INTEGER);
        CREATE TABLE carrier (carrier_id INTEGER PRIMARY KEY, carrier_name TEXT);
        CREATE TABLE ship (ship_id INTEGER PRIMARY KEY, carrier_id INTEGER, weight REAL);
        CREATE TABLE vendor (vendor_id INTEGER PRIMARY KEY, vendor_name TEXT);
        CREATE TABLE vendor_bak_20240101 (vendor_id INTEGER PRIMARY KEY, vendor_name TEXT);
        CREATE TABLE invoice (inv_id INTEGER PRIMARY KEY, vendor_id INTEGER, amount REAL);
        CREATE TABLE ticket (ticket_id INTEGER PRIMARY KEY, stat_cd TEXT, opened TEXT);
        CREATE TABLE audit_blob (blob_id INTEGER PRIMARY KEY, payload TEXT);
        CREATE VIEW v_ticket AS
            SELECT ticket_id,
                   CASE stat_cd WHEN 'O' THEN 'Open' WHEN 'C' THEN 'Closed'
                                WHEN 'P' THEN 'Pending' END AS status
            FROM ticket;
        """
    )
    many = conn.executemany
    many("INSERT INTO customer VALUES (?, ?)", [(i, f"Customer {i}") for i in range(1, 61)])
    many("INSERT INTO orders VALUES (?, ?, ?)",
         [(i, (i % 80) + 1, f"2024-02-{(i % 28) + 1:02d}") for i in range(1, 241)])
    many("INSERT INTO line VALUES (?, ?, ?)",
         [(i, (i % 240) + 1, (i % 5) + 1) for i in range(1, 501)])
    many("INSERT INTO carrier VALUES (?, ?)", [(i, f"Carrier {i}") for i in range(1, 16)])
    many("INSERT INTO ship VALUES (?, ?, ?)",
         [(i, (i % 40) + 1, float(i)) for i in range(1, 161)])
    many("INSERT INTO vendor VALUES (?, ?)", [(i, f"Vendor {i}") for i in range(1, 11)])
    many("INSERT INTO vendor_bak_20240101 VALUES (?, ?)",
         [(i, f"Vendor {i}") for i in range(1, 51)])
    many("INSERT INTO invoice VALUES (?, ?, ?)",
         [(i, (i % 50) + 1, float(i)) for i in range(1, 201)])
    many("INSERT INTO ticket VALUES (?, ?, ?)",
         [(i, "OCP"[i % 3], f"2024-03-{(i % 28) + 1:02d}") for i in range(1, 121)])
    many("INSERT INTO audit_blob VALUES (?, ?)", [(i, f"payload {i}") for i in range(1, 31)])
    conn.commit()
    conn.close()
    return path
