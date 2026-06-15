#!/usr/bin/env python3
"""
Scrape MSP billing codes from https://www.dr-bill.ca/msp_billing_codes
and store them in a SQLite database.

Usage:
    python scrape_msp_codes.py
    python scrape_msp_codes.py --format csv --output msp_codes.csv
    python scrape_msp_codes.py --search "consultation"
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.dr-bill.ca"
MSP_CODES_URL = f"{BASE_URL}/msp_billing_codes"

# The four top-level category pages that together contain every code.
CATEGORY_PAGES = [
    ("MSP Billing Codes", f"{BASE_URL}/msp_billing_codes/specialty/msp-billing-codes-by-specialty/"),
    ("Allied Health Services", f"{BASE_URL}/msp_billing_codes/specialty/allied-health-services/"),
    ("Dental Billing Codes", f"{BASE_URL}/msp_billing_codes/specialty/dental-billing-codes/"),
    ("WorkSafe BC", f"{BASE_URL}/msp_billing_codes/specialty/worksafe-bc/"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}


def fetch(url: str, retries: int = 3, backoff: float = 1.0) -> str:
    """Fetch a URL with simple retry/backoff logic."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries:
                raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
            time.sleep(backoff * attempt)
    raise RuntimeError("unreachable")


def extract_code_text(code_td) -> str:
    """Return just the billing code number from a code cell."""
    # On category pages the code is the last direct text node of the td
    # (inactive codes include extra tooltip/button markup inside the td).
    direct_texts = [
        s.strip() for s in code_td.find_all(string=True, recursive=False) if s.strip()
    ]
    if direct_texts:
        return direct_texts[-1]

    # Active search-result codes are wrapped in <span class="bg-yellow">.
    span = code_td.find("span", class_="bg-yellow")
    if span:
        return span.get_text(strip=True)

    return code_td.get_text(strip=True)


def is_inactive(row) -> bool:
    """Detect inactive codes by the grey text colour applied to the row cells."""
    for td in row.find_all("td"):
        classes = td.get("class", [])
        if any("text-[#C4C4C4]" in cls for cls in classes):
            return True
    return False


def parse_category_page(url: str, top_category: str):
    """Yield code records from a category page."""
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"data-codestable": ""})
    if not table:
        print(f"Warning: no code table found on {url}", file=sys.stderr)
        return

    tbody = table.find("tbody")
    if not tbody:
        print(f"Warning: no table body found on {url}", file=sys.stderr)
        return

    current_subcategory = None
    for tr in tbody.find_all("tr"):
        # Subcategory heading row
        if tr.get("data-categorytitle") == "":
            h2 = tr.find("h2")
            if h2:
                current_subcategory = h2.get_text(strip=True)
            continue

        code_td = tr.find("td", {"data-prop": "code"})
        if not code_td:
            continue

        code = extract_code_text(code_td)
        if not code:
            continue

        title_td = tr.find("td", {"data-prop": "title"})
        fee_td = tr.find("td", {"data-prop": "fee"})
        anes_td = tr.find("td", {"data-prop": "anes"})

        description = title_td.get_text(strip=True) if title_td else ""
        fee = fee_td.get_text(strip=True) if fee_td else ""
        anes = anes_td.get_text(strip=True) if anes_td else ""
        inactive = is_inactive(tr)

        yield {
            "code": code,
            "description": description,
            "fee": fee,
            "anes": anes,
            "subcategory": current_subcategory or "",
            "top_category": top_category,
            "inactive": inactive,
            "source_url": url,
        }


def scrape_all_codes():
    """Scrape every code from the four top-level category pages."""
    all_records = []
    for top_category, url in CATEGORY_PAGES:
        print(f"Fetching {top_category}...")
        records = list(parse_category_page(url, top_category))
        print(f"  -> {len(records)} codes")
        all_records.extend(records)
        time.sleep(0.5)

    # Deduplicate by code, preserving category / subcategory info.
    by_code = defaultdict(list)
    for rec in all_records:
        by_code[rec["code"]].append(rec)

    merged = []
    for code, recs in by_code.items():
        # Pick the first record as the canonical one and merge categories.
        canonical = dict(recs[0])
        canonical["top_categories"] = ", ".join(
            dict.fromkeys(r["top_category"] for r in recs)
        )
        canonical["subcategories"] = ", ".join(
            dict.fromkeys(r["subcategory"] for r in recs if r["subcategory"])
        )
        merged.append(canonical)

    return merged


def init_db(conn: sqlite3.Connection):
    """Create the codes table if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            description TEXT,
            fee TEXT,
            anes TEXT,
            subcategory TEXT,
            top_category TEXT,
            top_categories TEXT,
            subcategories TEXT,
            inactive INTEGER DEFAULT 0,
            source_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_codes_code ON codes(code)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_codes_description ON codes(description)
        """
    )
    conn.commit()


def save_to_sqlite(records, db_path: str):
    """Save records to a SQLite database."""
    conn = sqlite3.connect(db_path)
    init_db(conn)

    with conn:
        for rec in records:
            conn.execute(
                """
                INSERT INTO codes (
                    code, description, fee, anes, subcategory, top_category,
                    top_categories, subcategories, inactive, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    description=excluded.description,
                    fee=excluded.fee,
                    anes=excluded.anes,
                    subcategory=excluded.subcategory,
                    top_category=excluded.top_category,
                    top_categories=excluded.top_categories,
                    subcategories=excluded.subcategories,
                    inactive=excluded.inactive,
                    source_url=excluded.source_url,
                    created_at=CURRENT_TIMESTAMP
                """,
                (
                    rec["code"],
                    rec["description"],
                    rec["fee"],
                    rec["anes"],
                    rec["subcategory"],
                    rec["top_category"],
                    rec["top_categories"],
                    rec["subcategories"],
                    1 if rec["inactive"] else 0,
                    rec["source_url"],
                ),
            )
    conn.close()


def save_to_csv(records, csv_path: str):
    """Save records to a CSV file."""
    fieldnames = [
        "code",
        "description",
        "fee",
        "anes",
        "subcategory",
        "top_category",
        "top_categories",
        "subcategories",
        "inactive",
        "source_url",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            row = {k: rec.get(k, "") for k in fieldnames}
            row["inactive"] = "1" if rec.get("inactive") else "0"
            writer.writerow(row)


def save_to_json(records, json_path: str):
    """Save records to a JSON file."""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def search_codes(db_path: str, query: str):
    """Simple full-text search against the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    like = f"%{query}%"
    cur.execute(
        """
        SELECT code, description, fee, anes, top_categories, subcategories, inactive
        FROM codes
        WHERE code LIKE ? OR description LIKE ? OR top_categories LIKE ? OR subcategories LIKE ?
        ORDER BY code
        """,
        (like, like, like, like),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"No results for {query!r}")
        return

    print(f"Found {len(rows)} result(s) for {query!r}:\n")
    for row in rows:
        status = " (inactive)" if row["inactive"] else ""
        print(f"{row['code']}{status}: {row['description']}")
        print(f"  Fee: {row['fee'] or 'n/a'}  Anes: {row['anes'] or 'n/a'}")
        print(f"  Categories: {row['top_categories']} / {row['subcategories']}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Scrape MSP billing codes from dr-bill.ca into a database."
    )
    parser.add_argument(
        "--output",
        "-o",
        default="msp_codes.db",
        help="Output file (default: msp_codes.db). Extension determines format if --format is omitted.",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["sqlite", "csv", "json"],
        help="Output format. Inferred from --output if not given.",
    )
    parser.add_argument(
        "--search",
        "-s",
        metavar="QUERY",
        help="Search the existing database instead of scraping.",
    )
    parser.add_argument(
        "--no-scrape",
        action="store_true",
        help="Skip scraping; only valid with --search.",
    )
    args = parser.parse_args()

    fmt = args.format
    if fmt is None:
        if args.output.lower().endswith(".csv"):
            fmt = "csv"
        elif args.output.lower().endswith(".json"):
            fmt = "json"
        else:
            fmt = "sqlite"

    if args.search:
        if not args.no_scrape and not args.output.endswith((".db", ".sqlite", ".sqlite3")):
            # Default to sqlite for the database when searching.
            db_path = "msp_codes.db"
        else:
            db_path = args.output
        if not args.no_scrape:
            records = scrape_all_codes()
            save_to_sqlite(records, db_path)
        search_codes(db_path, args.search)
        return

    records = scrape_all_codes()
    print(f"\nTotal unique codes scraped: {len(records)}")

    if fmt == "sqlite":
        save_to_sqlite(records, args.output)
        print(f"Saved to SQLite database: {args.output}")
    elif fmt == "csv":
        save_to_csv(records, args.output)
        print(f"Saved to CSV: {args.output}")
    elif fmt == "json":
        save_to_json(records, args.output)
        print(f"Saved to JSON: {args.output}")


if __name__ == "__main__":
    main()
