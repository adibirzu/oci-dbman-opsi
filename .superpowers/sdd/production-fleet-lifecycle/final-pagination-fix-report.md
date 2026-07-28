# Final database pagination fix report

## Scope

- Base commit: `ba7a2d1`
- Owned code: `src/dbman_opsi/_oci_database.py`
- Owned tests: `tests/test_oci_cli.py`

## OCI CLI contract

Installed OCI CLI `3.87.0` was checked locally. `oci db database list --all --help`
and `oci db database list --page <token> --help` both fail parser validation, so
neither unsupported flag is emitted. The installed `oci raw-request` command is
parser-supported and returns JSON response `data`, `headers`, and `status`.

The implementation calls the Database REST list endpoint through `raw-request`,
passes only `compartmentId` plus the correct parent selector (`dbSystemId` or
`vmClusterId`), reads the `opc-next-page` header case-insensitively, and feeds
the returned token into the next `page` query parameter. It preserves the
first-seen order and de-duplicates only repeated non-empty database OCIDs.
Repeated page tokens fail rather than looping indefinitely. No `dbHomeId`,
`--all`, `--page`, or `--page-token` CLI arguments are used.

## Verification

- Red test before implementation:
  `UV_CACHE_DIR=.cache/uv uv run --extra dev pytest -q tests/test_oci_cli.py -k 'database_db_system_route_follows_all_pages or database_vm_cluster_route_follows_all_pages'`
  failed as expected because the second-page database was absent on each route.
- Green focused tests:
  `UV_CACHE_DIR=.cache/uv uv run --extra dev pytest -q --no-cov tests/test_oci_cli.py -k 'database_db_system_route_follows_all_pages or database_vm_cluster_route_follows_all_pages'`
  — `2 passed`.
- Relevant database/discovery suites:
  `UV_CACHE_DIR=.cache/uv uv run --extra dev pytest -q --no-cov tests/test_oci_cli.py tests/test_fleet_discovery.py`
  — `40 passed`.
- Full suite:
  `UV_CACHE_DIR=.cache/uv uv run --extra dev pytest -q --maxfail=1`
  — `581 passed` in `43.04s`; coverage `88.54%` (required `80%`).
