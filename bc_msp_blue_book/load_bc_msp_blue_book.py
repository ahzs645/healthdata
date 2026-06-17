#!/usr/bin/env python3
"""
BC MSP Blue Book Loader
=======================
Loads the BC Medical Services Plan Blue Book payments CSV into SQLite. The source
is the BC Data Catalogue MSP Blue Book CSV covering fiscal years 2014/2015
through 2024/2025. The source column is named "Payments to Practitioners", but
it also contains organizations such as groups, clinics, hospitals, health
authorities, laboratories, and diagnostic facilities.

Usage:
    python load_bc_msp_blue_book.py
    python load_bc_msp_blue_book.py --source /path/to/msp-blue-book-2014.2015-to-2024.2025.csv
    python load_bc_msp_blue_book.py --db bc_msp_blue_book.db --source source.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_URL = (
    "https://catalogue.data.gov.bc.ca/dataset/c7413d9f-112e-4a26-bbd1-ae2711856629/"
    "resource/42fb42eb-50a2-4e80-827e-35c075f94a51/download/"
    "msp-blue-book-2014.2015-to-2024.2025.csv"
)

HEADER_MAP = {
    "Payments to Practitioners": "practitioner_name",
    "Amount": "amount",
    "Fiscal Year": "fiscal_year",
}

PAYEE_PATTERNS = [
    ("source_total", re.compile(r"^total expenditures$", re.I)),
    ("health_authority", re.compile(r"\bhealth authority\b", re.I)),
    ("hospital", re.compile(r"\bhospital|hospitals\b", re.I)),
    (
        "diagnostic_facility",
        re.compile(r"\b(diagnostic|diagnostics|imaging|x[- ]?ray|ultrasound|mammography|laborator|laboratories|lab)\b", re.I),
    ),
    ("clinic", re.compile(r"\bclinic|clinics\b", re.I)),
    (
        "organization",
        re.compile(
            r"\b(group|associates|corporation|corp\.?|inc\.?|ltd\.?|centre|center|medical|health|practice|physicians?)\b",
            re.I,
        ),
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_amount(value: str) -> float | None:
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    return float(value)


def fiscal_start_year(fiscal_year: str) -> int | None:
    if not fiscal_year:
        return None
    try:
        return int(fiscal_year.split("/", 1)[0])
    except ValueError:
        return None


def payee_type(payee_name: str | None, amount: float | None) -> str:
    if not payee_name:
        return "blank"
    if amount is None:
        return "blank_amount"
    for kind, pattern in PAYEE_PATTERNS:
        if pattern.search(payee_name):
            return kind
    return "practitioner"


def download_csv(url: str) -> str:
    source_name = os.path.basename(urlparse(url).path) or "bc_msp_blue_book.csv"
    path = os.path.join(tempfile.mkdtemp(prefix="bc_msp_blue_book_"), source_name)
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; bcdatamapper/1.0)",
            "Accept": "text/csv,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=120) as response, open(path, "wb") as out:
        out.write(response.read())
    return path


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS fiscal_year_summary;
        DROP TABLE IF EXISTS practitioner_payments;

        CREATE TABLE IF NOT EXISTS msp_blue_book_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payee_name TEXT,
            amount REAL,
            payee_type TEXT NOT NULL,
            fiscal_year TEXT NOT NULL,
            fiscal_start_year INTEGER,
            source_file TEXT,
            loaded_at TEXT
        );

        CREATE TABLE IF NOT EXISTS fiscal_year_summary (
            fiscal_year TEXT PRIMARY KEY,
            fiscal_start_year INTEGER,
            practitioner_rows INTEGER NOT NULL,
            organization_rows INTEGER NOT NULL,
            rows_with_amount INTEGER NOT NULL,
            practitioner_total_amount REAL NOT NULL,
            organization_total_amount REAL NOT NULL,
            source_total_amount REAL,
            average_amount REAL,
            max_amount REAL,
            loaded_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_msp_blue_book_payee
            ON msp_blue_book_payments(payee_name);
        CREATE INDEX IF NOT EXISTS idx_msp_blue_book_year
            ON msp_blue_book_payments(fiscal_year);
        CREATE INDEX IF NOT EXISTS idx_msp_blue_book_payee_type
            ON msp_blue_book_payments(payee_type);
        """
    )
    conn.commit()


def validate_headers(fieldnames: list[str] | None) -> None:
    missing = [source for source in HEADER_MAP if not fieldnames or source not in fieldnames]
    if missing:
        raise SystemExit(f"CSV is missing required header(s): {', '.join(missing)}")


def load_csv(conn: sqlite3.Connection, path: str) -> tuple[int, int]:
    source_file = os.path.basename(path)
    loaded_at = now()
    total_rows = 0
    blank_amount_rows = 0

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        validate_headers(reader.fieldnames)

        with conn:
            conn.execute("DELETE FROM fiscal_year_summary")
            conn.execute("DELETE FROM msp_blue_book_payments")

            for row in reader:
                payee_name = (row["Payments to Practitioners"] or "").strip() or None
                fiscal_year = (row["Fiscal Year"] or "").strip()
                amount = clean_amount(row["Amount"])
                if amount is None:
                    blank_amount_rows += 1
                payment_payee_type = payee_type(payee_name, amount)
                conn.execute(
                    """
                    INSERT INTO msp_blue_book_payments (
                        payee_name, amount, payee_type, fiscal_year, fiscal_start_year,
                        source_file, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payee_name,
                        amount,
                        payment_payee_type,
                        fiscal_year,
                        fiscal_start_year(fiscal_year),
                        source_file,
                        loaded_at,
                    ),
                )
                total_rows += 1

            conn.execute(
                """
                INSERT INTO fiscal_year_summary (
                    fiscal_year, fiscal_start_year, practitioner_rows, organization_rows, rows_with_amount,
                    practitioner_total_amount, organization_total_amount, source_total_amount,
                    average_amount, max_amount, loaded_at
                )
                SELECT
                    fiscal_year,
                    fiscal_start_year,
                    SUM(CASE WHEN payee_type = 'practitioner' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN payee_type IN ('clinic', 'diagnostic_facility', 'health_authority', 'hospital', 'organization') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN payee_type NOT IN ('blank', 'blank_amount', 'source_total') AND amount IS NOT NULL THEN 1 ELSE 0 END),
                    COALESCE(SUM(CASE WHEN payee_type = 'practitioner' THEN amount ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN payee_type IN ('clinic', 'diagnostic_facility', 'health_authority', 'hospital', 'organization') THEN amount ELSE 0 END), 0),
                    MAX(CASE WHEN payee_type = 'source_total' THEN amount END),
                    AVG(CASE WHEN payee_type NOT IN ('blank', 'blank_amount', 'source_total') THEN amount END),
                    MAX(CASE WHEN payee_type NOT IN ('blank', 'blank_amount', 'source_total') THEN amount END),
                    ?
                FROM msp_blue_book_payments
                GROUP BY fiscal_year, fiscal_start_year
                """,
                (loaded_at,),
            )

    return total_rows, blank_amount_rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Load the BC MSP Blue Book CSV into SQLite.")
    ap.add_argument("--db", default="bc_msp_blue_book.db")
    ap.add_argument("--source", help="Local CSV path. If omitted, downloads from the BC Data Catalogue.")
    ap.add_argument("--url", default=DEFAULT_URL, help="CSV download URL used when --source is omitted.")
    args = ap.parse_args()

    temp_source = None
    source = args.source
    if not source:
        print(f"Downloading source CSV from {args.url}")
        temp_source = download_csv(args.url)
        source = temp_source

    try:
        conn = sqlite3.connect(args.db)
        init_db(conn)
        total_rows, blank_amount_rows = load_csv(conn, source)

        print(f"\n{args.db}: loaded {total_rows:,} rows from {os.path.basename(source)}")
        if blank_amount_rows:
            print(f"Rows with blank amount: {blank_amount_rows:,}")

        print("\nPayee-type counts:")
        for kind, row_count in conn.execute(
            """
            SELECT payee_type, COUNT(*)
            FROM msp_blue_book_payments
            GROUP BY payee_type
            ORDER BY payee_type
            """
        ):
            print(f"  {kind:20s} {row_count:>7,}")

        print("\nFiscal-year summary:")
        for fiscal_year, practitioner_rows, organization_rows, practitioner_total, organization_total, source_total in conn.execute(
            """
            SELECT fiscal_year, practitioner_rows, organization_rows,
                   practitioner_total_amount, organization_total_amount, source_total_amount
            FROM fiscal_year_summary
            ORDER BY fiscal_start_year
            """
        ):
            if source_total is None:
                print(
                    f"  {fiscal_year}: {practitioner_rows:>6,} practitioners, "
                    f"{organization_rows:>4,} organizations, "
                    f"${practitioner_total + organization_total:>16,.2f}"
                )
            else:
                print(
                    f"  {fiscal_year}: {practitioner_rows:>6,} practitioners, "
                    f"{organization_rows:>4,} organizations, "
                    f"${practitioner_total + organization_total:>16,.2f} "
                    f"(source total ${source_total:,.2f})"
                )
        conn.close()
    finally:
        if temp_source:
            os.unlink(temp_source)
            os.rmdir(os.path.dirname(temp_source))


if __name__ == "__main__":
    main()
