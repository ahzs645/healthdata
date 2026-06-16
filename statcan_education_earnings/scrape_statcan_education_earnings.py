#!/usr/bin/env python3
"""
StatCan Education Earnings Scraper
==================================
Pulls graduate employment-income-by-credential data from Statistics Canada and
stores it in SQLite, filtered to British Columbia. This is the public "wage
premium by education" input for the NHHR cost-benefit analysis (benefit line
B4 — Training & Education / trainee value: # trainees x delta income).

Primary table:
    37-10-0115  Characteristics and median employment income of longitudinal
                cohorts of postsecondary graduates, by credential / field /
                gender / age, 2 and 5 years after graduation.

The full table is downloaded fresh from the StatCan Web Data Service (WDS)
full-table CSV endpoint each run, then filtered to British Columbia, so the
values track whatever StatCan currently publishes.

Usage:
    python scrape_statcan_education_earnings.py
    python scrape_statcan_education_earnings.py --db education_earnings.db
    python scrape_statcan_education_earnings.py --geo "British Columbia" "Canada"
    python scrape_statcan_education_earnings.py --keep-zip   # cache the download
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen, urlretrieve

PRODUCT_ID = "37100115"
WDS_CSV = "https://www150.statcan.gc.ca/t1/wds/rest/getFullTableDownloadCSV/{pid}/en"

# Map the StatCan CSV columns we care about -> our tidy column names.
COLMAP = {
    "REF_DATE": "ref_date",
    "GEO": "geo",
    "Educational qualification": "credential",
    "Field of study": "field_of_study",
    "Gender": "gender",
    "Age group": "age_group",
    "Status of student in Canada": "student_status",
    "Characteristics after graduation": "grad_characteristic",
    "Graduate statistics": "graduate_statistic",
    "UOM": "uom",
    "VALUE": "value",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def download_csv_rows(keep_zip: bool):
    """Resolve the WDS download URL, fetch the zip, yield CSV dict rows."""
    meta = json.load(urlopen(WDS_CSV.format(pid=PRODUCT_ID), timeout=40))
    url = meta["object"]
    print(f"Downloading {url} ...")
    zip_path = f"{PRODUCT_ID}.zip"
    urlretrieve(url, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        name = [n for n in z.namelist() if n.endswith(f"{PRODUCT_ID}.csv")][0]
        with z.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig")
            for row in csv.DictReader(text):
                yield row
    if not keep_zip:
        import os
        os.remove(zip_path)


def init_db(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS education_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_date TEXT,
            geo TEXT,
            credential TEXT,
            field_of_study TEXT,
            gender TEXT,
            age_group TEXT,
            student_status TEXT,
            grad_characteristic TEXT,
            graduate_statistic TEXT,
            value REAL,
            uom TEXT,
            source_table TEXT,
            scraped_at TEXT,
            UNIQUE(ref_date, geo, credential, field_of_study, gender, age_group,
                   student_status, grad_characteristic, graduate_statistic)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ee_geo ON education_earnings(geo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ee_credential ON education_earnings(credential)")
    conn.commit()


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="Scrape StatCan education earnings (37-10-0115) into SQLite.")
    ap.add_argument("--db", default="education_earnings.db", help="Output SQLite DB (default: education_earnings.db)")
    ap.add_argument("--geo", nargs="+", default=["British Columbia"],
                    help="Geographies to keep (default: British Columbia). Pass 'Canada' too for a baseline.")
    ap.add_argument("--all-fields", action="store_true",
                    help="Keep every field of study (default keeps only 'Total, field of study').")
    ap.add_argument("--keep-zip", action="store_true", help="Keep the downloaded zip for reuse.")
    args = ap.parse_args()

    geos = set(args.geo)
    conn = sqlite3.connect(args.db)
    init_db(conn)

    kept = 0
    scraped_at = now()
    with conn:
        for row in download_csv_rows(args.keep_zip):
            if row.get("GEO") not in geos:
                continue
            if not args.all_fields and row.get("Field of study") != "Total, field of study":
                continue
            rec = {dst: row.get(src) for src, dst in COLMAP.items()}
            rec["value"] = to_float(rec["value"])
            rec["source_table"] = PRODUCT_ID
            rec["scraped_at"] = scraped_at
            conn.execute(
                """
                INSERT INTO education_earnings
                    (ref_date, geo, credential, field_of_study, gender, age_group,
                     student_status, grad_characteristic, graduate_statistic, value,
                     uom, source_table, scraped_at)
                VALUES (:ref_date,:geo,:credential,:field_of_study,:gender,:age_group,
                        :student_status,:grad_characteristic,:graduate_statistic,:value,
                        :uom,:source_table,:scraped_at)
                ON CONFLICT DO UPDATE SET value=excluded.value, scraped_at=excluded.scraped_at
                """,
                rec,
            )
            kept += 1

    print(f"Kept {kept} rows for geos {sorted(geos)} -> {args.db}")
    # Headline: median employment income 5 years after grad, by credential (latest cohort, BC, totals)
    cur = conn.execute(
        """
        SELECT credential, value FROM education_earnings
        WHERE geo=? AND graduate_statistic LIKE 'Median employment income five years%'
          AND gender='Total, gender' AND age_group='15 to 64 years'
          AND grad_characteristic='Graduates reporting employment income'
          AND field_of_study='Total, field of study'
        ORDER BY value DESC
        """,
        (args.geo[0],),
    )
    rows = [r for r in cur.fetchall() if r[1]]
    if rows:
        print(f"\nMedian employment income 5 yrs after graduation ({args.geo[0]}, by credential):")
        for cred, val in rows[:12]:
            print(f"  ${val:>10,.0f}  {cred}")
    conn.close()


if __name__ == "__main__":
    main()
