#!/usr/bin/env python3
"""
Load Statistics Canada's Open Database of Healthcare Facilities (ODHF).

The script downloads the official ODHF zip, stores the normalized rows in SQLite,
and writes BC-focused CSV and GeoJSON exports for the shared health place registry.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ODHF_URL = "https://www150.statcan.gc.ca/n1/pub/13-26-0001/2020001/ODHF_v1.1.zip"
SOURCE_PAGE_URL = "https://www.statcan.gc.ca/en/lode/databases/odhf"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "statcan_odhf.db"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"

FIELDS = [
    "index",
    "facility_name",
    "source_facility_type",
    "odhf_facility_type",
    "provider",
    "unit",
    "street_no",
    "street_name",
    "postal_code",
    "city",
    "province",
    "source_format_str_address",
    "CSDname",
    "CSDuid",
    "Pruid",
    "latitude",
    "longitude",
]


def clean(value: str | None) -> str:
    return (value or "").strip()


def download_zip(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "PGMaps healthdata StatCan ODhf loader",
            "Accept": "application/zip,application/octet-stream,*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        return response.read()


def read_odhf_csv(zip_bytes: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected exactly one ODHF CSV in zip, found {csv_names}")
        with archive.open(csv_names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="cp1252", newline="")
            reader = csv.DictReader(text)
            missing = [field for field in FIELDS if field not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"ODHF CSV is missing fields: {', '.join(missing)}")
            return [{field: clean(row.get(field)) for field in FIELDS} for row in reader]


def write_database(path: Path, rows: list[dict[str, str]], source_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS odhf_facilities")
        conn.execute(
            """
            CREATE TABLE odhf_facilities (
                odhf_index TEXT PRIMARY KEY,
                facility_name TEXT NOT NULL,
                source_facility_type TEXT,
                odhf_facility_type TEXT,
                provider TEXT,
                unit TEXT,
                street_no TEXT,
                street_name TEXT,
                postal_code TEXT,
                city TEXT,
                province TEXT,
                source_format_str_address TEXT,
                csd_name TEXT,
                csd_uid TEXT,
                pr_uid TEXT,
                latitude REAL,
                longitude REAL,
                source_url TEXT NOT NULL,
                downloaded_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO odhf_facilities (
                odhf_index,
                facility_name,
                source_facility_type,
                odhf_facility_type,
                provider,
                unit,
                street_no,
                street_name,
                postal_code,
                city,
                province,
                source_format_str_address,
                csd_name,
                csd_uid,
                pr_uid,
                latitude,
                longitude,
                source_url,
                downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["index"],
                    row["facility_name"],
                    row["source_facility_type"],
                    row["odhf_facility_type"],
                    row["provider"],
                    row["unit"],
                    row["street_no"],
                    row["street_name"],
                    row["postal_code"].upper(),
                    row["city"],
                    row["province"].upper(),
                    row["source_format_str_address"],
                    row["CSDname"],
                    row["CSDuid"],
                    row["Pruid"],
                    parse_float(row["latitude"]),
                    parse_float(row["longitude"]),
                    source_url,
                    datetime.now(UTC).isoformat(),
                )
                for row in rows
            ],
        )
        conn.execute("CREATE INDEX idx_odhf_facilities_province ON odhf_facilities (province)")
        conn.execute("CREATE INDEX idx_odhf_facilities_type ON odhf_facilities (odhf_facility_type)")
        conn.execute("CREATE INDEX idx_odhf_facilities_name ON odhf_facilities (facility_name)")


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def address_for(row: dict[str, str]) -> str:
    source_address = clean(row["source_format_str_address"])
    if source_address:
        return source_address
    parts = [clean(row["unit"]), clean(row["street_no"]), clean(row["street_name"])]
    return " ".join(part for part in parts if part)


def bc_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if clean(row["province"]).casefold() == "bc"]


def write_bc_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output_fields = [
        "odhf_index",
        "facility_name",
        "source_facility_type",
        "odhf_facility_type",
        "provider",
        "address",
        "postal_code",
        "city",
        "province",
        "csd_name",
        "csd_uid",
        "latitude",
        "longitude",
        "source_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "odhf_index": row["index"],
                    "facility_name": row["facility_name"],
                    "source_facility_type": row["source_facility_type"],
                    "odhf_facility_type": row["odhf_facility_type"],
                    "provider": row["provider"],
                    "address": address_for(row),
                    "postal_code": row["postal_code"].upper(),
                    "city": row["city"],
                    "province": row["province"].upper(),
                    "csd_name": row["CSDname"],
                    "csd_uid": row["CSDuid"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "source_url": SOURCE_PAGE_URL,
                }
            )


def write_bc_geojson(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    features = []
    for row in rows:
        latitude = parse_float(row["latitude"])
        longitude = parse_float(row["longitude"])
        if latitude is None or longitude is None:
            continue
        features.append(
            {
                "type": "Feature",
                "id": f"statcan-odhf:{row['index']}",
                "geometry": {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                },
                "properties": {
                    "odhf_index": row["index"],
                    "facility_name": row["facility_name"],
                    "source_facility_type": row["source_facility_type"] or None,
                    "odhf_facility_type": row["odhf_facility_type"] or None,
                    "provider": row["provider"] or None,
                    "address": address_for(row) or None,
                    "postal_code": row["postal_code"].upper() or None,
                    "city": row["city"] or None,
                    "province": row["province"].upper(),
                    "csd_name": row["CSDname"] or None,
                    "csd_uid": row["CSDuid"] or None,
                    "source_url": SOURCE_PAGE_URL,
                },
            }
        )
    features.sort(key=lambda feature: (feature["properties"]["facility_name"].casefold(), feature["properties"]["odhf_index"]))
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "type": "FeatureCollection",
                "name": "statcan_odhf_bc",
                "metadata": {
                    "source": "Statistics Canada Open Database of Healthcare Facilities v1.1",
                    "source_url": SOURCE_PAGE_URL,
                    "download_url": ODHF_URL,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "bc_rows": len(rows),
                    "mapped_bc_rows": len(features),
                },
                "features": features,
            },
            handle,
            indent=2,
        )
        handle.write("\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download and load the StatCan ODHF dataset.")
    parser.add_argument("--url", default=ODHF_URL, help="ODHF zip URL")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for CSV/GeoJSON exports")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    zip_bytes = download_zip(args.url)
    rows = read_odhf_csv(zip_bytes)
    bc = bc_rows(rows)

    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    write_database(db_path, rows, args.url)
    write_bc_csv(output_dir / "statcan-odhf-bc.csv", bc)
    write_bc_geojson(output_dir / "statcan-odhf-bc.geojson", bc)

    print(f"Wrote {len(rows)} ODHF rows ({len(bc)} BC) to {db_path}")
    print(f"Wrote BC exports to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
