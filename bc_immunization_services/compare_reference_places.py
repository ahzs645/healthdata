#!/usr/bin/env python3
"""
Compare manual reference observations against the BC immunization-services export.

The reference file is for manually entered observations from Vaccines411 or other
review sources. This script does not fetch or scrape those sources.
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BC_CSV = SCRIPT_DIR / "output" / "bc-immunization-services.csv"
DEFAULT_REFERENCE_CSV = SCRIPT_DIR / "reference_checks" / "reference_places.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "reference_checks" / "missing_from_bc_review.csv"

REFERENCE_FIELDS = [
    "reference_source",
    "search_context",
    "observed_name",
    "observed_address",
    "observed_city",
    "observed_province",
    "observed_postal_code",
    "observed_phone",
    "observed_website",
    "vaccine_categories",
    "observed_url",
    "observed_at",
    "notes",
]

OUTPUT_FIELDS = REFERENCE_FIELDS + [
    "match_status",
    "match_score",
    "match_reason",
    "bc_source_row_number",
    "bc_organization_name",
    "bc_service_name",
    "bc_address",
    "bc_city",
    "bc_postal_code",
    "bc_phone",
    "bc_website",
    "bc_healthlinkbc_url",
]

STOP_WORDS = {
    "and",
    "bc",
    "canada",
    "clinic",
    "clinics",
    "drug",
    "drugs",
    "health",
    "immunization",
    "immunizations",
    "pharmacy",
    "services",
    "the",
    "vaccination",
    "vaccinations",
    "vaccine",
    "vaccines",
}


@dataclass
class Match:
    row: dict[str, str] | None
    score: float
    reason: str


def clean(value: str | None) -> str:
    return (value or "").strip()


def normalize_text(value: str) -> str:
    value = clean(value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[.'`]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def normalize_postal(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean(value).casefold()).upper()


def normalize_phone(value: str) -> str:
    return re.sub(r"\D+", "", clean(value))


def tokens(value: str) -> set[str]:
    return {token for token in normalize_text(value).split() if len(token) > 1 and token not in STOP_WORDS}


def token_similarity(left: str, right: str) -> float:
    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    containment = intersection / min(len(left_tokens), len(right_tokens))
    jaccard = intersection / len(left_tokens | right_tokens)
    return (containment * 0.7) + (jaccard * 0.3)


def exactish(left: str, right: str) -> bool:
    return normalize_text(left) == normalize_text(right) and bool(normalize_text(left))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [{key: clean(value) for key, value in row.items()} for row in reader]


def candidate_score(reference: dict[str, str], candidate: dict[str, str]) -> Match:
    ref_name = reference.get("observed_name", "")
    ref_address = reference.get("observed_address", "")
    ref_city = reference.get("observed_city", "")
    ref_postal = normalize_postal(reference.get("observed_postal_code", ""))
    ref_phone = normalize_phone(reference.get("observed_phone", ""))

    bc_name = candidate.get("organization_name", "")
    bc_address = candidate.get("address", "")
    bc_city = candidate.get("city", "")
    bc_postal = normalize_postal(candidate.get("postal_code", ""))
    bc_phone = normalize_phone(candidate.get("phone", ""))

    name_score = token_similarity(ref_name, bc_name)
    address_score = token_similarity(ref_address, bc_address)
    city_match = exactish(ref_city, bc_city)
    postal_match = bool(ref_postal and ref_postal == bc_postal)
    phone_match = bool(ref_phone and bc_phone and ref_phone[-7:] == bc_phone[-7:])

    if postal_match and exactish(ref_address, bc_address):
        return Match(candidate, 1.0, "same postal code and address")
    if phone_match and name_score >= 0.55:
        return Match(candidate, 0.96, "same phone and similar name")
    if postal_match and name_score >= 0.72:
        return Match(candidate, 0.94, "same postal code and similar name")
    if city_match and name_score >= 0.72 and address_score >= 0.45:
        return Match(candidate, 0.88, "same city with similar name/address")
    if postal_match and address_score >= 0.72:
        return Match(candidate, 0.86, "same postal code and similar address")
    if city_match and name_score >= 0.84:
        return Match(candidate, 0.78, "same city and strong name similarity")

    combined = (name_score * 0.55) + (address_score * 0.35) + (0.10 if city_match else 0)
    if postal_match:
        combined += 0.15
    return Match(candidate, min(combined, 0.74), "weak fuzzy candidate")


def best_match(reference: dict[str, str], bc_rows: list[dict[str, str]]) -> Match:
    best = Match(None, 0.0, "")
    ref_city = normalize_text(reference.get("observed_city", ""))
    ref_postal = normalize_postal(reference.get("observed_postal_code", ""))

    candidates = []
    for row in bc_rows:
        bc_city = normalize_text(row.get("city", ""))
        bc_postal = normalize_postal(row.get("postal_code", ""))
        if ref_postal and bc_postal and ref_postal[:3] == bc_postal[:3]:
            candidates.append(row)
        elif ref_city and ref_city == bc_city:
            candidates.append(row)

    if not candidates:
        candidates = bc_rows

    for candidate in candidates:
        match = candidate_score(reference, candidate)
        if match.score > best.score:
            best = match
    return best


def status_for(match: Match) -> str:
    if match.score >= 0.86:
        return "matched"
    if match.score >= 0.72:
        return "possible_match"
    return "missing_from_bc_source"


def output_row(reference: dict[str, str], match: Match) -> dict[str, str]:
    candidate = match.row or {}
    row = {field: reference.get(field, "") for field in REFERENCE_FIELDS}
    row.update(
        {
            "match_status": status_for(match),
            "match_score": f"{match.score:.2f}",
            "match_reason": match.reason,
            "bc_source_row_number": candidate.get("source_row_number", ""),
            "bc_organization_name": candidate.get("organization_name", ""),
            "bc_service_name": candidate.get("service_name", ""),
            "bc_address": candidate.get("address", ""),
            "bc_city": candidate.get("city", ""),
            "bc_postal_code": candidate.get("postal_code", ""),
            "bc_phone": candidate.get("phone", ""),
            "bc_website": candidate.get("website", ""),
            "bc_healthlinkbc_url": candidate.get("healthlinkbc_url", ""),
        }
    )
    return row


def compare(reference_path: Path, bc_path: Path, output_path: Path) -> tuple[int, dict[str, int]]:
    reference_rows = [row for row in read_csv(reference_path) if clean(row.get("observed_name"))]
    bc_rows = read_csv(bc_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"matched": 0, "possible_match": 0, "missing_from_bc_source": 0}
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for reference in reference_rows:
            match = best_match(reference, bc_rows)
            row = output_row(reference, match)
            counts[row["match_status"]] += 1
            writer.writerow(row)
    return len(reference_rows), counts


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare manual reference places against the BC source export.")
    parser.add_argument("--bc-csv", default=str(DEFAULT_BC_CSV))
    parser.add_argument("--reference-csv", default=str(DEFAULT_REFERENCE_CSV))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    total, counts = compare(Path(args.reference_csv), Path(args.bc_csv), Path(args.output_csv))
    print(f"Reference rows compared: {total}")
    for status, count in counts.items():
        print(f"{status}: {count}")
    print(f"Output: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
