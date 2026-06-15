# healthdata

A collection of health-data scrapers. **Each data source lives in its own folder**,
containing the scraper that builds it and the SQLite database it produces.

```
healthdata/
├── requirements.txt          # shared Python deps (requests, beautifulsoup4)
├── bc_wait_times/            # BC surgery wait times — swt.hlth.gov.bc.ca
│   ├── scrape_bc_wait_times.py
│   └── bc_wait_times.db
└── msp_codes/                # MSP billing codes — dr-bill.ca
    ├── scrape_msp_codes.py
    └── msp_codes.db
```

## Setup

```bash
pip install -r requirements.txt
```

## Running a scraper

Each scraper writes its database into the current directory by default, so run it
from inside its own folder:

```bash
cd bc_wait_times && python scrape_bc_wait_times.py
cd msp_codes     && python scrape_msp_codes.py
```

## Adding a new data source

Create a new folder named after the dataset and drop the scraper inside it:

```
healthdata/
└── <dataset_name>/
    ├── scrape_<dataset_name>.py
    └── <dataset_name>.db
```
