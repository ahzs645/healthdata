#!/usr/bin/env python3
"""
BC Patient Travel Scraper
=========================
Builds the public patient-travel evidence for the NHHR cost-benefit analysis
benefit line B3 (Avoided Patient Travel Costs):

  1. nh_connections_schedule  -- scraped Northern Health Connections medical-travel
     bus routes (origin, stops, times) from nhconnections.ca. Shows the actual
     north->south travel network and stop geography.
  2. travel_cost_benchmarks   -- curated unit costs confirmed by hand (CRA per-km
     allowance, the subsidized NH Connections fare, and the derived economic
     driving cost), since these are not published as scrapable tables.

NH Connections does NOT publish a fare table on its website (fares are quoted at
booking), so the fare figure is seeded from public reporting and flagged.

Usage:
    python scrape_bc_patient_travel.py
    python scrape_bc_patient_travel.py --db bc_patient_travel.db
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://nhconnections.ca"
INDEX = f"{BASE}/schedules-and-fares"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}

# Curated unit costs (confirmed 2026-06-15). Refresh when CRA rates / fares change.
TRAVEL_BENCHMARKS = [
    ("CRA automobile allowance - first 5,000 km", "72", "cents/km", "2025 tax year",
     "Canada Revenue Agency",
     "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/benefits-allowances/automobile/automobile-motor-vehicle-allowances/automobile-allowance-rates.html",
     "Economic mileage basis. 2024 = 70 c/km."),
    ("CRA automobile allowance - after 5,000 km", "66", "cents/km", "2025 tax year",
     "Canada Revenue Agency",
     "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/benefits-allowances/automobile/automobile-motor-vehicle-allowances/automobile-allowance-rates.html",
     "2024 = 64 c/km."),
    ("CRA automobile allowance - territories uplift", "+4", "cents/km", "2025 tax year",
     "Canada Revenue Agency",
     "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/benefits-allowances/automobile/automobile-motor-vehicle-allowances/automobile-allowance-rates.html",
     "YT/NWT/NU add 4 c/km."),
    ("NH Connections fare Prince George <-> Vancouver", "20", "CAD each way", "2026",
     "Northern Health Connections (public reporting)",
     "https://nhconnections.ca/schedules-and-fares/prince-george-vancouver-prince-george",
     "SUBSIDIZED patient out-of-pocket fare; not published on site, sourced from news. NOT the economic cost."),
    ("Economic driving cost Prince George <-> Vancouver", "566", "CAD one way", "2025 CRA rate",
     "Derived (CRA 72 c/km x ~786 km)", "",
     "Upper-bound economic cost vs the $20 subsidized fare; brackets the avoided-travel value. ~1,132 round trip."),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(attempt)


def route_slugs(index_html):
    slugs = re.findall(r"/schedules-and-fares/([a-z0-9\-]+)", index_html)
    return sorted(set(slugs))


def parse_schedule(html, slug):
    """Yield (direction_header, seq, stop_text, time) rows from a route page."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        header_cells = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        direction = header_cells[0] if header_cells else ""
        seq = 0
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 2 or not cells[0]:
                continue
            seq += 1
            yield direction, seq, cells[0], cells[1]


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nh_connections_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_slug TEXT, direction TEXT, seq INTEGER,
            stop_event TEXT, stop_time TEXT, source_url TEXT, scraped_at TEXT
        );
        CREATE TABLE IF NOT EXISTS travel_cost_benchmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric TEXT, value TEXT, unit TEXT, period TEXT,
            source TEXT, source_url TEXT, note TEXT, scraped_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sched_route ON nh_connections_schedule(route_slug);
        """
    )
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Scrape NH Connections schedules + travel cost benchmarks into SQLite.")
    ap.add_argument("--db", default="bc_patient_travel.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    init_db(conn)
    ts = now()

    with conn:
        conn.execute("DELETE FROM travel_cost_benchmarks")
        for row in TRAVEL_BENCHMARKS:
            conn.execute(
                "INSERT INTO travel_cost_benchmarks (metric,value,unit,period,source,source_url,note,scraped_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (*row, ts))

        conn.execute("DELETE FROM nh_connections_schedule")
        slugs = route_slugs(fetch(INDEX))
        print(f"Found {len(slugs)} NH Connections routes")
        total = 0
        for slug in slugs:
            url = f"{BASE}/schedules-and-fares/{slug}"
            try:
                n = 0
                for direction, seq, stop, t in parse_schedule(fetch(url), slug):
                    conn.execute(
                        "INSERT INTO nh_connections_schedule (route_slug,direction,seq,stop_event,stop_time,source_url,scraped_at) "
                        "VALUES (?,?,?,?,?,?,?)", (slug, direction, seq, stop, t, url, ts))
                    n += 1
                total += n
                print(f"  {slug:42s} {n} stops")
            except Exception as exc:
                print(f"  {slug:42s} FAILED: {exc}")
            time.sleep(0.4)

    print(f"\nRoutes scraped: {total} schedule rows; {len(TRAVEL_BENCHMARKS)} cost benchmarks -> {args.db}")
    conn.close()


if __name__ == "__main__":
    main()
