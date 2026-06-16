#!/usr/bin/env python3
"""
StatCan CBA Macro Tables Scraper
================================
Pulls the Statistics Canada public tables that feed the NHHR cost-benefit
analysis macro inputs, filtered to British Columbia / Northern BC, into a single
SQLite database (one table per dataset). Re-run any time to refresh; every table
is downloaded fresh from the StatCan Web Data Service (WDS) full-table CSV
endpoint, so values track whatever StatCan currently publishes.

  14-10-0064  Employee wages by industry, annual       -> bc_wages        (B2 value-of-time, B19-21 wages)
  14-10-0060  Retirement age by class of worker        -> retirement_age  (B13 extended workforce)
  14-10-0387  Labour force characteristics by region   -> northern_bc_lfs (B12 employed population)
  17-10-0022  Interprovincial migrants by province     -> bc_migration    (B5 out-migration proxy)

Usage:
    python scrape_statcan_cba.py
    python scrape_statcan_cba.py --db statcan_cba.db --keep-zip
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen, urlretrieve

WDS = "https://www150.statcan.gc.ca/t1/wds/rest/getFullTableDownloadCSV/{pid}/en"
NORTHERN_BC = [
    "Cariboo, British Columbia",
    "North Coast and Nechako, British Columbia",
    "Northeast, British Columbia",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_table(pid: str, keep_zip: bool):
    """Download a StatCan full-table CSV and return list-of-dict rows."""
    url = json.load(urlopen(WDS.format(pid=pid), timeout=40))["object"]
    print(f"  [{pid}] {url}")
    zpath = f"{pid}.zip"
    urlretrieve(url, zpath)
    rows = []
    with zipfile.ZipFile(zpath) as z:
        name = [n for n in z.namelist() if n.endswith(f"{pid}.csv")][0]
        with z.open(name) as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
    if not keep_zip:
        os.remove(zpath)
    return rows


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bc_wages (
            ref_date TEXT, geo TEXT, wages TEXT, naics TEXT, value REAL, uom TEXT, scraped_at TEXT,
            UNIQUE(ref_date, geo, wages, naics));
        CREATE TABLE IF NOT EXISTS retirement_age (
            ref_date TEXT, geo TEXT, retirement_age TEXT, class_of_worker TEXT, value REAL, scraped_at TEXT,
            UNIQUE(ref_date, geo, retirement_age, class_of_worker));
        CREATE TABLE IF NOT EXISTS northern_bc_lfs (
            year TEXT, geo TEXT, characteristic TEXT, value REAL, uom TEXT, scraped_at TEXT,
            UNIQUE(year, geo, characteristic));
        CREATE TABLE IF NOT EXISTS bc_migration (
            ref_date TEXT, out_migrants_from_bc REAL, in_migrants_to_bc REAL, net_migration REAL, scraped_at TEXT,
            UNIQUE(ref_date));
        """
    )
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Pull StatCan CBA macro tables into SQLite (BC / Northern BC).")
    ap.add_argument("--db", default="statcan_cba.db")
    ap.add_argument("--keep-zip", action="store_true", help="Keep downloaded zips for reuse.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    init_db(conn)
    ts = now()

    with conn:
        # 14-10-0064 BC wages
        print("14-10-0064 Employee wages (BC)")
        for r in load_table("14100064", args.keep_zip):
            if (r["GEO"] == "British Columbia"
                    and r["Type of work"] == "Both full- and part-time employees"
                    and r["Gender"] == "Total - Gender"
                    and r["Age group"] == "15 years and over"
                    and r["Wages"] in ("Average hourly wage rate", "Average weekly wage rate")):
                conn.execute(
                    "INSERT INTO bc_wages VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET value=excluded.value, scraped_at=excluded.scraped_at",
                    (r["REF_DATE"], r["GEO"], r["Wages"],
                     r["North American Industry Classification System (NAICS)"], num(r["VALUE"]), r["UOM"], ts))

        # 14-10-0060 retirement age
        print("14-10-0060 Retirement age")
        for r in load_table("14100060", args.keep_zip):
            if r["Gender"] == "Total - Gender":
                conn.execute(
                    "INSERT INTO retirement_age VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET value=excluded.value, scraped_at=excluded.scraped_at",
                    (r["REF_DATE"], r["GEO"], r["Retirement age"], r["Class of worker"], num(r["VALUE"]), ts))

        # 14-10-0387 Northern BC labour force (annual avg of monthly estimates)
        print("14-10-0387 Northern BC labour force")
        agg = {}
        for r in load_table("14100387", args.keep_zip):
            if r["GEO"] in NORTHERN_BC and r["Statistics"] == "Estimate":
                key = (r["REF_DATE"][:4], r["GEO"], r["Labour force characteristics"])
                v = num(r["VALUE"])
                if v is not None:
                    agg.setdefault(key, [0.0, 0, r["UOM"]])
                    agg[key][0] += v
                    agg[key][1] += 1
        for (year, geo, char), (tot, cnt, uom) in agg.items():
            conn.execute(
                "INSERT INTO northern_bc_lfs VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET value=excluded.value, scraped_at=excluded.scraped_at",
                (year, geo, char, round(tot / cnt, 1), uom, ts))

        # 17-10-0022 BC interprovincial migration
        print("17-10-0022 BC interprovincial migration")
        rows = load_table("17100022", args.keep_zip)
        years = sorted({r["REF_DATE"] for r in rows})
        for yr in years:
            out = sum(num(r["VALUE"]) or 0 for r in rows
                      if r["REF_DATE"] == yr and r["GEO"] == "British Columbia, province of origin")
            inc = sum(num(r["VALUE"]) or 0 for r in rows
                      if r["REF_DATE"] == yr and r["Geography, province of destination"] == "British Columbia, province of destination")
            conn.execute(
                "INSERT INTO bc_migration VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET out_migrants_from_bc=excluded.out_migrants_from_bc, in_migrants_to_bc=excluded.in_migrants_to_bc, net_migration=excluded.net_migration, scraped_at=excluded.scraped_at",
                (yr, out, inc, inc - out, ts))

    # headline
    def one(sql, *a):
        c = conn.execute(sql, a).fetchone()
        return c[0] if c else None
    latest_wage_yr = one("SELECT MAX(ref_date) FROM bc_wages")
    print(f"\n{args.db} built.")
    print(f"  BC avg hourly wage (all industries, {latest_wage_yr}): "
          f"${one('SELECT value FROM bc_wages WHERE ref_date=? AND wages=? AND naics=?', latest_wage_yr, 'Average hourly wage rate', 'Total employees, all industries')}")
    print(f"  Avg retirement age (all retirees, latest): "
          f"{one('SELECT value FROM retirement_age WHERE retirement_age=? AND class_of_worker=? ORDER BY ref_date DESC LIMIT 1','Average age','Total, all retirees')}")
    yr = one("SELECT MAX(year) FROM northern_bc_lfs")
    print(f"  Northern BC employed ({yr}): "
          f"{one('SELECT ROUND(SUM(value),1) FROM northern_bc_lfs WHERE year=? AND characteristic=?', yr, 'Employment')}k")
    print(f"  BC out-migration (latest): "
          f"{one('SELECT out_migrants_from_bc FROM bc_migration ORDER BY ref_date DESC LIMIT 1')}")
    conn.close()


if __name__ == "__main__":
    main()
