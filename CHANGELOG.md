# Changelog

All notable changes to this project will be documented here.

## [Unreleased]

## [0.2.0] — 2026-05-19

### Added
- **Biml output format** (`--output-format biml`): generates `.biml` XML files that BimlExpress or BimlStudio compile directly into `.dtsx` SSIS packages, eliminating manual Execute SQL Task wiring.
  - Each `.biml` file contains a `<Package ConstraintMode="Linear">` with 7 `<ExecuteSQL>` tasks (create schema, drop/create dim, drop/create fact, load dim, load fact).
  - New `--dw-connection` argument embeds the OLE DB connection string as a `DW` connection manager in the Biml output.
  - Default format remains `sql`; existing workflows are unaffected.
- `_build_sql_batches()`: internal function that produces an ordered `(task_name, sql)` list shared by both the SQL and Biml renderers, removing duplicated generation logic.
- 25 new tests covering `_build_sql_batches`, `generate_package_biml` (with and without ID column), XML validity, namespace handling, task ordering, and SQL content round-tripping. Test suite grows from 65 to **90 tests**.
- README: Biml usage example, Biml output section with annotated XML, updated Arguments table, updated SSIS integration guide with Biml as the recommended path.

### Fixed
- **`CREATE TABLE` syntax**: `[SourceId]`, `[DimKey]`, and `[LoadDate]` columns in generated dim and fact tables were missing leading commas, causing immediate parse failures in SQL Server.
- **Fact `INSERT` column list**: the no-`ID` branch reset `insert_fact[0]` to just `([DimKey], [LoadDate]` — stripping all other column names and the closing `)`. Column count mismatch caused every fact load to fail.
- **Fact `SELECT` missing comma**: both the ID and no-`ID` branches omitted the comma between `SYSUTCDATETIME() AS [LoadDate]` and the source columns, producing invalid SQL.
- **Dim `SELECT` missing source columns**: the no-`ID` branch dropped the actual dimension attribute columns from the `SELECT`, leaving the `INSERT` column list and `SELECT` column count mismatched.

## [0.1.0] — 2026-05-19

### Added
- `generate_ssis_packages.py`: CLI tool that connects to SQL Server, inspects user tables, and emits `.sql` ETL package scripts building a star schema under the `datawarehouse` schema.
- `test_generate_ssis_packages.py`: 65-test suite covering all pure functions — no database connection required. Tests use `pytest` and include cases for type mapping, column selection logic, table name sanitisation, SQL structure, and connection string building.

### Fixed
- `generate_package_sql`: off-by-one `IndexError` in the no-`ID` branch. The code was assigning to `insert_dim[2/3/4]` when `insert_dim` only has indices `0–3`; corrected to `insert_dim[1/2/3]` so `ROW_NUMBER`-based dim inserts are generated without error.
