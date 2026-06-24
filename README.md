# healthdata

A collection of health-data scrapers. **Each data source lives in its own folder**,
containing the scraper that builds it and the SQLite database it produces.

```
healthdata/
├── health_place_registry/        # Shared health place names, addresses, aliases
│   ├── build_health_place_registry.py
│   ├── health_place_registry.csv
│   └── health_place_registry.geojson
├── requirements.txt              # shared Python deps (requests, beautifulsoup4, pdfplumber)
├── bc_wait_times/                # BC surgery wait times — swt.hlth.gov.bc.ca           [CBA B2]
│   ├── scrape_bc_wait_times.py
│   └── bc_wait_times.db
├── msp_codes/                    # MSP billing codes — dr-bill.ca                       [CBA B1]
│   ├── scrape_msp_codes.py
│   └── msp_codes.db
├── bc_msp_blue_book/             # MSP practitioner payments — BC Data Catalogue        [CBA B1]
│   ├── load_bc_msp_blue_book.py
│   └── bc_msp_blue_book.db
├── statcan_education_earnings/   # Graduate income by credential — StatCan 37-10-0115   [CBA B4]
│   ├── scrape_statcan_education_earnings.py
│   └── education_earnings.db
├── unbc_sofi/                    # UNBC employee remuneration — SOFI PDF             [CBA B19-B21,B5]
│   ├── scrape_unbc_sofi.py
│   └── unbc_sofi.db
├── bc_utility_tariffs/           # Electricity/gas/water rates — BC Hydro/FortisBC/PG   [CBA opex]
│   ├── scrape_bc_utility_tariffs.py
│   └── bc_utility_tariffs.db
├── bc_permit_fees/               # PG permit & regulatory fees — bylaw PDFs             [CBA capex]
│   ├── scrape_bc_permit_fees.py
│   └── bc_permit_fees.db
├── bc_patient_travel/            # NH Connections routes + travel cost benchmarks       [CBA B3]
│   ├── scrape_bc_patient_travel.py
│   └── bc_patient_travel.db
├── statcan_odhf/                 # Open Database of Healthcare Facilities — StatCan
│   ├── scrape_statcan_odhf.py
│   ├── statcan_odhf.db
│   └── output/
├── statcan_cba/                  # BC wages/retirement/labour force/migration — StatCan [CBA B2,B5,B12,B13]
│   ├── scrape_statcan_cba.py
│   └── statcan_cba.db
└── cihi_cshs/                    # Cost of a Standard Hospital Stay — CIHI (manual export) [CBA B1]
    ├── load_cihi_cshs.py
    ├── source/                   # drop CIHI .xlsx exports here, then run the loader
    └── cihi_cshs.db
```

These all feed the NHHR cost-benefit analysis public-data needs (`[CBA ...]` tags map
to benefit/cost lines). `statcan_education_earnings` pulls fresh from the StatCan WDS
API; `unbc_sofi`, `bc_utility_tariffs` and `bc_permit_fees` download + parse PDFs
(needs `pdfplumber`).

## PGMaps exports

PGMaps serves a small set of health snapshots from `/data/*.json`. Keep those
deploy-ready exports beside the source that owns them:

- `bc_wait_times/output/bc-wait-specialists.json` is the surgery wait-times app
  export derived from `bc_wait_times.db` and reviewed facility locations.
- `erstat/output/erstat-hospitals.json` is the ERStat hospital wait snapshot.
- `statcan_odhf/output/statcan-odhf-bc.{csv,geojson}` is the BC subset of
  StatCan's Open Database of Healthcare Facilities.
- `health_place_registry/health_place_registry.{csv,geojson}` merges reviewed
  registry entries with MSP, wait-times, and StatCan ODHF source membership.

## Setup

```bash
pip install -r requirements.txt
```

## Running a scraper

Each scraper writes its database into the current directory by default, so run it
from inside its own folder:

```bash
cd bc_wait_times              && python scrape_bc_wait_times.py
cd msp_codes                  && python scrape_msp_codes.py
cd bc_msp_blue_book           && python load_bc_msp_blue_book.py
cd health_place_registry      && python build_health_place_registry.py --sync --export-geojson --export-sites-geojson
cd statcan_education_earnings && python scrape_statcan_education_earnings.py
cd unbc_sofi                  && python scrape_unbc_sofi.py
cd bc_utility_tariffs         && python scrape_bc_utility_tariffs.py
cd bc_permit_fees             && python scrape_bc_permit_fees.py
cd bc_patient_travel          && python scrape_bc_patient_travel.py
cd statcan_odhf               && python scrape_statcan_odhf.py
cd statcan_cba                && python scrape_statcan_cba.py
cd cihi_cshs                  && python load_cihi_cshs.py   # parses CIHI exports in source/
```

`cihi_cshs` is a **loader, not a scraper**: CIHI publishes CSHS through an
interactive tool, so download the BC / Canada exports from cihi.ca into
`cihi_cshs/source/` and re-run the loader to refresh.

## Adding a new data source

Create a new folder named after the dataset and drop the scraper inside it:

```
healthdata/
└── <dataset_name>/
    ├── scrape_<dataset_name>.py
    └── <dataset_name>.db
```
