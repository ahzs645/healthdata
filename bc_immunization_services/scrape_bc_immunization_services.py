#!/usr/bin/env python3
"""
Mirror BC Data Catalogue immunization-service locations.

The source dataset is HealthLinkBC's "Immunization Services in BC" catalogue
record. It provides CSV/TXT downloads plus a BCGW map service, so this loader uses
the catalogue API to discover the current CSV resource, stores the rows in
SQLite, and writes lightweight map exports for PGMaps.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

PACKAGE_ID = "f49ebe46-6faf-4b5d-a21e-c19db5ec69d7"
PACKAGE_API_URL = f"https://catalogue.data.gov.bc.ca/api/3/action/package_show?id={PACKAGE_ID}"
DATASET_PAGE_URL = "https://catalogue.data.gov.bc.ca/dataset/immunization-services-in-bc"
SOURCE_LABEL = "HealthLinkBC Immunization Services in BC"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "bc_immunization_services.db"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"

SOURCE_FIELDS = [
    "SV_TAXONOMY",
    "TAXONOMY_NAME",
    "RG_REFERENCE",
    "RG_NAME",
    "SV_REFERENCE",
    "SV_NAME",
    "SV_DESCRIPTION",
    "SL_REFERENCE",
    "LC_REFERENCE",
    "PHONE_NUMBER",
    "WEBSITE",
    "EMAIL_ADDRESS",
    "WHEELCHAIR_ACCESSIBLE",
    "LANGUAGE",
    "HOURS",
    "STREET_NUMBER",
    "STREET_NAME",
    "STREET_TYPE",
    "STREET_DIRECTION",
    "CITY",
    "PROVINCE",
    "POSTAL_CODE",
    "LATITUDE",
    "LONGITUDE",
    "811_LINK",
]

EXPORT_FIELDS = [
    "source_row_number",
    "service_listing_id",
    "location_id",
    "organization_id",
    "organization_name",
    "service_id",
    "service_name",
    "taxonomy_code",
    "taxonomy_name",
    "description",
    "phone",
    "website",
    "email",
    "wheelchair_accessible",
    "language",
    "hours",
    "address",
    "city",
    "province",
    "postal_code",
    "latitude",
    "longitude",
    "healthlinkbc_url",
    "source_label",
    "source_url",
]


def clean(value: str | None) -> str:
    return (value or "").strip()


def now() -> str:
    return datetime.now(UTC).isoformat()


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "PGMaps healthdata immunization-services loader"})
    with urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "PGMaps healthdata immunization-services loader",
            "Accept": "text/csv,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8-sig")


def load_package_metadata(package_api_url: str) -> dict:
    payload = fetch_json(package_api_url)
    if not payload.get("success"):
        raise ValueError(f"BC Data Catalogue package_show failed for {package_api_url}")
    return payload["result"]


def csv_resource(package: dict) -> dict:
    resources = package.get("resources") or []
    for resource in resources:
        if clean(resource.get("format")).casefold() == "csv" and clean(resource.get("url")):
            return resource
    raise ValueError("No CSV resource with a download URL found in catalogue metadata")


def read_source_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    missing = [field for field in SOURCE_FIELDS if field not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"Source CSV is missing fields: {', '.join(missing)}")
    rows = []
    for source_row_number, row in enumerate(reader, start=1):
        item = {field: clean(row.get(field)) for field in SOURCE_FIELDS}
        item["__source_row_number"] = str(source_row_number)
        rows.append(item)
    return rows


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def address_for(row: dict[str, str]) -> str:
    parts = [row["STREET_NUMBER"], row["STREET_NAME"], row["STREET_TYPE"], row["STREET_DIRECTION"]]
    return " ".join(part for part in parts if part)


def normalized_row(row: dict[str, str], source_url: str) -> dict[str, str]:
    return {
        "source_row_number": row["__source_row_number"],
        "service_listing_id": row["SL_REFERENCE"],
        "location_id": row["LC_REFERENCE"],
        "organization_id": row["RG_REFERENCE"],
        "organization_name": row["RG_NAME"],
        "service_id": row["SV_REFERENCE"],
        "service_name": row["SV_NAME"],
        "taxonomy_code": row["SV_TAXONOMY"],
        "taxonomy_name": row["TAXONOMY_NAME"],
        "description": row["SV_DESCRIPTION"],
        "phone": row["PHONE_NUMBER"],
        "website": row["WEBSITE"],
        "email": row["EMAIL_ADDRESS"],
        "wheelchair_accessible": row["WHEELCHAIR_ACCESSIBLE"],
        "language": row["LANGUAGE"],
        "hours": row["HOURS"],
        "address": address_for(row),
        "city": row["CITY"],
        "province": row["PROVINCE"].upper(),
        "postal_code": row["POSTAL_CODE"].upper(),
        "latitude": row["LATITUDE"],
        "longitude": row["LONGITUDE"],
        "healthlinkbc_url": row["811_LINK"],
        "source_label": SOURCE_LABEL,
        "source_url": source_url,
    }


def normalized_rows(rows: list[dict[str, str]], source_url: str) -> list[dict[str, str]]:
    return [normalized_row(row, source_url) for row in rows]


def write_database(path: Path, rows: list[dict[str, str]], package: dict, resource: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_at = now()
    source_url = clean(resource.get("url"))
    items = normalized_rows(rows, source_url)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS immunization_services")
        conn.execute("DROP TABLE IF EXISTS dataset_metadata")
        conn.execute(
            """
            CREATE TABLE immunization_services (
                source_row_number INTEGER PRIMARY KEY,
                service_listing_id TEXT,
                location_id TEXT,
                organization_id TEXT,
                organization_name TEXT,
                service_id TEXT,
                service_name TEXT,
                taxonomy_code TEXT,
                taxonomy_name TEXT,
                description TEXT,
                phone TEXT,
                website TEXT,
                email TEXT,
                wheelchair_accessible TEXT,
                language TEXT,
                hours TEXT,
                address TEXT,
                city TEXT,
                province TEXT,
                postal_code TEXT,
                latitude REAL,
                longitude REAL,
                healthlinkbc_url TEXT,
                source_label TEXT,
                source_url TEXT,
                downloaded_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO immunization_services (
                source_row_number,
                service_listing_id,
                location_id,
                organization_id,
                organization_name,
                service_id,
                service_name,
                taxonomy_code,
                taxonomy_name,
                description,
                phone,
                website,
                email,
                wheelchair_accessible,
                language,
                hours,
                address,
                city,
                province,
                postal_code,
                latitude,
                longitude,
                healthlinkbc_url,
                source_label,
                source_url,
                downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(item["source_row_number"]),
                    item["service_listing_id"],
                    item["location_id"],
                    item["organization_id"],
                    item["organization_name"],
                    item["service_id"],
                    item["service_name"],
                    item["taxonomy_code"],
                    item["taxonomy_name"],
                    item["description"],
                    item["phone"],
                    item["website"],
                    item["email"],
                    item["wheelchair_accessible"],
                    item["language"],
                    item["hours"],
                    item["address"],
                    item["city"],
                    item["province"],
                    item["postal_code"],
                    parse_float(item["latitude"]),
                    parse_float(item["longitude"]),
                    item["healthlinkbc_url"],
                    item["source_label"],
                    item["source_url"],
                    downloaded_at,
                )
                for item in items
            ],
        )
        conn.executescript(
            """
            CREATE INDEX idx_immunization_services_location ON immunization_services (location_id);
            CREATE INDEX idx_immunization_services_city ON immunization_services (city);
            CREATE INDEX idx_immunization_services_taxonomy ON immunization_services (taxonomy_code);
            CREATE INDEX idx_immunization_services_org ON immunization_services (organization_name);

            CREATE TABLE dataset_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        metadata = {
            "dataset_title": clean(package.get("title")),
            "dataset_page_url": DATASET_PAGE_URL,
            "package_api_url": PACKAGE_API_URL,
            "package_id": PACKAGE_ID,
            "license_title": clean(package.get("license_title")),
            "license_url": clean(package.get("license_url")),
            "organization": clean((package.get("organization") or {}).get("title")),
            "record_last_modified": clean(package.get("record_last_modified")),
            "metadata_modified": clean(package.get("metadata_modified")),
            "resource_id": clean(resource.get("id")),
            "resource_url": source_url,
            "resource_last_modified": clean(resource.get("last_modified")),
            "resource_update_cycle": clean(resource.get("resource_update_cycle")),
            "data_quality": clean(package.get("data_quality")),
            "downloaded_at": downloaded_at,
        }
        conn.executemany("INSERT INTO dataset_metadata (key, value) VALUES (?, ?)", sorted(metadata.items()))


def write_csv(path: Path, rows: list[dict[str, str]], source_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for item in normalized_rows(rows, source_url):
            writer.writerow(item)


def write_geojson(path: Path, rows: list[dict[str, str]], source_url: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    features = []
    for row in rows:
        latitude = parse_float(row["LATITUDE"])
        longitude = parse_float(row["LONGITUDE"])
        if latitude is None or longitude is None:
            continue
        item = normalized_row(row, source_url)
        feature_id = (
            "bc-immunization-services:"
            f"{item['source_row_number']}:{item['service_listing_id']}:{item['location_id']}:{item['service_id']}"
        )
        features.append(
            {
                "type": "Feature",
                "id": feature_id,
                "geometry": {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                },
                "properties": item,
            }
        )
    features.sort(
        key=lambda feature: (
            feature["properties"]["city"].casefold(),
            feature["properties"]["organization_name"].casefold(),
            feature["properties"]["service_name"].casefold(),
        )
    )
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")
    return len(features)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror HealthLinkBC immunization services into SQLite and GeoJSON.")
    parser.add_argument("--package-api-url", default=PACKAGE_API_URL)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    package = load_package_metadata(args.package_api_url)
    resource = csv_resource(package)
    source_url = clean(resource.get("url"))
    rows = read_source_csv(fetch_text(source_url))

    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    write_database(db_path, rows, package, resource)
    write_csv(output_dir / "bc-immunization-services.csv", rows, source_url)
    feature_count = write_geojson(output_dir / "bc-immunization-services.geojson", rows, source_url)

    print(f"Rows loaded: {len(rows)}")
    print(f"GeoJSON features: {feature_count}")
    print(f"Database: {db_path}")
    print(f"Output: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
