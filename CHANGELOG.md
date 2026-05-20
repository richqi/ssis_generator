# Changelog

All notable changes to this project will be documented here.

## [Unreleased]

## [0.1.0] — 2026-05-19

### Added
- `generate_ssis_packages.py`: CLI tool that connects to SQL Server, inspects user tables, and emits `.sql` ETL package scripts building a star schema under the `datawarehouse` schema.
- `test_generate_ssis_packages.py`: 65-test suite covering all pure functions — no database connection required. Tests use `pytest` and include cases for type mapping, column selection logic, table name sanitisation, SQL structure, and connection string building.

### Fixed
- `generate_package_sql`: off-by-one `IndexError` in the no-`ID` branch. The code was assigning to `insert_dim[2/3/4]` when `insert_dim` only has indices `0–3`; corrected to `insert_dim[1/2/3]` so `ROW_NUMBER`-based dim inserts are generated without error.
