#!/usr/bin/env python3
"""
BC / Prince George Permit & Regulatory Fee Scraper
==================================================
Collects the City of Prince George Comprehensive Fees and Charges Bylaw and the
Building Bylaw, and extracts fee lines (description + dollar amount) into SQLite.
Feeds the NHHR cost-benefit analysis capital cost line "Licensing, permits and
regulatory compliance".

Source documents are consolidated bylaw PDFs on princegeorge.ca; the exact links
change as bylaws are re-consolidated, so update SOURCES (or pass --pdf-url) when
they move.

Usage:
    python scrape_bc_permit_fees.py
    python scrape_bc_permit_fees.py --db bc_permit_fees.db
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests

try:
    import pdfplumber
except ImportError:
    import sys
    sys.exit("pdfplumber is required: pip install pdfplumber")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}

SOURCES = [
    ("City of Prince George", "Comprehensive Fees and Charges Bylaw 7557",
     "https://www.princegeorge.ca/sites/default/files/2025-12/BL7557_CONSOLIDATED_2025-12-15.pdf"),
    ("City of Prince George", "Building Bylaw 8922",
     "https://www.princegeorge.ca/media/2460"),
]

# A fee line: "<description> $1,234.56" or "<description> 152.00" (first amount on the line wins;
# trailing text like "plus $6.50 per..." is ignored). Description must end in a letter or ).
FEE_RE = re.compile(r"^(?P<desc>.+?[A-Za-z\)\.])\s+\$?(?P<amt>\d{1,3}(?:,\d{3})*\.\d{2})\b")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            authority TEXT, title TEXT, url TEXT, local_path TEXT,
            bytes INTEGER, pages INTEGER, fetched_at TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS fee_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            authority TEXT, source_title TEXT, source_url TEXT,
            page INTEGER, description TEXT, amount REAL, scraped_at TEXT
        );
        """
    )
    conn.commit()


def parse_fees(data: bytes):
    out = []
    pages = 0
    with pdfplumber.open(io.BytesIO(data)) as doc:
        pages = len(doc.pages)
        for pageno, page in enumerate(doc.pages, 1):
            for raw in (page.extract_text() or "").split("\n"):
                m = FEE_RE.match(raw.strip())
                if not m:
                    continue
                desc = m.group("desc").strip(" .")
                if len(desc) < 4:
                    continue
                try:
                    amt = float(m.group("amt").replace(",", ""))
                except ValueError:
                    continue
                out.append((pageno, desc[:160], amt))
    return pages, out


def main():
    ap = argparse.ArgumentParser(description="Scrape Prince George permit/fee bylaws into SQLite.")
    ap.add_argument("--db", default="bc_permit_fees.db")
    ap.add_argument("--docs-dir", default="docs")
    args = ap.parse_args()
    os.makedirs(args.docs_dir, exist_ok=True)

    conn = sqlite3.connect(args.db)
    init_db(conn)
    scraped_at = now()
    total = 0

    with conn:
        for authority, title, url in SOURCES:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=90)
                resp.raise_for_status()
                data = resp.content
                fn = os.path.join(args.docs_dir, (url.rsplit("/", 1)[-1] or title) + (".pdf" if not url.endswith(".pdf") else ""))
                with open(fn, "wb") as f:
                    f.write(data)
                pages, fees = parse_fees(data)
                for pageno, desc, amt in fees:
                    conn.execute(
                        "INSERT INTO fee_lines (authority,source_title,source_url,page,description,amount,scraped_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (authority, title, url, pageno, desc, amt, scraped_at),
                    )
                total += len(fees)
                status = "ok"
                print(f"{title:42s} {len(data):>9,} bytes  {pages}p  {len(fees)} fee lines")
            except Exception as exc:
                data, fn, pages, status = b"", "", 0, f"error: {exc}"
                print(f"{title:42s} FAILED: {exc}")
            conn.execute(
                "INSERT INTO documents (authority,title,url,local_path,bytes,pages,fetched_at,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (authority, title, url, fn, len(data), pages, scraped_at, status),
            )

    print(f"\nTotal fee lines: {total} -> {args.db}")
    cur = conn.execute(
        "SELECT description, amount FROM fee_lines WHERE description LIKE '%building permit%' OR description LIKE '%permit%' "
        "ORDER BY amount DESC LIMIT 8"
    )
    sample = cur.fetchall()
    if sample:
        print("Sample permit-related fees:")
        for desc, amt in sample:
            print(f"  ${amt:>10,.2f}  {desc}")
    conn.close()


if __name__ == "__main__":
    main()
