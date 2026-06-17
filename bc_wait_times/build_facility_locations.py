#!/usr/bin/env python3
"""
Maintain BC surgery wait-time facility locations outside the scraped database.

The surgery wait-times source exposes facility names, not street addresses or
coordinates. This helper keeps a reviewed facility_locations.csv in sync with
the DB facility names, optionally geocodes reviewed addresses with the BC
Address Geocoder, and exports map-ready joined files without mutating the
source SQLite database.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

BC_GEOCODER_URL = "https://geocoder.api.gov.bc.ca/addresses.json"

LOCATION_FIELDS = [
    "facility_name",
    "health_authority",
    "address",
    "locality",
    "province",
    "postal_code",
    "latitude",
    "longitude",
    "source_label",
    "source_url",
    "verification_status",
    "notes",
]

log = logging.getLogger("bc_wait_times_locations")


@dataclass
class FacilityLocation:
    facility_name: str
    health_authority: str
    address: str = ""
    locality: str = ""
    province: str = ""
    postal_code: str = ""
    latitude: str = ""
    longitude: str = ""
    source_label: str = ""
    source_url: str = ""
    verification_status: str = "needs_review"
    notes: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return normalize_key(self.facility_name), normalize_key(self.health_authority)

    @property
    def geocode_query(self) -> Optional[str]:
        if not self.address.strip():
            return None
        parts = [self.address, self.locality, self.province or "BC", self.postal_code]
        query = ", ".join(part.strip() for part in parts if part and part.strip())
        return query or None

    def has_coordinates(self) -> bool:
        return bool(parse_float(self.latitude) is not None and parse_float(self.longitude) is not None)

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in LOCATION_FIELDS}


def normalize_key(value: str) -> str:
    return " ".join((value or "").casefold().replace("&", "and").split())


def parse_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_locations(path: Path) -> list[FacilityLocation]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in LOCATION_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        return [
            FacilityLocation(**{field: (row.get(field) or "").strip() for field in LOCATION_FIELDS})
            for row in reader
        ]


def write_locations(path: Path, locations: list[FacilityLocation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    locations = sorted(locations, key=lambda item: (item.facility_name.casefold(), item.health_authority.casefold()))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOCATION_FIELDS)
        writer.writeheader()
        for location in locations:
            writer.writerow(location.to_row())


def db_facilities(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT facility_name, health_authority
        FROM (
            SELECT facility_name, health_authority FROM specialist_wait_times
            UNION
            SELECT facility_name, health_authority FROM facility_wait_times
        )
        WHERE facility_name IS NOT NULL AND facility_name <> ''
        ORDER BY facility_name, health_authority
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def sync_location_inventory(conn: sqlite3.Connection, csv_path: Path) -> list[FacilityLocation]:
    existing = {location.key: location for location in read_locations(csv_path)}
    added = 0
    for facility_name, health_authority in db_facilities(conn):
        key = normalize_key(facility_name), normalize_key(health_authority)
        if key not in existing:
            existing[key] = FacilityLocation(
                facility_name=facility_name,
                health_authority=health_authority,
                notes="Added from wait-times database; needs authoritative address review.",
            )
            added += 1
    locations = list(existing.values())
    write_locations(csv_path, locations)
    log.info("Synced %d facility location rows (%d added)", len(locations), added)
    return locations


def geocode_address(address: str, timeout: float = 30.0) -> Optional[dict]:
    query = urlencode(
        {
            "addressString": address,
            "maxResults": 1,
            "minScore": 75,
            "echo": "true",
            "brief": "false",
            "outputSRS": 4326,
            "interpolation": "adaptive",
        }
    )
    with urlopen(f"{BC_GEOCODER_URL}?{query}", timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    features = payload.get("features") or []
    return features[0] if features else None


def geocode_locations(csv_path: Path, delay: float) -> list[FacilityLocation]:
    locations = read_locations(csv_path)
    updated = 0
    skipped = 0
    for location in locations:
        if location.has_coordinates():
            skipped += 1
            continue
        query = location.geocode_query
        if not query:
            skipped += 1
            continue
        feature = geocode_address(query)
        if not feature:
            location.verification_status = "geocode_failed"
            updated += 1
            log.warning("No geocoder result for %s (%s)", location.facility_name, query)
            continue

        coordinates = feature.get("geometry", {}).get("coordinates") or []
        properties = feature.get("properties", {})
        if len(coordinates) >= 2:
            location.longitude = f"{float(coordinates[0]):.7f}"
            location.latitude = f"{float(coordinates[1]):.7f}"
            location.address = properties.get("streetAddress") or location.address
            location.locality = properties.get("localityName") or location.locality
            location.province = properties.get("provinceCode") or location.province or "BC"
            location.verification_status = "geocoded_needs_review"
            location.notes = append_note(
                location.notes,
                f"BC Geocoder matched {properties.get('fullAddress', query)}"
                f" score={properties.get('score', '')} precision={properties.get('matchPrecision', '')}.",
            )
            updated += 1
        time.sleep(delay)

    write_locations(csv_path, locations)
    log.info("Geocoded %d rows (%d skipped)", updated, skipped)
    return locations


def append_note(existing: str, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def export_geojson(conn: sqlite3.Connection, locations: list[FacilityLocation], output_path: Path) -> None:
    latest_run_id = conn.execute(
        "SELECT MAX(run_id) FROM scrape_runs WHERE status IN ('completed', 'completed_with_errors')"
    ).fetchone()[0]
    if latest_run_id is None:
        raise ValueError("No completed scrape run found")

    stats = {
        (normalize_key(row[0]), normalize_key(row[1])): row
        for row in conn.execute(
            """
            SELECT
                facility_name,
                health_authority,
                COUNT(DISTINCT specialist_id) AS specialist_count,
                COUNT(DISTINCT procedure_key) AS procedure_count,
                COUNT(*) AS wait_time_row_count
            FROM specialist_wait_times
            WHERE run_id = ?
            GROUP BY facility_name, health_authority
            """,
            (latest_run_id,),
        )
    }

    features = []
    for location in locations:
        lat = parse_float(location.latitude)
        lng = parse_float(location.longitude)
        if lat is None or lng is None:
            continue
        stat = stats.get(location.key)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {
                    "facility_name": location.facility_name,
                    "health_authority": location.health_authority,
                    "address": location.address or None,
                    "locality": location.locality or None,
                    "province": location.province or None,
                    "postal_code": location.postal_code or None,
                    "source_label": location.source_label or None,
                    "source_url": location.source_url or None,
                    "verification_status": location.verification_status,
                    "latest_run_id": latest_run_id,
                    "specialist_count": stat[2] if stat else 0,
                    "procedure_count": stat[3] if stat else 0,
                    "wait_time_row_count": stat[4] if stat else 0,
                },
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")
    log.info("Exported %d mapped facilities to %s", len(features), output_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain map locations for BC surgery wait-time facilities.",
    )
    parser.add_argument("--db", default="bc_wait_times.db", help="Path to bc_wait_times.db")
    parser.add_argument("--csv", default="facility_locations.csv", help="Path to maintained facility locations CSV")
    parser.add_argument("--sync", action="store_true", help="Add any DB facilities missing from the CSV")
    parser.add_argument("--geocode", action="store_true", help="Geocode rows with reviewed addresses but no coordinates")
    parser.add_argument("--geocode-delay", type=float, default=0.2, help="Seconds between geocoder calls")
    parser.add_argument("--geojson", default="bc_wait_times_facilities.geojson", help="Output GeoJSON path")
    parser.add_argument("--export-geojson", action="store_true", help="Export mapped facilities as GeoJSON")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    csv_path = Path(args.csv)
    db_path = Path(args.db)
    locations = read_locations(csv_path)
    try:
        with sqlite3.connect(db_path) as conn:
            if args.sync:
                locations = sync_location_inventory(conn, csv_path)
            if args.geocode:
                locations = geocode_locations(csv_path, args.geocode_delay)
            if args.export_geojson:
                locations = read_locations(csv_path)
                export_geojson(conn, locations, Path(args.geojson))
    except Exception as exc:
        log.exception("Facility location build failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
