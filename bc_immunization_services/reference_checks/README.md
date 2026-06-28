# Reference Checks

Use this folder to compare the official BC immunization-services export against
manual reference observations, including Vaccines411 spot checks.

Vaccines411 should not be bulk scraped. For this workflow, use it only as a
manual reference: run a postal-code/category search in the public site, record
the visible clinic details in `reference_places.csv`, then compare those rows
against the BC Data Catalogue export.

Workflow:

```bash
cd vendor/bcdatamapper/data-sources/healthdata/bc_immunization_services
python compare_reference_places.py
```

Inputs:

- `output/bc-immunization-services.csv`: generated official BC source export.
- `reference_checks/reference_places.csv`: manually entered observations from
  Vaccines411 or other reference sources.

Output:

- `reference_checks/missing_from_bc_review.csv`: one row per reference place,
  with a match status and the best BC-source candidate.

Review statuses:

- `matched`: likely already represented in the BC source.
- `possible_match`: similar enough to review manually.
- `missing_from_bc_source`: no reasonable candidate found in the BC source.

The comparison is intentionally conservative. It uses normalized names,
addresses, postal codes, and cities to find likely matches, but it does not
promote a reference observation into the official source automatically.
