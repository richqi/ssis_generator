# SSIS SQL Package Generator

A command-line tool that connects to a SQL Server database, inspects its user tables, and generates ready-to-run `.sql` ETL scripts that build a star schema under a `datawarehouse` schema — one dimension table and one fact table per source table.

## Requirements

- Python 3.10+
- [pyodbc](https://pypi.org/project/pyodbc/)
- ODBC Driver 18 for SQL Server ([download](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server))

```bash
pip install pyodbc
```

## Usage

```bash
python generate_ssis_packages.py \
    --server <SERVER> \
    --database <DATABASE> \
    (--trusted | --username <USER> --password <PASS>) \
    --package-count <N> \
    [--output-dir <DIR>] \
    [--include-views]
```

### Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--server` | Yes | — | SQL Server instance name or IP address |
| `--database` | Yes | — | Database to inspect |
| `--trusted` | Yes* | — | Use Windows Integrated Authentication |
| `--username` | Yes* | — | SQL login username |
| `--password` | — | — | SQL login password (required with `--username`) |
| `--package-count` | Yes | — | Number of `.sql` package files to generate |
| `--output-dir` | No | `ssis_sql_packages` | Folder to write output files into |
| `--include-views` | No | off | Also inspect views when building metadata |

\* `--trusted` and `--username` are mutually exclusive; one is required.

### Examples

**Windows auth, 5 packages:**
```bash
python generate_ssis_packages.py \
    --server localhost\SQLEXPRESS \
    --database AdventureWorks \
    --trusted \
    --package-count 5
```

**SQL auth, custom output folder:**
```bash
python generate_ssis_packages.py \
    --server 10.0.0.5 \
    --database SalesDB \
    --username etl_user \
    --password s3cr3t \
    --package-count 10 \
    --output-dir ./generated_sql
```

## Output

Each generated file is named `package_01.sql`, `package_02.sql`, … and contains a complete, self-contained SQL script structured as follows:

```sql
SET NOCOUNT ON;
GO

-- Guard: create datawarehouse schema if it doesn't exist
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'datawarehouse')
EXEC('CREATE SCHEMA [datawarehouse]');
GO

-- Drop and recreate dimension table
IF OBJECT_ID(N'[datawarehouse].[dim_orders]', N'U') IS NOT NULL
    DROP TABLE [datawarehouse].[dim_orders];
GO

CREATE TABLE [datawarehouse].[dim_orders] (
    [DimKey]     INT IDENTITY(1,1) NOT NULL,
    [SourceId]   INT NOT NULL,
    [LoadDate]   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    ,[CustomerName] NVARCHAR(100) NULL,
    ,[Region]       VARCHAR(50)   NULL,
    ,CONSTRAINT [PK_dim_orders] PRIMARY KEY CLUSTERED ([DimKey])
) ON [PRIMARY];
GO

-- Drop and recreate fact table (FK → dim)
...

-- Load dimension data
INSERT INTO [datawarehouse].[dim_orders] ...
GO

-- Load fact data
INSERT INTO [datawarehouse].[fact_orders] ...
GO
```

### Dimension / fact column selection

- **Dimension columns:** up to 2 columns whose data type is a string or integer type (`NVARCHAR`, `VARCHAR`, `CHAR`, `NCHAR`, `INT`, `BIGINT`, `SMALLINT`, `TINYINT`) and whose name does not end with `id`. If no qualifying columns exist the first column is used as a fallback.
- **Fact columns:** all remaining source columns not used as dimension attributes.

### ID column handling

| Source table has `ID` column? | Dim INSERT strategy |
|---|---|
| Yes | `SELECT DISTINCT [ID] AS [SourceId] … WHERE [ID] IS NOT NULL` |
| No | `SELECT DISTINCT ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS [SourceId]` + `CROSS JOIN` to dim for fact load |

## Running the tests

No database connection required — all tests exercise pure functions.

```bash
pip install pytest
pytest test_generate_ssis_packages.py -v
```

Expected result: **65 passed**.

## Caveats

- Scripts are generated in a **drop-and-recreate** pattern. Do not run against production without reviewing the output first.
- The tool uses `ODBC Driver 18 for SQL Server` with `Encrypt=no`. Adjust the connection string in `build_connection_string` if your environment requires encryption.
- `--include-views` collects view metadata but the current generator only uses user tables as fact sources.
- The `datawarehouse` schema and all generated tables are created in the **same database** as the source tables.
