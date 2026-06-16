#!/usr/bin/env python3
"""
BC Utility Tariffs Scraper
==========================
Collects the authoritative utility-rate documents that feed the NHHR cost-benefit
analysis operating-cost lines (electricity, natural gas, water, sewer) and pulls
out candidate rate lines, plus a curated set of confirmed seed values.

These tariffs live in big PDFs (BC Hydro Electric Tariff) and JS-rendered pages
(FortisBC), so exact per-rate parsing is brittle. This scraper therefore:
  1. downloads each source document into ./docs (re-runnable),
  2. extracts every line/cell that looks like a rate (cents/kWh, $/GJ, $/m3,
     per day/quarter) into `extracted_rate_lines` for searching,
  3. stores a small curated `seed_rates` table of values confirmed by hand.

A research facility bills on commercial (Medium/Large General Service) electricity
and commercial gas rates -- NOT residential. Confirm the exact commercial energy
+ demand charges in the downloaded BC Hydro Electric Tariff PDF.

Usage:
    python scrape_bc_utility_tariffs.py
    python scrape_bc_utility_tariffs.py --db bc_utility_tariffs.db
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}

SOURCES = [
    ("BC Hydro", "Electric Tariff (all rate schedules)", "pdf",
     "https://www.bchydro.com/content/dam/BCHydro/customer-portal/documents/corporate/tariff-filings/electric-tariff/bchydro-electric-tariff.pdf"),
    ("FortisBC", "Natural gas business rates", "html",
     "https://www.fortisbc.com/accounts/billing-rates/natural-gas-rates/business-rates"),
    ("City of Prince George", "Water Regulation and Rates Bylaw 7479", "pdf",
     "https://www.princegeorge.ca/sites/default/files/2025-06/BL7479_CONSOLIDATED_2025-05-26.pdf"),
]

# Confirmed by hand (web search / official pages), 2026-06-15. Refresh if rates change.
SEED_RATES = [
    ("BC Hydro", "Residential Step 1 (Tier 1) energy charge", "~11.87", "cents/kWh", "fiscal 2026 (verify in tariff)"),
    ("BC Hydro", "Residential Step 2 (Tier 2) energy charge", "14.08", "cents/kWh", "effective 2026-04-01"),
    ("BC Hydro", "Residential net rate change", "+3.75", "percent", "effective 2026-04-01"),
    ("FortisBC", "Natural gas commodity (CCRC)", "2.230", "$/GJ", "effective 2026-01-01"),
    ("FortisBC", "Residential bill change", "+11.1", "percent", "effective 2026-01-01"),
]

RATE_RE = re.compile(
    r"(¢|cents?\b|\$)\s?\d|\bper\s+(kwh|gj|m3|cubic|kilolitre|kl|day|quarter|year|month)\b",
    re.I,
)
HAS_NUM = re.compile(r"\d")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            utility TEXT, title TEXT, kind TEXT, url TEXT,
            local_path TEXT, bytes INTEGER, fetched_at TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS seed_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            utility TEXT, rate_name TEXT, value TEXT, unit TEXT, effective TEXT, scraped_at TEXT
        );
        CREATE TABLE IF NOT EXISTS extracted_rate_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            utility TEXT, source_url TEXT, page INTEGER, line TEXT, scraped_at TEXT
        );
        """
    )
    conn.commit()


def extract_pdf_lines(data: bytes):
    if not pdfplumber:
        return
    with pdfplumber.open(io.BytesIO(data)) as doc:
        for pageno, page in enumerate(doc.pages, 1):
            for ln in (page.extract_text() or "").split("\n"):
                s = ln.strip()
                if s and HAS_NUM.search(s) and RATE_RE.search(s) and len(s) < 160:
                    yield pageno, s


def extract_html_lines(data: bytes):
    text = BeautifulSoup(data, "html.parser").get_text("\n", strip=True)
    for s in text.split("\n"):
        s = s.strip()
        if s and HAS_NUM.search(s) and RATE_RE.search(s) and len(s) < 160:
            yield None, s


def main():
    ap = argparse.ArgumentParser(description="Collect BC utility tariff docs + rate lines into SQLite.")
    ap.add_argument("--db", default="bc_utility_tariffs.db")
    ap.add_argument("--docs-dir", default="docs")
    args = ap.parse_args()
    os.makedirs(args.docs_dir, exist_ok=True)

    conn = sqlite3.connect(args.db)
    init_db(conn)
    scraped_at = now()

    with conn:
        conn.execute("DELETE FROM seed_rates")
        for utility, name, value, unit, eff in SEED_RATES:
            conn.execute(
                "INSERT INTO seed_rates (utility,rate_name,value,unit,effective,scraped_at) VALUES (?,?,?,?,?,?)",
                (utility, name, value, unit, eff, scraped_at),
            )

        for utility, title, kind, url in SOURCES:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=90)
                resp.raise_for_status()
                data = resp.content
                fn = os.path.join(args.docs_dir, url.rsplit("/", 1)[-1] or f"{utility}.dat")
                if kind == "html":
                    fn = os.path.join(args.docs_dir, f"{utility.replace(' ','_').lower()}.html")
                with open(fn, "wb") as f:
                    f.write(data)
                status = "ok"
                n = 0
                lines = extract_pdf_lines(data) if kind == "pdf" else extract_html_lines(data)
                for pageno, line in lines:
                    conn.execute(
                        "INSERT INTO extracted_rate_lines (utility,source_url,page,line,scraped_at) VALUES (?,?,?,?,?)",
                        (utility, url, pageno, line, scraped_at),
                    )
                    n += 1
                print(f"{utility:22s} {status}  {len(data):>9,} bytes  {n} rate lines")
            except Exception as exc:
                status = f"error: {exc}"
                data = b""
                fn = ""
                print(f"{utility:22s} FAILED: {exc}")
            conn.execute(
                "INSERT INTO documents (utility,title,kind,url,local_path,bytes,fetched_at,status) VALUES (?,?,?,?,?,?,?,?)",
                (utility, title, kind, url, fn, len(data), scraped_at, status),
            )

    print(f"\nSeed rates: {conn.execute('SELECT COUNT(*) FROM seed_rates').fetchone()[0]}")
    print(f"Extracted rate lines: {conn.execute('SELECT COUNT(*) FROM extracted_rate_lines').fetchone()[0]}")
    print(f"Documents saved under: {args.docs_dir}/")
    conn.close()


if __name__ == "__main__":
    main()
