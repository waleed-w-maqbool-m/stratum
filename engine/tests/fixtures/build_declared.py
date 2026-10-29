"""A small database for relationship provenance, with one relationship of each origin.

* `orders.cust_id -> customer.cust_id`   declared, and three orders point at customers
  that do not exist (SQLite does not enforce foreign keys unless asked to)
* `order_line.order_id -> orders.order_id`  declared and clean
* `customer_profile.cust_id -> customer.cust_id`  declared, and one-to-one
* `stock.loc -> warehouse.wh_id`          declared nowhere; written in the view `v_stock`
* `invoice.order_id -> orders.order_id`   declared nowhere, written nowhere; the data
  and the names are the only evidence

The names of the view-only pair are deliberately unrelated, so inference from names and
values could not find it: it can only be known from the view.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ORPHAN_CUSTOMERS = (9001, 9002, 9003)


def build(path: Path) -> Path:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customer (cust_id INTEGER PRIMARY KEY, cust_name TEXT);
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            cust_id INTEGER REFERENCES customer(cust_id),
            placed TEXT
        );
        CREATE TABLE order_line (
            line_id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(order_id),
            qty INTEGER
        );
        CREATE TABLE customer_profile (
            cust_id INTEGER PRIMARY KEY REFERENCES customer(cust_id), tier TEXT
        );
        CREATE TABLE warehouse (wh_id INTEGER PRIMARY KEY, wh_name TEXT);
        CREATE TABLE stock (stk_id INTEGER PRIMARY KEY, loc INTEGER, on_hand INTEGER);
        CREATE TABLE invoice (inv_id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);
        CREATE VIEW v_stock AS
            SELECT s.stk_id, w.wh_name FROM stock s JOIN warehouse w ON s.loc = w.wh_id;
        """
    )
    conn.executemany(
        "INSERT INTO customer VALUES (?, ?)", [(i, f"Customer {i}") for i in range(1, 61)]
    )
    orders = []
    for i in range(1, 201):
        if i <= 10:
            cust = None                                  # an order with no customer yet
        elif i <= 10 + len(ORPHAN_CUSTOMERS):
            cust = ORPHAN_CUSTOMERS[i - 11]              # a customer that was deleted
        else:
            cust = (i % 60) + 1
        orders.append((i, cust, f"2024-01-{(i % 28) + 1:02d}"))
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?)", orders)
    conn.executemany(
        "INSERT INTO order_line VALUES (?, ?, ?)",
        [(i, (i % 200) + 1, (i % 5) + 1) for i in range(1, 401)],
    )
    conn.executemany(
        "INSERT INTO customer_profile VALUES (?, ?)", [(i, "gold") for i in range(1, 41)]
    )
    conn.executemany(
        "INSERT INTO warehouse VALUES (?, ?)", [(i, f"Warehouse {i}") for i in range(1, 13)]
    )
    conn.executemany(
        "INSERT INTO stock VALUES (?, ?, ?)", [(i, (i % 12) + 1, i * 3) for i in range(1, 101)]
    )
    conn.executemany(
        "INSERT INTO invoice VALUES (?, ?, ?)",
        [(i, (i % 100) + 1, float(i) * 1.5) for i in range(1, 151)],
    )
    conn.commit()
    conn.close()
    return path
