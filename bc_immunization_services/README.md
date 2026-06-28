# BC Immunization Services

`scrape_bc_immunization_services.py` mirrors the BC Data Catalogue
`Immunization Services in BC` dataset into `bc_immunization_services.db` and
writes GeoJSON/CSV exports under `output/`.

This is the preferred source for BC vaccination-service locations. It is
published by HealthLinkBC / BC Ministry of Health, licensed under the Open
Government Licence - British Columbia, includes coordinates, and is designed as
bulk public data. The catalogue metadata says HealthLinkBC reviews records for
accuracy and completeness at least every two years, and the resource update cycle
is monthly.

Workflow:

```bash
cd vendor/bcdatamapper/data-sources/healthdata/bc_immunization_services
python scrape_bc_immunization_services.py
```

Outputs:

- `bc_immunization_services.db`: raw catalogue rows plus a `dataset_metadata`
  table recording the catalogue/API source.
- `output/bc-immunization-services.csv`: normalized tabular extract.
- `output/bc-immunization-services.geojson`: map-ready point features.

Source:

- Dataset page: https://catalogue.data.gov.bc.ca/dataset/immunization-services-in-bc
- CKAN API: https://catalogue.data.gov.bc.ca/api/3/action/package_show?id=f49ebe46-6faf-4b5d-a21e-c19db5ec69d7
- WFS layer: `WHSE_IMAGERY_AND_BASE_MAPS.GSR_IMMUNIZATION_SERVICES_SV`

## Vaccines411

Vaccines411 is useful as a consumer-facing clinic finder, but it is not a good
bulk source for this project. Its public workflow is postal-code/category search,
not a downloadable all-locations directory, and its terms prohibit scraping,
data-mining, extraction, or collection of site content. Keep Vaccines411 as a
manual discovery/reference link only unless they provide explicit permission or a
licensed data feed.

To use Vaccines411 as a reference check, manually enter observed search results
in `reference_checks/reference_places.csv`, then run:

```bash
python compare_reference_places.py
```

The review output is written to
`reference_checks/missing_from_bc_review.csv` with match status and the closest
BC-source candidate.
