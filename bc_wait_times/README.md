# BC Surgery Wait Times

`scrape_bc_wait_times.py` mirrors the BC Surgery Wait Times source into
`bc_wait_times.db`. The upstream source provides facility names in the wait-time
tables, but it does not provide civic addresses or map coordinates.

## Facility Locations

Facility addresses and coordinates are maintained outside the scraped database in
`facility_locations.csv`. This keeps reviewed location metadata separate from the
source pull and makes missing or ambiguous locations explicit.

Workflow:

```bash
cd vendor/bcdatamapper/data-sources/healthdata/bc_wait_times

# Add any new facility names from the SQLite database to facility_locations.csv.
python build_facility_locations.py --sync

# After adding reviewed addresses, optionally fill coordinates with the
# BC Address Geocoder. Review geocoded_needs_review rows before publishing.
python build_facility_locations.py --geocode

# Export mapped facilities joined to latest wait-time specialist/procedure counts.
python build_facility_locations.py --export-geojson
```

Join key:

- `facility_name`
- `health_authority`

Use both fields when joining because `facility_name` alone is not guaranteed to be
a stable identifier across source tables or future source changes.

`verification_status` values are intentionally plain text. Suggested values:

- `needs_review`: source facility exists, address/coordinates not yet verified.
- `verified`: address and coordinates were checked against an authoritative source.
- `geocoded_needs_review`: coordinates came from the BC Address Geocoder and need
  human review.
- `geocode_failed`: a reviewed address did not return a usable geocoder match.

Some source facility labels are roll-ups rather than a single concrete civic
location, such as `Greater Victoria Hospitals`, and should be handled deliberately
before rendering on a map.
