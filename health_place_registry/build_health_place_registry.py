#!/usr/bin/env python3
"""
BC Health Place Registry Builder
================================
Builds a shared place-name/address registry for BC health datasets.

The registry is intentionally source-agnostic: dataset-specific scrapers can keep
their own raw tables, while this registry carries reviewed civic addresses,
coordinates, aliases, and source membership for hospitals, clinics, diagnostic
facilities, health authorities, and other health organizations.

Usage:
    python build_health_place_registry.py --sync
    python build_health_place_registry.py --export-geojson
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

REGISTRY_FIELDS = [
    "canonical_name",
    "place_type",
    "place_status",
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
    "source_datasets",
    "aliases",
    "notes",
]

SITE_FIELDS = [
    "parent_canonical_name",
    "parent_place_type",
    "site_name",
    "site_type",
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

DEFAULT_REGISTRY = Path("health_place_registry.csv")
DEFAULT_GEOJSON = Path("health_place_registry.geojson")
DEFAULT_SITES = Path("health_place_sites.csv")
DEFAULT_SITES_GEOJSON = Path("health_place_sites.geojson")
DEFAULT_WAIT_TIMES_LOCATIONS = Path("../bc_wait_times/facility_locations.csv")
DEFAULT_MSP_DB = Path("../bc_msp_blue_book/bc_msp_blue_book.db")
BC_GEOCODER_URL = "https://geocoder.api.gov.bc.ca/addresses.json"

MSP_PLACE_TYPES = {"clinic", "diagnostic_facility", "health_authority", "hospital", "organization"}

TYPE_PRIORITY = {
    "hospital": 0,
    "diagnostic_facility": 1,
    "clinic": 2,
    "health_authority": 3,
    "organization": 4,
    "facility": 5,
}

log = logging.getLogger("health_place_registry")


@dataclass
class HealthPlace:
    canonical_name: str
    place_type: str
    place_status: str = "physical_place"
    health_authority: str = ""
    address: str = ""
    locality: str = ""
    province: str = ""
    postal_code: str = ""
    latitude: str = ""
    longitude: str = ""
    source_label: str = ""
    source_url: str = ""
    verification_status: str = "needs_review"
    source_datasets: str = ""
    aliases: str = ""
    notes: str = ""

    @property
    def key(self) -> str:
        return normalize_name(self.canonical_name)

    def alias_set(self) -> set[str]:
        values = {self.canonical_name}
        values.update(split_multi_value(self.aliases))
        return {value for value in values if value}

    def dataset_set(self) -> set[str]:
        return set(split_multi_value(self.source_datasets))

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in REGISTRY_FIELDS}


@dataclass
class HealthPlaceSite:
    parent_canonical_name: str
    parent_place_type: str
    site_name: str
    site_type: str
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
        return normalize_name(self.parent_canonical_name), normalize_name(self.site_name)

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in SITE_FIELDS}


def normalize_name(value: str) -> str:
    value = (value or "").casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[.'`]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def split_multi_value(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def join_multi_value(values: Iterable[str]) -> str:
    return "|".join(sorted({value.strip() for value in values if value and value.strip()}, key=str.casefold))


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_coordinates(place: HealthPlace) -> bool:
    return parse_float(place.latitude) is not None and parse_float(place.longitude) is not None


def site_has_coordinates(site: HealthPlaceSite) -> bool:
    return parse_float(site.latitude) is not None and parse_float(site.longitude) is not None


def read_registry(path: Path) -> dict[str, HealthPlace]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        compatible_fields = list(REGISTRY_FIELDS)
        fieldnames = reader.fieldnames or []
        missing = [field for field in compatible_fields if field not in fieldnames and field != "place_status"]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        places = {}
        for row in reader:
            if "place_status" not in row:
                row["place_status"] = default_place_status(row.get("place_type", ""))
            place = HealthPlace(**{field: (row.get(field) or "").strip() for field in REGISTRY_FIELDS})
            places[place.key] = place
        return places


def write_registry(path: Path, places: dict[str, HealthPlace]) -> None:
    rows = sorted(
        places.values(),
        key=lambda place: (
            TYPE_PRIORITY.get(place.place_type, 99),
            place.canonical_name.casefold(),
            place.health_authority.casefold(),
        ),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRY_FIELDS)
        writer.writeheader()
        for place in rows:
            writer.writerow(place.to_row())
    log.info("Wrote %d registry rows to %s", len(rows), path)


def read_sites(path: Path) -> dict[tuple[str, str], HealthPlaceSite]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in SITE_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        sites = {}
        for row in reader:
            site = HealthPlaceSite(**{field: (row.get(field) or "").strip() for field in SITE_FIELDS})
            sites[site.key] = site
        return sites


def write_sites(path: Path, sites: dict[tuple[str, str], HealthPlaceSite]) -> None:
    rows = sorted(
        sites.values(),
        key=lambda site: (
            site.parent_canonical_name.casefold(),
            site.site_name.casefold(),
            site.locality.casefold(),
        ),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SITE_FIELDS)
        writer.writeheader()
        for site in rows:
            writer.writerow(site.to_row())
    log.info("Wrote %d site rows to %s", len(rows), path)


def merge_site(sites: dict[tuple[str, str], HealthPlaceSite], incoming: HealthPlaceSite) -> bool:
    existing = sites.get(incoming.key)
    if existing is None:
        sites[incoming.key] = incoming
        return True
    for field in SITE_FIELDS:
        if field in {"parent_canonical_name", "site_name"}:
            continue
        value = getattr(incoming, field)
        if value and (not getattr(existing, field) or field in {"verification_status", "notes"}):
            if field == "notes":
                existing.notes = append_note(existing.notes, value)
            else:
                setattr(existing, field, value)
    return False


def merge_place(places: dict[str, HealthPlace], incoming: HealthPlace) -> bool:
    key = incoming.key
    existing = places.get(key)
    if existing is None:
        incoming.aliases = join_multi_value(incoming.alias_set())
        incoming.source_datasets = join_multi_value(incoming.dataset_set())
        places[key] = incoming
        return True

    if TYPE_PRIORITY.get(incoming.place_type, 99) < TYPE_PRIORITY.get(existing.place_type, 99):
        existing.place_type = incoming.place_type
    for field in [
        "place_status",
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
    ]:
        if not getattr(existing, field) and getattr(incoming, field):
            setattr(existing, field, getattr(incoming, field))

    existing.aliases = join_multi_value(existing.alias_set() | incoming.alias_set())
    existing.source_datasets = join_multi_value(existing.dataset_set() | incoming.dataset_set())
    existing.notes = append_note(existing.notes, incoming.notes)
    return False


def append_note(existing: str, note: str) -> str:
    note = (note or "").strip()
    if not note:
        return existing
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def load_wait_times_locations(path: Path) -> list[HealthPlace]:
    if not path.exists():
        log.warning("Wait-times locations file not found: %s", path)
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            facility_name = (row.get("facility_name") or "").strip()
            if not facility_name:
                continue
            rows.append(
                HealthPlace(
                    canonical_name=facility_name,
                    place_type=infer_place_type(facility_name, default="facility"),
                    place_status="physical_place",
                    health_authority=(row.get("health_authority") or "").strip(),
                    address=(row.get("address") or "").strip(),
                    locality=(row.get("locality") or "").strip(),
                    province=(row.get("province") or "").strip(),
                    postal_code=(row.get("postal_code") or "").strip(),
                    latitude=(row.get("latitude") or "").strip(),
                    longitude=(row.get("longitude") or "").strip(),
                    source_label=(row.get("source_label") or "").strip(),
                    source_url=(row.get("source_url") or "").strip(),
                    verification_status=(row.get("verification_status") or "needs_review").strip(),
                    source_datasets="bc_wait_times",
                    aliases=facility_name,
                    notes=append_note(
                        (row.get("notes") or "").strip(),
                        "Imported from bc_wait_times/facility_locations.csv.",
                    ),
                )
            )
        return rows


def load_msp_places(db_path: Path) -> list[HealthPlace]:
    if not db_path.exists():
        log.warning("MSP Blue Book database not found: %s", db_path)
        return []
    rows = []
    with sqlite3.connect(db_path) as conn:
        for payee_name, payee_type, first_year, latest_year, row_count, total_amount in conn.execute(
            """
            SELECT
                payee_name,
                payee_type,
                MIN(fiscal_year),
                MAX(fiscal_year),
                COUNT(*),
                SUM(amount)
            FROM msp_blue_book_payments
            WHERE payee_type IN ('clinic', 'diagnostic_facility', 'health_authority', 'hospital', 'organization')
              AND payee_name IS NOT NULL
              AND payee_name <> ''
            GROUP BY payee_name, payee_type
            ORDER BY payee_type, payee_name
            """
        ):
            rows.append(
                HealthPlace(
                    canonical_name=payee_name,
                    place_type=payee_type,
                    place_status=default_place_status(payee_type),
                    verification_status="needs_review",
                    source_datasets="bc_msp_blue_book",
                    aliases=payee_name,
                    notes=(
                        "Imported from MSP Blue Book payee names; address not verified. "
                        f"Seen {row_count} row(s), {first_year} to {latest_year}, "
                        f"total payments ${float(total_amount or 0):,.2f}."
                    ),
                )
            )
    return rows


def infer_place_type(name: str, default: str = "organization") -> str:
    lowered = name.casefold()
    if "health authority" in lowered:
        return "health_authority"
    if "hospital" in lowered:
        return "hospital"
    if re.search(r"\b(diagnostic|diagnostics|imaging|x[- ]?ray|ultrasound|mammography|laborator|laboratories|lab)\b", lowered):
        return "diagnostic_facility"
    if re.search(r"\bclinic|clinics\b", lowered):
        return "clinic"
    return default


def default_place_status(place_type: str) -> str:
    if place_type in {"hospital", "clinic", "diagnostic_facility", "health_authority", "facility"}:
        return "physical_place"
    return "ambiguous"


def geocode_query(place: HealthPlace) -> str | None:
    if not place.address.strip():
        return None
    parts = [place.address, place.locality, place.province or "BC", place.postal_code]
    query = ", ".join(part.strip() for part in parts if part and part.strip())
    return query or None


def site_geocode_query(site: HealthPlaceSite) -> str | None:
    if not site.address.strip():
        return None
    parts = [site.address, site.locality, site.province or "BC", site.postal_code]
    query = ", ".join(part.strip() for part in parts if part and part.strip())
    return query or None


def geocode_address(address: str, timeout: float = 30.0) -> dict | None:
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


def apply_enrichment(places: dict[str, HealthPlace], path: Path) -> int:
    if not path.exists():
        raise FileNotFoundError(path)
    updated = 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            canonical_name = (row.get("canonical_name") or "").strip()
            if not canonical_name:
                continue
            key = normalize_name(canonical_name)
            place = places.get(key)
            if place is None:
                place = HealthPlace(
                    canonical_name=canonical_name,
                    place_type=(row.get("place_type") or "organization").strip(),
                    place_status=(row.get("classification") or "physical_place").strip(),
                    source_datasets="manual_enrichment",
                    aliases=canonical_name,
                )
                places[key] = place

            classification = (row.get("classification") or "").strip()
            if classification:
                place.place_status = classification
            elif any((row.get(field) or "").strip() for field in ("address", "source_url")):
                place.place_status = "physical_place"

            for field in ["place_type", "address", "locality", "province", "postal_code", "source_label", "source_url", "verification_status"]:
                value = (row.get(field) or "").strip()
                if value:
                    setattr(place, field, value)

            place.aliases = join_multi_value(place.alias_set() | {canonical_name})
            place.source_datasets = join_multi_value(place.dataset_set() | {"manual_enrichment"})
            place.notes = append_note(place.notes, (row.get("notes") or "").strip())
            updated += 1
    log.info("Applied %d enrichment rows from %s", updated, path)
    return updated


def geocode_registry(places: dict[str, HealthPlace], delay: float) -> int:
    updated = 0
    for place in places.values():
        if has_coordinates(place):
            continue
        if place.place_status not in {"physical_place", ""}:
            continue
        query = geocode_query(place)
        if not query:
            continue
        feature = geocode_address(query)
        if not feature:
            place.verification_status = "geocode_failed"
            updated += 1
            continue
        coordinates = feature.get("geometry", {}).get("coordinates") or []
        properties = feature.get("properties", {})
        if len(coordinates) >= 2:
            place.longitude = f"{float(coordinates[0]):.7f}"
            place.latitude = f"{float(coordinates[1]):.7f}"
            place.address = properties.get("streetAddress") or place.address
            place.locality = properties.get("localityName") or place.locality
            place.province = properties.get("provinceCode") or place.province or "BC"
            if place.verification_status == "needs_review":
                place.verification_status = "geocoded_needs_review"
            place.notes = append_note(
                place.notes,
                f"BC Geocoder matched {properties.get('fullAddress', query)}"
                f" score={properties.get('score', '')} precision={properties.get('matchPrecision', '')}.",
            )
            updated += 1
        time.sleep(delay)
    log.info("Geocoded %d registry rows", updated)
    return updated


def geocode_sites(sites: dict[tuple[str, str], HealthPlaceSite], delay: float) -> int:
    updated = 0
    for site in sites.values():
        if site_has_coordinates(site):
            continue
        query = site_geocode_query(site)
        if not query:
            continue
        feature = geocode_address(query)
        if not feature:
            site.verification_status = "geocode_failed"
            updated += 1
            continue
        coordinates = feature.get("geometry", {}).get("coordinates") or []
        properties = feature.get("properties", {})
        if len(coordinates) >= 2:
            site.longitude = f"{float(coordinates[0]):.7f}"
            site.latitude = f"{float(coordinates[1]):.7f}"
            site.address = properties.get("streetAddress") or site.address
            site.locality = properties.get("localityName") or site.locality
            site.province = properties.get("provinceCode") or site.province or "BC"
            if site.verification_status == "needs_review":
                site.verification_status = "geocoded_needs_review"
            site.notes = append_note(
                site.notes,
                f"BC Geocoder matched {properties.get('fullAddress', query)}"
                f" score={properties.get('score', '')} precision={properties.get('matchPrecision', '')}.",
            )
            updated += 1
        time.sleep(delay)
    log.info("Geocoded %d site rows", updated)
    return updated


def export_geojson(path: Path, places: dict[str, HealthPlace]) -> None:
    features = []
    for place in places.values():
        if not has_coordinates(place):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(place.longitude), float(place.latitude)],
                },
                "properties": {
                    "canonical_name": place.canonical_name,
                    "place_type": place.place_type,
                    "place_status": place.place_status,
                    "health_authority": place.health_authority or None,
                    "address": place.address or None,
                    "locality": place.locality or None,
                    "province": place.province or None,
                    "postal_code": place.postal_code or None,
                    "source_label": place.source_label or None,
                    "source_url": place.source_url or None,
                    "verification_status": place.verification_status,
                    "source_datasets": split_multi_value(place.source_datasets),
                    "aliases": split_multi_value(place.aliases),
                },
            }
        )
    features.sort(key=lambda feature: feature["properties"]["canonical_name"].casefold())
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")
    log.info("Exported %d mapped places to %s", len(features), path)


def export_sites_geojson(path: Path, sites: dict[tuple[str, str], HealthPlaceSite]) -> None:
    features = []
    for site in sites.values():
        if not site_has_coordinates(site):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(site.longitude), float(site.latitude)],
                },
                "properties": {
                    "parent_canonical_name": site.parent_canonical_name,
                    "parent_place_type": site.parent_place_type,
                    "site_name": site.site_name,
                    "site_type": site.site_type,
                    "address": site.address or None,
                    "locality": site.locality or None,
                    "province": site.province or None,
                    "postal_code": site.postal_code or None,
                    "source_label": site.source_label or None,
                    "source_url": site.source_url or None,
                    "verification_status": site.verification_status,
                    "notes": site.notes or None,
                },
            }
        )
    features.sort(
        key=lambda feature: (
            feature["properties"]["parent_canonical_name"].casefold(),
            feature["properties"]["site_name"].casefold(),
        )
    )
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")
    log.info("Exported %d mapped child sites to %s", len(features), path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the shared BC health place registry.")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="Output/input registry CSV")
    parser.add_argument("--geojson", default=str(DEFAULT_GEOJSON), help="Output GeoJSON path")
    parser.add_argument("--sites", default=str(DEFAULT_SITES), help="Input/output child-sites CSV")
    parser.add_argument("--sites-geojson", default=str(DEFAULT_SITES_GEOJSON), help="Output child-sites GeoJSON path")
    parser.add_argument("--wait-times-locations", default=str(DEFAULT_WAIT_TIMES_LOCATIONS))
    parser.add_argument("--msp-db", default=str(DEFAULT_MSP_DB))
    parser.add_argument("--sync", action="store_true", help="Merge source names into the registry CSV")
    parser.add_argument(
        "--apply-enrichment",
        action="append",
        default=[],
        help="Apply a reviewed enrichment CSV. Can be provided multiple times.",
    )
    parser.add_argument("--geocode", action="store_true", help="Geocode rows with addresses but no coordinates")
    parser.add_argument("--geocode-sites", action="store_true", help="Geocode child-site rows with addresses but no coordinates")
    parser.add_argument("--geocode-delay", type=float, default=0.2, help="Seconds between geocoder calls")
    parser.add_argument("--export-geojson", action="store_true", help="Export mapped registry rows as GeoJSON")
    parser.add_argument("--export-sites-geojson", action="store_true", help="Export mapped child-site rows as GeoJSON")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    registry_path = Path(args.registry)
    sites_path = Path(args.sites)
    places = read_registry(registry_path)
    sites = read_sites(sites_path)

    if args.sync:
        added = 0
        for incoming in load_wait_times_locations(Path(args.wait_times_locations)):
            added += 1 if merge_place(places, incoming) else 0
        for incoming in load_msp_places(Path(args.msp_db)):
            added += 1 if merge_place(places, incoming) else 0
        write_registry(registry_path, places)
        log.info("Sync complete (%d added, %d total)", added, len(places))

    if args.apply_enrichment:
        for enrichment_path in args.apply_enrichment:
            apply_enrichment(places, Path(enrichment_path))
        write_registry(registry_path, places)

    if args.geocode:
        geocode_registry(places, args.geocode_delay)
        write_registry(registry_path, places)

    if args.geocode_sites:
        geocode_sites(sites, args.geocode_delay)
        write_sites(sites_path, sites)

    if args.export_geojson:
        if not places and registry_path.exists():
            places = read_registry(registry_path)
        export_geojson(Path(args.geojson), places)

    if args.export_sites_geojson:
        if not sites and sites_path.exists():
            sites = read_sites(sites_path)
        export_sites_geojson(Path(args.sites_geojson), sites)

    if (
        not args.sync
        and not args.apply_enrichment
        and not args.geocode
        and not args.geocode_sites
        and not args.export_geojson
        and not args.export_sites_geojson
    ):
        log.warning(
            "No action requested. Use --sync, --apply-enrichment, --geocode, "
            "--geocode-sites, --export-geojson, and/or --export-sites-geojson."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
