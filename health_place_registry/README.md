# BC Health Place Registry

Shared place-name/address registry for BC health datasets.

This registry exists so dataset scrapers can keep their raw source tables while
future health-data loaders can cross-list names against one reviewed address and
alias inventory. It is seeded from:

- `bc_wait_times/facility_locations.csv` for reviewed facility addresses.
- `bc_msp_blue_book/bc_msp_blue_book.db` for non-practitioner MSP payee names.

Workflow:

```bash
cd vendor/bcdatamapper/data-sources/healthdata/health_place_registry

# Merge known health place names into health_place_registry.csv.
python build_health_place_registry.py --sync

# Apply reviewed address/classification CSVs.
python build_health_place_registry.py --apply-enrichment enrichment_hospitals_health_authorities.csv

# Fill coordinates for reviewed addresses using the BC Address Geocoder.
python build_health_place_registry.py --geocode

# Fill coordinates for child site rows used by rollups or multi-site payees.
python build_health_place_registry.py --geocode-sites

# Export rows with coordinates as map-ready GeoJSON.
python build_health_place_registry.py --export-geojson --export-sites-geojson
```

Important fields:

- `canonical_name`: preferred display/join name.
- `place_type`: `hospital`, `clinic`, `diagnostic_facility`, `health_authority`,
  `organization`, or `facility`.
- `place_status`: `physical_place`, `non_place_program`,
  `corporate_entity_no_public_clinic`, or `ambiguous`.
- `source_datasets`: pipe-delimited list of datasets that mention this place.
- `aliases`: pipe-delimited names that should match this canonical row.
- `verification_status`: plain-text review status; use the same conventions as
  `bc_wait_times/facility_locations.csv`.

## Child Sites

`health_place_sites.csv` stores one-to-many mappings for parent labels that
represent more than one physical location, such as `Greater Victoria Hospitals`
or multi-site diagnostic payees. Keep the parent in `health_place_registry.csv`
with `place_status` like `rollup_multi_site` or `multi_site`, then list each
actual physical site in `health_place_sites.csv`.

`health_place_sites.geojson` exports the mapped child sites.

MSP Blue Book `organization` rows are mixed: some are real places, while others
are committees, programs, corporations, or payment mechanisms. Keep them as
`needs_review` until an address and place type have been checked.
