#!/usr/bin/env python3
"""
UNBC Statement of Financial Information (SOFI) Scraper
======================================================
Downloads UNBC's annual Statement of Financial Information PDF and extracts the
SCHEDULE OF EMPLOYEE REMUNERATION (every employee paid over the BC FIA threshold,
~$75k) into SQLite. This is the public salary benchmark for the NHHR cost-benefit
analysis: employment income (benefit lines B19-B21), hiring/replacement-cost
benchmark (B5), and cost-side staffing salaries.

Source: https://www.unbc.ca/finance/financial-statements  (latest SOFI PDF)

The SOFI PDF text-extracts with the leading digit of 100k+ amounts split off by a
space (e.g. "1 34,809.76" = 134,809.76); the parser repairs this before reading
the two trailing money columns (remuneration, expenses).

Usage:
    python scrape_unbc_sofi.py
    python scrape_unbc_sofi.py --pdf-url <direct link to a SOFI pdf>
    python scrape_unbc_sofi.py --db unbc_sofi.db --keep-pdf
"""
from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
from datetime import datetime, timezone

import requests

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber is required: pip install pdfplumber")

BASE = "https://www.unbc.ca"
STATEMENTS_PAGE = f"{BASE}/finance/financial-statements"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}

REMUN_HEADER = "SCHEDULE OF EMPLOYEE REMUNERATION"
FACULTY_RE = re.compile(r"\b(Prof\b|Professor|Lecturer|Instructor|Chair\b|Dean\b|Librarian)", re.I)
# After repair: "<name + position>  <remuneration>  <expenses|->"
ROW_RE = re.compile(
    r"^(?P<np>.+?)\s+(?P<remun>\d{1,3}(?:,\d{3})*\.\d{2})\s+(?P<exp>\d{1,3}(?:,\d{3})*\.\d{2}|-)\s*$"
)
YEAR_RE = re.compile(r"(20\d{2})")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_sofi_url() -> str:
    """Find the most recent SOFI PDF link on the UNBC financial-statements page."""
    r = requests.get(STATEMENTS_PAGE, headers=HEADERS, timeout=40)
    r.raise_for_status()
    links = re.findall(r'href="([^"]+\.pdf[^"]*)"', r.text, re.I)
    sofi = [l for l in links if "sofi" in l.lower()]
    if not sofi:
        raise RuntimeError("No SOFI PDF link found; pass --pdf-url explicitly.")
    url = sofi[0]
    return url if url.startswith("http") else BASE + url


def repair(line: str) -> str:
    """Re-join a leading digit that pdfplumber split off a 100k+ amount."""
    return re.sub(r"(?<=\d) (?=\d{2},\d{3}\.\d{2})", "", line)


def money(s: str):
    return None if s == "-" else float(s.replace(",", ""))


def parse_remuneration(pdf_bytes: bytes):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        for pageno, page in enumerate(doc.pages, 1):
            text = page.extract_text() or ""
            if REMUN_HEADER not in text.upper():
                continue
            for raw in text.split("\n"):
                line = repair(raw.strip())
                if not line or line.upper().startswith("EMPLOYEE NAME"):
                    continue
                m = ROW_RE.match(line)
                if not m:
                    continue
                np = m.group("np").strip()
                # Skip header/total artifacts that slipped through.
                if "," not in np or np.upper().startswith("TOTAL"):
                    continue
                rows.append(
                    {
                        "name_position": np,
                        "surname": np.split(",")[0].strip(),
                        "remuneration": money(m.group("remun")),
                        "expenses": money(m.group("exp")),
                        "is_faculty": 1 if FACULTY_RE.search(np) else 0,
                        "page": pageno,
                    }
                )
    return rows


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_remuneration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_year TEXT,
            name_position TEXT,
            surname TEXT,
            remuneration REAL,
            expenses REAL,
            is_faculty INTEGER,
            page INTEGER,
            source_url TEXT,
            scraped_at TEXT,
            UNIQUE(source_year, name_position, remuneration)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_remun ON employee_remuneration(remuneration)")
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Scrape UNBC SOFI employee remuneration into SQLite.")
    ap.add_argument("--db", default="unbc_sofi.db", help="Output SQLite DB (default: unbc_sofi.db)")
    ap.add_argument("--pdf-url", help="Direct SOFI PDF URL (default: auto-discover latest).")
    ap.add_argument("--keep-pdf", action="store_true", help="Save the downloaded PDF next to the DB.")
    args = ap.parse_args()

    url = args.pdf_url or discover_sofi_url()
    print(f"SOFI PDF: {url}")
    pdf_bytes = requests.get(url, headers=HEADERS, timeout=90).content
    print(f"  {len(pdf_bytes):,} bytes")
    if args.keep_pdf:
        fn = url.rsplit("/", 1)[-1]
        with open(fn, "wb") as f:
            f.write(pdf_bytes)

    year_match = YEAR_RE.findall(url)
    source_year = year_match[-1] if year_match else ""

    rows = parse_remuneration(pdf_bytes)
    if not rows:
        sys.exit("No remuneration rows parsed -- the PDF layout may have changed.")

    conn = sqlite3.connect(args.db)
    init_db(conn)
    scraped_at = now()
    with conn:
        for r in rows:
            r["source_year"] = source_year
            r["source_url"] = url
            r["scraped_at"] = scraped_at
            conn.execute(
                """
                INSERT INTO employee_remuneration
                    (source_year, name_position, surname, remuneration, expenses,
                     is_faculty, page, source_url, scraped_at)
                VALUES (:source_year,:name_position,:surname,:remuneration,:expenses,
                        :is_faculty,:page,:source_url,:scraped_at)
                ON CONFLICT DO UPDATE SET expenses=excluded.expenses, scraped_at=excluded.scraped_at
                """,
                r,
            )

    vals = [r["remuneration"] for r in rows if r["remuneration"]]
    fac = [r["remuneration"] for r in rows if r["is_faculty"] and r["remuneration"]]
    vals.sort()
    print(f"\nParsed {len(rows)} employees (>{ '$75k FIA threshold' }) -> {args.db}")
    print(f"  median remuneration: ${vals[len(vals)//2]:,.0f}")
    print(f"  mean remuneration:   ${sum(vals)/len(vals):,.0f}")
    print(f"  max remuneration:    ${max(vals):,.0f}")
    if fac:
        fac.sort()
        print(f"  faculty (n={len(fac)}) median: ${fac[len(fac)//2]:,.0f}  mean: ${sum(fac)/len(fac):,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
