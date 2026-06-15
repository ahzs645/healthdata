#!/usr/bin/env python3
"""
BC Surgery Wait Times Scraper
=============================
Iteratively scrapes https://swt.hlth.gov.bc.ca/swt and stores wait-time data
in a SQLite database. Each execution creates a new "scrape run" so the database
can be refreshed on demand while keeping historical snapshots.

The site lists the same procedure twice in some cases (adult "Y" and pediatric
"N"), so each (procedure_id, adult_flag) combination is treated as a distinct
procedure variant.

Usage:
    python scrape_bc_wait_times.py --full
    python scrape_bc_wait_times.py --limit 10 --delay 1.0
    python scrape_bc_wait_times.py --db ./wait_times.db --workers 4
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://swt.hlth.gov.bc.ca/swt"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

log = logging.getLogger("bc_wait_times")


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #


@dataclass
class ProcedureRef:
    procedure_key: str
    procedure_id: str
    name: str
    adult: str  # "Y" or "N" as discovered from the A-Z listing


@dataclass
class SpecialistRef:
    specialist_id: str
    first_name: str
    last_name: str
    full_name: str


@dataclass
class ProcedureSummary:
    procedure_key: str
    procedure_id: str
    procedure_name: str
    report_as_of: Optional[str]
    period_start: Optional[str]
    period_end: Optional[str]
    cases_waiting_raw: str
    cases_waiting: Optional[int]
    p50_weeks: Optional[float]
    p90_weeks: Optional[float]
    data_source: Optional[str] = None


@dataclass
class HealthAuthorityRecord:
    health_authority: str
    cases_waiting_raw: str
    cases_waiting: Optional[int]
    p50_weeks: Optional[float]
    p90_weeks: Optional[float]


@dataclass
class FacilityRecord:
    health_authority: str
    facility_name: str
    facility_id: Optional[str]
    cases_waiting_raw: str
    cases_waiting: Optional[int]
    p50_weeks: Optional[float]
    p90_weeks: Optional[float]


@dataclass
class SpecialistRecord:
    health_authority: str
    facility_name: str
    specialist_id: Optional[str]
    specialist_name: str
    cases_waiting_raw: str
    cases_waiting: Optional[int]
    p50_weeks: Optional[float]
    p90_weeks: Optional[float]


@dataclass
class SpecialistProfileRecord:
    specialist_id: str
    specialist_name: str
    adult: str
    facility_name: str
    procedure_key: str
    procedure_name: str
    report_as_of: Optional[str]
    period_start: Optional[str]
    period_end: Optional[str]
    cases_waiting_raw: str
    cases_waiting: Optional[int]
    p50_weeks: Optional[float]
    p90_weeks: Optional[float]
    data_source: Optional[str] = None


@dataclass
class ProcedurePage:
    procedure_key: str
    procedure_id: str
    procedure_name: str
    adult: str
    url: str
    report_as_of: Optional[str]
    period_start: Optional[str]
    period_end: Optional[str]
    definition: Optional[str]
    data_source: Optional[str]
    facility_ids: dict[str, str] = field(default_factory=dict)
    summary: Optional[ProcedureSummary] = None
    health_authorities: list[HealthAuthorityRecord] = field(default_factory=list)
    facilities: list[FacilityRecord] = field(default_factory=list)
    specialists: list[SpecialistRecord] = field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def parse_weeks(value: str) -> Optional[float]:
    """Convert strings like '1.1 weeks', 'N/A', '22.3 weeks' to float weeks."""
    value = value.strip()
    if not value or value.upper() == "N/A":
        return None
    # Some cells contain the full tooltip after the value; grab the leading number.
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
    if m:
        return float(m.group(1))
    return None


def parse_cases(value: str) -> tuple[str, Optional[int]]:
    """Return (raw_text, numeric_or_none). 'Less than 5' is stored as None."""
    raw = re.sub(r"For the purposes of.*", "", value, flags=re.S).strip()
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw.lower() == "less than 5":
        return raw, None
    # Remove commas so "2,270" parses correctly.
    cleaned = raw.replace(",", "")
    m = re.search(r"([0-9]+)", cleaned)
    if m:
        return raw, int(m.group(1))
    return raw, None


def clean_cell_text(td) -> str:
    """
    Return visible text for a table cell, stripping hidden PrimeFaces tooltip
    markup that BeautifulSoup would otherwise include.
    """
    # Work on a shallow copy so we don't mutate the parsed tree unexpectedly.
    cell = BeautifulSoup(str(td), "html.parser").find()
    for el in cell.find_all(["div", "script"], class_=lambda c: c and "ui-tooltip" in c):
        el.decompose()
    return cell.get_text(" ", strip=True)


def parse_date_header(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract report_as_of, period_start, period_end from the table header text.
    The site uses mixed date formats (e.g. "Mar 31, 2026" and "01-Feb-2026").
    """
    report_as_of = None
    period_start = None
    period_end = None

    # "as of Apr 30, 2026" or "as of Mar 31, 2026"
    m_as_of = re.search(r"as of\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text)
    if m_as_of:
        report_as_of = normalize_date(m_as_of.group(1))

    # "Between Jan 01, 2026 and Mar 31, 2026" or "Between 01-Feb-2026 and 30-Apr-2026"
    m_between = re.search(
        r"Between\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{1,2}-[A-Za-z]{3}-\d{4})\s+and\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d{1,2}-[A-Za-z]{3}-\d{4})",
        text,
    )
    if m_between:
        period_start = normalize_date(m_between.group(1))
        period_end = normalize_date(m_between.group(2))

    return report_as_of, period_start, period_end


def normalize_date(text: str) -> Optional[str]:
    text = text.strip()
    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%B-%Y",
    ):
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
    return None


def extract_health_authorities(soup: BeautifulSoup) -> set[str]:
    """Read the health authority filter dropdown to know the valid HA names."""
    ha_select = soup.find("select", {"name": "form1:healthAuthorityFilter"})
    if not ha_select:
        return set()
    names: set[str] = set()
    for opt in ha_select.find_all("option"):
        text = opt.get_text(strip=True)
        if text and opt.get("value"):
            names.add(text)
    return names


def extract_procedure_definition(soup: BeautifulSoup, proc_name: str) -> Optional[str]:
    """Extract the procedure definition paragraph from the results page."""
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        prefix = f"Definition: {proc_name}"
        if prefix in text:
            # Keep everything after the dash following the definition label.
            parts = text.split(" - ", 1)
            if len(parts) == 2:
                return " ".join(parts[1].split())
            # Fallback: remove the definition label itself.
            return " ".join(text.replace(prefix, "").strip(" -").split())
    return None


def extract_facility_ids(soup: BeautifulSoup) -> dict[str, str]:
    """
    Read the facility filter dropdown to map facility names to stable IDs.
    Labels are formatted as 'City - Facility Name'.
    """
    mapping: dict[str, str] = {}
    container = soup.find("div", {"id": "form1:facilityFilter"})
    if not container:
        return mapping
    for inp in container.find_all("input", {"type": "checkbox"}):
        value = inp.get("value")
        inp_id = inp.get("id")
        if not value or not inp_id:
            continue
        label = container.find("label", {"for": inp_id})
        if not label:
            continue
        label_text = label.get_text(strip=True)
        # Labels look like "Abbotsford - Abbotsford Regional Hospital And Cancer Centre"
        if " - " in label_text:
            facility_name = label_text.split(" - ", 1)[1]
        else:
            facility_name = label_text
        mapping[facility_name] = value
    return mapping


def determine_data_source(proc_name: str) -> Optional[str]:
    """Map non-SPR procedures to their external data source."""
    lower = proc_name.lower()
    if "corneal transplant" in lower:
        return "Eye Bank of BC"
    if "coronary artery bypass graft" in lower or "open heart surgery" in lower:
        return "Cardiac Services BC"
    return None


# --------------------------------------------------------------------------- #
# Network helpers
# --------------------------------------------------------------------------- #


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.adapters.Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
        )
    )
    session.mount("https://", adapter)
    return session


def fetch_html(session: requests.Session, url: str) -> BeautifulSoup:
    log.debug("Fetching %s", url)
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# --------------------------------------------------------------------------- #
# List scrapers
# --------------------------------------------------------------------------- #


def scrape_procedures(session: requests.Session) -> list[ProcedureRef]:
    """Scrape the Procedures A-Z page for every available procedure variant."""
    url = f"{BASE_URL}/ProceduresAToZ.xhtml"
    soup = fetch_html(session, url)
    links = soup.find_all("a", href=lambda h: h and "WaitTimesResults" in h)

    procedures: list[ProcedureRef] = []
    seen: set[str] = set()
    for a in links:
        href = a["href"]
        qs = parse_qs(urlparse(href).query)
        proc_id_list = qs.get("rollupProcedure")
        if not proc_id_list:
            continue
        proc_id = proc_id_list[0]
        adult = qs.get("adult", ["Y"])[0]
        key = f"{proc_id}:{adult}"
        if key in seen:
            continue
        seen.add(key)
        name = unquote_plus(qs.get("procName", [a.get_text(strip=True)])[0])
        procedures.append(
            ProcedureRef(procedure_key=key, procedure_id=proc_id, name=name, adult=adult)
        )

    procedures.sort(key=lambda p: (p.name.lower(), p.adult, p.procedure_id))
    log.info("Discovered %d procedure variants from A-Z listing", len(procedures))
    return procedures


def scrape_specialists(session: requests.Session) -> list[SpecialistRef]:
    """Scrape the Specialists A-Z page for every listed specialist."""
    url = f"{BASE_URL}/SpecialistsAToZ.xhtml"
    soup = fetch_html(session, url)
    links = soup.find_all("a", href=lambda h: h and "SpecialistProfile" in h)

    specialists: list[SpecialistRef] = []
    seen: set[str] = set()
    for a in links:
        href = a["href"]
        qs = parse_qs(urlparse(href).query)
        spec_id_list = qs.get("rollupSurgeonId")
        if not spec_id_list:
            continue
        spec_id = spec_id_list[0]
        if spec_id in seen:
            continue
        seen.add(spec_id)
        full = a.get_text(strip=True)
        first = unquote_plus(qs.get("firstName", [""])[0])
        last = unquote_plus(qs.get("lastName", [""])[0]).rstrip(",")
        specialists.append(
            SpecialistRef(
                specialist_id=spec_id,
                first_name=first,
                last_name=last,
                full_name=full,
            )
        )

    specialists.sort(key=lambda s: s.full_name.lower())
    log.info("Discovered %d specialists from A-Z listing", len(specialists))
    return specialists


def scrape_procedure_groupings(session: requests.Session) -> dict[str, list[str]]:
    """
    Scrape the Procedure Groupings page to map body-area categories to
    procedure group names displayed on the site.
    """
    url = f"{BASE_URL}/ProcedureGroupings.xhtml"
    soup = fetch_html(session, url)
    categories: dict[str, list[str]] = {}
    current_category: Optional[str] = None

    for table in soup.find_all("table", class_="procedure-grouping"):
        header = table.find("thead")
        if header:
            cat_name = header.get_text(" ", strip=True)
            if cat_name:
                current_category = cat_name
                categories.setdefault(current_category, [])
        if not current_category:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            # First data column is the procedure group name.
            group_name = clean_cell_text(cells[0])
            if not group_name:
                continue
            # Skip the header row inside the table body.
            if group_name.lower() in {"procedure group", "provincial procedures included"}:
                continue
            # Some group names include a "(non-SPR data)" note on a separate line.
            group_name = group_name.split("(")[0].strip()
            if group_name and group_name not in categories[current_category]:
                categories[current_category].append(group_name)

    log.info("Discovered %d procedure categories", len(categories))
    return categories


# --------------------------------------------------------------------------- #
# Procedure page scraper
# --------------------------------------------------------------------------- #


def scrape_procedure_page(
    session: requests.Session, proc: ProcedureRef
) -> ProcedurePage:
    """Scrape a single WaitTimesResults page and return structured data."""
    url = (
        f"{BASE_URL}/WaitTimesResults.xhtml?"
        f"rollupProcedure={proc.procedure_id}&"
        f"procName={requests.utils.quote(proc.name)}&"
        f"adult={proc.adult}"
    )
    page = ProcedurePage(
        procedure_key=proc.procedure_key,
        procedure_id=proc.procedure_id,
        procedure_name=proc.name,
        adult=proc.adult,
        url=url,
        report_as_of=None,
        period_start=None,
        period_end=None,
        definition=None,
        data_source=determine_data_source(proc.name),
        summary=None,
    )

    try:
        soup = fetch_html(session, url)
    except requests.HTTPError as exc:
        # Some procedures return 500 when there is no data for the adult/pediatric flag.
        page.error = f"HTTP {exc.response.status_code}"
        return page
    except Exception as exc:
        page.error = f"{type(exc).__name__}: {exc}"
        return page

    # Extract reporting period from the table header.
    thead = soup.find("thead")
    if thead:
        header_text = thead.get_text(" ", strip=True)
        page.report_as_of, page.period_start, page.period_end = parse_date_header(
            header_text
        )

    page.definition = extract_procedure_definition(soup, proc.name)
    page.facility_ids = extract_facility_ids(soup)

    health_authorities = extract_health_authorities(soup)

    # The site uses at least two different result table IDs/layouts.
    results = soup.find(id="form1:nonSprResultsTable") or soup.find(
        id="form1:resultsTable"
    )
    if not results:
        page.error = "Results table not found"
        return page

    rows = results.find_all("tr")
    current_ha: Optional[str] = None
    current_facility: Optional[str] = None
    summary_captured = False

    for tr in rows:
        tds = tr.find_all(["td", "th"])
        if len(tds) != 4:
            # The site renders each data row as a visible 4-cell row plus one or
            # more duplicate/hidden rows. We only need the 4-cell visible rows.
            continue

        cell_texts = [clean_cell_text(td) for td in tds]
        name = cell_texts[0]
        cases_text = cell_texts[1]
        p50_text = cell_texts[2]
        p90_text = cell_texts[3]

        if not name:
            continue

        cases_raw, cases_num = parse_cases(cases_text)
        p50 = parse_weeks(p50_text)
        p90 = parse_weeks(p90_text)

        # Determine whether this row represents the procedure, HA, facility, or specialist.
        spec_link = tds[0].find(
            "a", href=lambda h: h and "SpecialistProfile" in h
        )
        is_specialist = bool(spec_link)
        is_procedure = (name == proc.name)
        is_health_authority = name in health_authorities

        if is_procedure and not summary_captured:
            page.summary = ProcedureSummary(
                procedure_key=proc.procedure_key,
                procedure_id=proc.procedure_id,
                procedure_name=proc.name,
                report_as_of=page.report_as_of,
                period_start=page.period_start,
                period_end=page.period_end,
                cases_waiting_raw=cases_raw,
                cases_waiting=cases_num,
                p50_weeks=p50,
                p90_weeks=p90,
                data_source=page.data_source,
            )
            summary_captured = True
        elif is_health_authority:
            current_ha = name
            current_facility = None
            page.health_authorities.append(
                HealthAuthorityRecord(
                    health_authority=name,
                    cases_waiting_raw=cases_raw,
                    cases_waiting=cases_num,
                    p50_weeks=p50,
                    p90_weeks=p90,
                )
            )
        elif is_specialist:
            if current_ha and current_facility:
                spec_id: Optional[str] = None
                if spec_link and spec_link.get("href"):
                    qs = parse_qs(urlparse(spec_link["href"]).query)
                    spec_id = qs.get("rollupSurgeonId", [None])[0]
                page.specialists.append(
                    SpecialistRecord(
                        health_authority=current_ha,
                        facility_name=current_facility,
                        specialist_id=spec_id,
                        specialist_name=name,
                        cases_waiting_raw=cases_raw,
                        cases_waiting=cases_num,
                        p50_weeks=p50,
                        p90_weeks=p90,
                    )
                )
            else:
                log.warning(
                    "Specialist row before facility context: %s (%s)",
                    name,
                    proc.procedure_key,
                )
        else:
            # Anything else with a name is treated as a facility.
            current_facility = name
            page.facilities.append(
                FacilityRecord(
                    health_authority=current_ha or "Unknown",
                    facility_name=name,
                    facility_id=page.facility_ids.get(name),
                    cases_waiting_raw=cases_raw,
                    cases_waiting=cases_num,
                    p50_weeks=p50,
                    p90_weeks=p90,
                )
            )

    return page


def scrape_specialist_profile(
    session: requests.Session,
    spec: SpecialistRef,
    procedure_name_to_keys: dict[str, list[str]],
) -> list[SpecialistProfileRecord]:
    """
    Scrape a single SpecialistProfile page and return procedure-level wait-time
    records for that specialist, pivoted by facility.
    """
    url = (
        f"{BASE_URL}/SpecialistProfile.xhtml?"
        f"rollupSurgeonId={spec.specialist_id}&"
        f"surgeonNm={requests.utils.quote(spec.full_name)}"
    )
    records: list[SpecialistProfileRecord] = []

    try:
        soup = fetch_html(session, url)
    except requests.HTTPError as exc:
        log.warning("Specialist profile %s returned HTTP %s", spec.specialist_id, exc.response.status_code)
        return records
    except Exception as exc:
        log.warning("Specialist profile %s failed: %s", spec.specialist_id, exc)
        return records

    # Extract reporting period from the first table header we find.
    report_as_of: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    thead = soup.find("thead")
    if thead:
        report_as_of, period_start, period_end = parse_date_header(
            thead.get_text(" ", strip=True)
        )

    # The page has ADULT and PEDIATRIC tab panels.
    tab_headers = {
        tab.get("data-index"): tab
        for tab in soup.find_all("li", class_="ui-tabs-header")
        if tab.get("data-index") is not None
    }
    tab_panels = soup.find_all("div", class_="ui-tabs-panel")
    for panel in tab_panels:
        # Determine adult flag from the matching tab header.
        adult = "Y"
        panel_index = panel.get("data-index")
        header = tab_headers.get(panel_index) if panel_index is not None else None
        if header:
            tab_text = header.get_text(" ", strip=True).lower()
            if "pediatric" in tab_text or "under 17" in tab_text:
                adult = "N"

        # Skip panels that contain no data.
        if "No procedures found" in panel.get_text(" ", strip=True):
            continue

        # Each facility is introduced by a bold span, followed by a data table.
        current_facility: Optional[str] = None
        for child in panel.descendants:
            if child.name == "span" and child.get("style") and "font-weight:600" in child.get("style", ""):
                current_facility = child.get_text(strip=True)
                continue
            if child.name != "div":
                continue
            if "ui-datatable" not in child.get("class", []):
                continue
            table = child.find("table", role="grid")
            if not table:
                continue

            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) != 4:
                    continue

                proc_cell = tds[0]
                proc_link = proc_cell.find("a", href=True)
                proc_name = proc_link.get_text(strip=True) if proc_link else clean_cell_text(proc_cell)
                if not proc_name:
                    continue

                cases_raw, cases_num = parse_cases(clean_cell_text(tds[1]))
                p50 = parse_weeks(clean_cell_text(tds[2]))
                p90 = parse_weeks(clean_cell_text(tds[3]))

                # Try to resolve the procedure key from the link or the lookup map.
                procedure_key: Optional[str] = None
                if proc_link and proc_link.get("href"):
                    qs = parse_qs(urlparse(proc_link["href"]).query)
                    link_name = unquote_plus(qs.get("procName", [""])[0])
                    link_adult = qs.get("adult", [adult])[0]
                    if link_name:
                        for pk in procedure_name_to_keys.get(link_name, []):
                            if pk.endswith(f":{link_adult}"):
                                procedure_key = pk
                                break
                if not procedure_key:
                    for pk in procedure_name_to_keys.get(proc_name, []):
                        if pk.endswith(f":{adult}"):
                            procedure_key = pk
                            break

                records.append(
                    SpecialistProfileRecord(
                        specialist_id=spec.specialist_id,
                        specialist_name=spec.full_name,
                        adult=adult,
                        facility_name=current_facility or "Unknown",
                        procedure_key=procedure_key or proc_name,
                        procedure_name=proc_name,
                        report_as_of=report_as_of,
                        period_start=period_start,
                        period_end=period_end,
                        cases_waiting_raw=cases_raw,
                        cases_waiting=cases_num,
                        p50_weeks=p50,
                        p90_weeks=p90,
                        data_source=determine_data_source(proc_name),
                    )
                )

    return records


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    procedures_total INTEGER,
    procedures_ok INTEGER,
    procedures_failed INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS procedures (
    procedure_key TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL,
    name TEXT NOT NULL,
    adult_flag TEXT NOT NULL,
    definition TEXT,
    url_template TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_procedures_id ON procedures(procedure_id);

CREATE TABLE IF NOT EXISTS procedure_categories (
    category_name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS procedure_category_mappings (
    procedure_key TEXT NOT NULL REFERENCES procedures(procedure_key) ON DELETE CASCADE,
    category_name TEXT NOT NULL REFERENCES procedure_categories(category_name) ON DELETE CASCADE,
    PRIMARY KEY (procedure_key, category_name)
);

CREATE INDEX IF NOT EXISTS idx_category_proc ON procedure_category_mappings(procedure_key);
CREATE INDEX IF NOT EXISTS idx_category_name ON procedure_category_mappings(category_name);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    facility_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facilities_name ON facilities(facility_name);

CREATE TABLE IF NOT EXISTS specialists (
    specialist_id TEXT PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedure_summaries (
    summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(run_id) ON DELETE CASCADE,
    procedure_key TEXT NOT NULL REFERENCES procedures(procedure_key),
    report_as_of TEXT,
    period_start TEXT,
    period_end TEXT,
    data_source TEXT,
    cases_waiting_raw TEXT,
    cases_waiting INTEGER,
    p50_weeks REAL,
    p90_weeks REAL,
    scraped_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proc_summaries_run ON procedure_summaries(run_id);

CREATE TABLE IF NOT EXISTS health_authority_wait_times (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(run_id) ON DELETE CASCADE,
    procedure_key TEXT NOT NULL REFERENCES procedures(procedure_key),
    health_authority TEXT NOT NULL,
    cases_waiting_raw TEXT,
    cases_waiting INTEGER,
    p50_weeks REAL,
    p90_weeks REAL,
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facility_wait_times (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(run_id) ON DELETE CASCADE,
    procedure_key TEXT NOT NULL REFERENCES procedures(procedure_key),
    health_authority TEXT NOT NULL,
    facility_id TEXT REFERENCES facilities(facility_id),
    facility_name TEXT NOT NULL,
    cases_waiting_raw TEXT,
    cases_waiting INTEGER,
    p50_weeks REAL,
    p90_weeks REAL,
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specialist_wait_times (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(run_id) ON DELETE CASCADE,
    procedure_key TEXT NOT NULL REFERENCES procedures(procedure_key),
    health_authority TEXT NOT NULL,
    facility_name TEXT NOT NULL,
    specialist_id TEXT,
    specialist_name TEXT NOT NULL,
    cases_waiting_raw TEXT,
    cases_waiting INTEGER,
    p50_weeks REAL,
    p90_weeks REAL,
    scraped_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ha_run ON health_authority_wait_times(run_id);
CREATE INDEX IF NOT EXISTS idx_ha_proc ON health_authority_wait_times(procedure_key);
CREATE INDEX IF NOT EXISTS idx_facility_run ON facility_wait_times(run_id);
CREATE INDEX IF NOT EXISTS idx_facility_proc ON facility_wait_times(procedure_key);
CREATE INDEX IF NOT EXISTS idx_specialist_run ON specialist_wait_times(run_id);
CREATE INDEX IF NOT EXISTS idx_specialist_proc ON specialist_wait_times(procedure_key);
CREATE INDEX IF NOT EXISTS idx_specialist_name ON specialist_wait_times(specialist_name);

CREATE TABLE IF NOT EXISTS specialist_profiles (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scrape_runs(run_id) ON DELETE CASCADE,
    specialist_id TEXT NOT NULL,
    specialist_name TEXT NOT NULL,
    adult_flag TEXT NOT NULL,
    facility_name TEXT NOT NULL,
    procedure_key TEXT,
    procedure_name TEXT NOT NULL,
    report_as_of TEXT,
    period_start TEXT,
    period_end TEXT,
    data_source TEXT,
    cases_waiting_raw TEXT,
    cases_waiting INTEGER,
    p50_weeks REAL,
    p90_weeks REAL,
    scraped_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spec_profile_run ON specialist_profiles(run_id);
CREATE INDEX IF NOT EXISTS idx_spec_profile_spec ON specialist_profiles(specialist_id);
CREATE INDEX IF NOT EXISTS idx_spec_profile_proc ON specialist_profiles(procedure_key);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """Add a column to an existing table if it doesn't already exist."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Migrate pre-existing databases that were created before these columns existed.
    _add_column_if_missing(conn, "procedures", "definition", "TEXT")
    _add_column_if_missing(conn, "procedure_summaries", "data_source", "TEXT")
    _add_column_if_missing(conn, "facility_wait_times", "facility_id", "TEXT REFERENCES facilities(facility_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facility_id ON facility_wait_times(facility_id)"
    )
    conn.commit()
    return conn


def upsert_procedure(
    conn: sqlite3.Connection,
    proc: ProcedureRef,
    now: str,
    definition: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO procedures (procedure_key, procedure_id, name, adult_flag, definition, url_template, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(procedure_key) DO UPDATE SET
            name = excluded.name,
            adult_flag = excluded.adult_flag,
            definition = COALESCE(excluded.definition, procedures.definition),
            url_template = excluded.url_template,
            last_seen_at = excluded.last_seen_at
        """,
        (
            proc.procedure_key,
            proc.procedure_id,
            proc.name,
            proc.adult,
            definition,
            f"{BASE_URL}/WaitTimesResults.xhtml?rollupProcedure={{}}&procName={{}}&adult={proc.adult}",
            now,
            now,
        ),
    )


def upsert_specialist(
    conn: sqlite3.Connection, spec: SpecialistRef, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO specialists (specialist_id, first_name, last_name, full_name, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(specialist_id) DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            full_name = excluded.full_name,
            last_seen_at = excluded.last_seen_at
        """,
        (spec.specialist_id, spec.first_name, spec.last_name, spec.full_name, now, now),
    )


def save_run_start(conn: sqlite3.Connection, total: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, status, procedures_total) VALUES (?, 'running', ?)",
        (now, total),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def save_run_complete(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    ok: int,
    failed: int,
    notes: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE scrape_runs
        SET completed_at = ?, status = ?, procedures_ok = ?, procedures_failed = ?, notes = ?
        WHERE run_id = ?
        """,
        (now, status, ok, failed, notes, run_id),
    )
    conn.commit()


def save_procedure_page(
    conn: sqlite3.Connection, run_id: int, page: ProcedurePage
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if page.summary:
        conn.execute(
            """
            INSERT INTO procedure_summaries
            (run_id, procedure_key, report_as_of, period_start, period_end, data_source,
             cases_waiting_raw, cases_waiting, p50_weeks, p90_weeks, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                page.summary.procedure_key,
                page.summary.report_as_of,
                page.summary.period_start,
                page.summary.period_end,
                page.summary.data_source,
                page.summary.cases_waiting_raw,
                page.summary.cases_waiting,
                page.summary.p50_weeks,
                page.summary.p90_weeks,
                now,
            ),
        )
    for ha in page.health_authorities:
        conn.execute(
            """
            INSERT INTO health_authority_wait_times
            (run_id, procedure_key, health_authority,
             cases_waiting_raw, cases_waiting, p50_weeks, p90_weeks, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                page.procedure_key,
                ha.health_authority,
                ha.cases_waiting_raw,
                ha.cases_waiting,
                ha.p50_weeks,
                ha.p90_weeks,
                now,
            ),
        )
    for fac in page.facilities:
        if fac.facility_id:
            conn.execute(
                """
                INSERT INTO facilities (facility_id, facility_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(facility_id) DO UPDATE SET
                    facility_name = excluded.facility_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (fac.facility_id, fac.facility_name, now, now),
            )
        conn.execute(
            """
            INSERT INTO facility_wait_times
            (run_id, procedure_key, health_authority, facility_id, facility_name,
             cases_waiting_raw, cases_waiting, p50_weeks, p90_weeks, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                page.procedure_key,
                fac.health_authority,
                fac.facility_id,
                fac.facility_name,
                fac.cases_waiting_raw,
                fac.cases_waiting,
                fac.p50_weeks,
                fac.p90_weeks,
                now,
            ),
        )
    for spec in page.specialists:
        conn.execute(
            """
            INSERT INTO specialist_wait_times
            (run_id, procedure_key, health_authority, facility_name, specialist_id,
             specialist_name, cases_waiting_raw, cases_waiting, p50_weeks, p90_weeks, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                page.procedure_key,
                spec.health_authority,
                spec.facility_name,
                spec.specialist_id,
                spec.specialist_name,
                spec.cases_waiting_raw,
                spec.cases_waiting,
                spec.p50_weeks,
                spec.p90_weeks,
                now,
            ),
        )
    conn.commit()


# The Procedure Groupings page uses some names that differ slightly from the
# Procedures A-Z page. Map grouping-page names to the canonical A-Z names.
GROUP_NAME_ALIASES: dict[str, str] = {
    "Corneal Transplant": "Corneal Transplants",
    "Lens & Vitreous": "Lens & Vitreous (non-cataract) Surgery",
    "Bariatric Procedure": "Bariatric Surgery",
    "Gastrostomy / Jejunostomy": "Gastrostomy/Jejunostomy",
    "Rectal Procedure": "Rectal Surgery",
    "D & C and Related": "D&C and Related Surgery",
    "Tonsillectomy": "Tonsillectomy/Adenoidectomy",
    "Varicose Vein Ligation and Stripping": "Varicose Veins Ligation/Stripping",
}


def save_procedure_categories(
    conn: sqlite3.Connection,
    categories: dict[str, list[str]],
    procedure_name_to_keys: dict[str, list[str]],
) -> None:
    for category_name, group_names in categories.items():
        conn.execute(
            "INSERT INTO procedure_categories (category_name) VALUES (?) ON CONFLICT(category_name) DO NOTHING",
            (category_name,),
        )
        for group_name in group_names:
            canonical_name = GROUP_NAME_ALIASES.get(group_name, group_name)
            proc_keys = procedure_name_to_keys.get(canonical_name, [])
            if not proc_keys:
                log.warning("Category %s references unknown procedure group: %s", category_name, group_name)
                continue
            for proc_key in proc_keys:
                conn.execute(
                    """
                    INSERT INTO procedure_category_mappings (procedure_key, category_name)
                    VALUES (?, ?)
                    ON CONFLICT(procedure_key, category_name) DO NOTHING
                    """,
                    (proc_key, category_name),
                )
    conn.commit()
    log.info("Saved %d procedure categories", len(categories))


def save_specialist_profiles(
    conn: sqlite3.Connection, run_id: int, records: list[SpecialistProfileRecord]
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for rec in records:
        conn.execute(
            """
            INSERT INTO specialist_profiles
            (run_id, specialist_id, specialist_name, adult_flag, facility_name,
             procedure_key, procedure_name, report_as_of, period_start, period_end,
             data_source, cases_waiting_raw, cases_waiting, p50_weeks, p90_weeks, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                rec.specialist_id,
                rec.specialist_name,
                rec.adult,
                rec.facility_name,
                rec.procedure_key if rec.procedure_key != rec.procedure_name else None,
                rec.procedure_name,
                rec.report_as_of,
                rec.period_start,
                rec.period_end,
                rec.data_source,
                rec.cases_waiting_raw,
                rec.cases_waiting,
                rec.p50_weeks,
                rec.p90_weeks,
                now,
            ),
        )
    conn.commit()
    log.info("Saved %d specialist profile records", len(records))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scrape_all(
    db_path: str,
    limit: Optional[int],
    workers: int,
    delay: float,
    skip_az: bool = False,
    skip_specialist_profiles: bool = False,
    specialist_workers: int = 3,
    specialist_delay: float = 0.75,
) -> None:
    conn = init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    try:
        session = get_session()

        # Seed reference tables from A-Z listings.
        procedures = scrape_procedures(session)
        if limit:
            procedures = procedures[:limit]

        procedure_name_to_keys: dict[str, list[str]] = defaultdict(list)
        for p in procedures:
            procedure_name_to_keys[p.name].append(p.procedure_key)

        if not skip_az:
            specialists = scrape_specialists(session)
            for spec in specialists:
                upsert_specialist(conn, spec, now)
            log.info("Upserted %d specialists", len(specialists))
        else:
            specialists = []

        for proc in procedures:
            upsert_procedure(conn, proc, now)
        conn.commit()
        log.info("Upserted %d procedure variants", len(procedures))

        # Scrape procedure groupings/categories once up front.
        categories = scrape_procedure_groupings(session)
        save_procedure_categories(conn, categories, procedure_name_to_keys)

        # Scrape each procedure page.
        run_id = save_run_start(conn, len(procedures))
        log.info("Started scrape run %d", run_id)

        ok = 0
        failed = 0
        errors: list[str] = []

        def scrape_one(proc: ProcedureRef) -> tuple[ProcedureRef, ProcedurePage]:
            # Use a per-thread session to avoid connection contention.
            local_session = get_session()
            return proc, scrape_procedure_page(local_session, proc)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(scrape_one, p): p for p in procedures}
            for idx, future in enumerate(as_completed(futures), start=1):
                proc, page = future.result()
                if page.error:
                    failed += 1
                    msg = f"{proc.name} ({proc.procedure_key}): {page.error}"
                    errors.append(msg)
                    log.warning("[%d/%d] FAILED %s", idx, len(procedures), msg)
                else:
                    ok += 1
                    if page.definition:
                        upsert_procedure(conn, proc, now, page.definition)
                    save_procedure_page(conn, run_id, page)
                    log.info(
                        "[%d/%d] OK %s - %d HA, %d facilities, %d specialists",
                        idx,
                        len(procedures),
                        proc.procedure_key,
                        len(page.health_authorities),
                        len(page.facilities),
                        len(page.specialists),
                    )
                time.sleep(delay)

        # Scrape individual specialist profile pages for cross-reference data.
        if specialists and not skip_specialist_profiles:
            log.info("Starting specialist profile scrape for %d specialists", len(specialists))
            profile_records: list[SpecialistProfileRecord] = []

            def scrape_one_profile(spec: SpecialistRef) -> list[SpecialistProfileRecord]:
                local_session = get_session()
                return scrape_specialist_profile(local_session, spec, procedure_name_to_keys)

            with ThreadPoolExecutor(max_workers=specialist_workers) as executor:
                futures = {executor.submit(scrape_one_profile, s): s for s in specialists}
                for idx, future in enumerate(as_completed(futures), start=1):
                    spec = futures[future]
                    try:
                        records = future.result()
                        profile_records.extend(records)
                        log.info(
                            "[%d/%d] Specialist profile %s - %d records",
                            idx,
                            len(specialists),
                            spec.specialist_id,
                            len(records),
                        )
                    except Exception as exc:
                        log.warning(
                            "[%d/%d] Specialist profile %s failed: %s",
                            idx,
                            len(specialists),
                            spec.specialist_id,
                            exc,
                        )
                    time.sleep(specialist_delay)

            save_specialist_profiles(conn, run_id, profile_records)

        status = "completed" if failed == 0 else "completed_with_errors"
        notes = "; ".join(errors[:20])
        if len(errors) > 20:
            notes += f"; ...and {len(errors) - 20} more errors"
        save_run_complete(conn, run_id, status, ok, failed, notes)
        log.info(
            "Scrape run %d finished: %d OK, %d failed", run_id, ok, failed
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape BC Surgery Wait Times into a SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default="bc_wait_times.db",
        help="Path to SQLite database (default: bc_wait_times.db)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scrape all procedure variants (default behaviour if no limit is set)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of procedure variants to scrape (useful for testing)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of parallel workers (default: 3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.75,
        help="Seconds to sleep between completions (default: 0.75)",
    )
    parser.add_argument(
        "--skip-az",
        action="store_true",
        help="Skip scraping the A-Z specialist listing (faster if already seeded)",
    )
    parser.add_argument(
        "--skip-specialist-profiles",
        action="store_true",
        help="Skip scraping individual SpecialistProfile pages",
    )
    parser.add_argument(
        "--specialist-workers",
        type=int,
        default=3,
        help="Parallel workers for specialist profile scraping (default: 3)",
    )
    parser.add_argument(
        "--specialist-delay",
        type=float,
        default=0.75,
        help="Seconds to sleep between specialist profile completions (default: 0.75)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "WARNING", "ERROR", "INFO"],
        help="Logging level (default: INFO)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        scrape_all(
            db_path=args.db,
            limit=args.limit,
            workers=args.workers,
            delay=args.delay,
            skip_az=args.skip_az,
            skip_specialist_profiles=args.skip_specialist_profiles,
            specialist_workers=args.specialist_workers,
            specialist_delay=args.specialist_delay,
        )
    except Exception as exc:
        log.exception("Scraper failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
