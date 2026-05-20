# SSIS SQL Package Generator

A command-line tool that connects to a SQL Server database, inspects its user tables, and generates ETL scripts that build a star schema under a `datawarehouse` schema — one dimension table and one fact table per source table.

Output formats:
- **`.sql`** (default) — ready-to-run T-SQL scripts executed via SSMS, sqlcmd, or SSIS Execute Process Tasks
- **`.biml`** — Biml XML files that [BimlExpress](https://www.varigence.com/BimlExpress) or [BimlStudio](https://www.varigence.com/BimlStudio) compile directly into `.dtsx` SSIS packages

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
    [--output-format sql|biml] \
    [--dw-connection <OLE_DB_CONN_STR>] \
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
| `--package-count` | Yes | — | Number of package files to generate |
| `--output-dir` | No | `ssis_sql_packages` | Folder to write output files into |
| `--output-format` | No | `sql` | Output format: `sql` or `biml` |
| `--dw-connection` | No† | — | OLE DB connection string embedded in Biml output |
| `--include-views` | No | off | Also inspect views when building metadata |

\* `--trusted` and `--username` are mutually exclusive; one is required.  
† Required when `--output-format=biml`.

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

**Biml output (compiles to .dtsx via BimlExpress):**
```bash
python generate_ssis_packages.py \
    --server localhost\SQLEXPRESS \
    --database AdventureWorks \
    --trusted \
    --package-count 5 \
    --output-format biml \
    --dw-connection "Provider=SQLNCLI11;Server=localhost\SQLEXPRESS;Initial Catalog=AdventureWorks;Integrated Security=SSPI;"
```

## Output

### SQL output (`--output-format sql`, default)

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

### Biml output (`--output-format biml`)

Each generated file is named `package_01.biml`, `package_02.biml`, … and contains a Biml XML document that BimlExpress or BimlStudio compiles into a `.dtsx` SSIS package.

```xml
<?xml version="1.0" encoding="utf-8"?>
<Biml xmlns="http://schemas.varigence.com/biml.xsd">
  <Connections>
    <OleDbConnection Name="DW" ConnectionString="Provider=SQLNCLI11;..." />
  </Connections>
  <Packages>
    <Package Name="package_01" ConstraintMode="Linear">
      <Tasks>
        <ExecuteSQL Name="Create datawarehouse schema" ConnectionName="DW">
          <DirectInput>IF NOT EXISTS ... EXEC('CREATE SCHEMA [datawarehouse]');</DirectInput>
        </ExecuteSQL>
        <ExecuteSQL Name="Drop dim_orders" ConnectionName="DW">...</ExecuteSQL>
        <ExecuteSQL Name="Create dim_orders" ConnectionName="DW">...</ExecuteSQL>
        <ExecuteSQL Name="Drop fact_orders" ConnectionName="DW">...</ExecuteSQL>
        <ExecuteSQL Name="Create fact_orders" ConnectionName="DW">...</ExecuteSQL>
        <ExecuteSQL Name="Load dim_orders" ConnectionName="DW">...</ExecuteSQL>
        <ExecuteSQL Name="Load fact_orders" ConnectionName="DW">...</ExecuteSQL>
      </Tasks>
    </Package>
  </Packages>
</Biml>
```

`ConstraintMode="Linear"` wires the 7 tasks sequentially without manual Precedence Constraints. Each task holds a single SQL batch — no `GO` separators are needed.

**To compile into .dtsx packages:**
1. Install [BimlExpress](https://www.varigence.com/BimlExpress) (free Visual Studio extension).
2. Add the `.biml` files to an Integration Services project.
3. Select all `.biml` files → right-click → **Generate SSIS Packages**.

## Running the tests

No database connection required — all tests exercise pure functions.

```bash
pip install pytest
pytest test_generate_ssis_packages.py -v
```

Expected result: **90 passed**.

## Using the generated scripts in SSIS

Choose the path that best fits your toolchain:

| Path | Output format | Effort | Best for |
|---|---|---|---|
| [Biml → BimlExpress](#biml-output--bimlexpress-recommended) | `.biml` | Lowest | New packages; direct `.dtsx` compile |
| [Execute Process Task](#option-a--execute-process-task) | `.sql` | Low | Quick automation via sqlcmd |
| [Execute SQL Tasks](#option-b--execute-sql-task-split-on-go-boundaries) | `.sql` | Medium | SSIS-native logging & transactions |
| [Schema setup + Data Flow](#option-c--one-time-schema-setup-then-data-flow-tasks) | `.sql` | Higher | Incremental / production-grade loads |

---

### Biml output + BimlExpress (recommended)

If you have [BimlExpress](https://www.varigence.com/BimlExpress) (free) or BimlStudio installed, use `--output-format biml` to skip manual SSIS wiring entirely.

**Steps:**

1. Generate `.biml` files with `--output-format biml --dw-connection "<your OLE DB string>"`.
2. In Visual Studio, open (or create) an **Integration Services Project**.
3. Add the generated `.biml` files to the project.
4. Select all `.biml` files → right-click → **Generate SSIS Packages**.
5. BimlExpress creates one `.dtsx` per `.biml` file, each with 7 pre-wired **Execute SQL Tasks**.

> The Biml packages use `ConstraintMode="Linear"` — tasks run sequentially in the correct order with no manual Precedence Constraints needed. The OLE DB connection string you passed via `--dw-connection` is embedded as a connection manager named `DW`; rename it in the project if needed.

---

### Option A — Execute Process Task (recommended for full scripts)

This is the simplest integration. SSIS shells out to `sqlcmd.exe`, which handles `GO` natively.

**Prerequisites:** `sqlcmd` installed on the SSIS server (`sqlcmd` ships with SQL Server or the [ODBC command-line tools](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-utility)).

**Steps:**

1. Open your SSIS project in Visual Studio (SSDT).
2. On the **Control Flow** canvas, add an **Execute Process Task** for each `.sql` file.
3. Configure each task:

   | Property | Value |
   |---|---|
   | **Executable** | `C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\SQLCMD.EXE` *(adjust path as needed)* |
   | **Arguments** | `-S $(ServerName) -d $(DatabaseName) -i "$(PackageSqlPath)\package_01.sql" -b` |
   | **FailTaskIfReturnCodeIsNotSuccessValue** | `True` |

4. Add SSIS variables (`ServerName`, `DatabaseName`, `PackageSqlPath`) via **SSIS → Variables** and populate them at runtime or via package configuration.
5. Connect the tasks with **Precedence Constraints** (green arrows) to enforce execution order — typically schema setup first, then dimension loads, then fact loads.

```
[Execute package_01.sql] → [Execute package_02.sql] → … → [Execute package_N.sql]
```

---

### Option B — Execute SQL Task (split on GO boundaries)

Use this when you need full SSIS logging, transactions, or connection manager reuse and cannot call `sqlcmd`.

Each `GO` boundary in the generated script becomes a separate **Execute SQL Task**.

**Steps:**

1. In each generated `.sql` file, identify the `GO`-delimited batches. For example, `package_01.sql` produces these logical tasks:

   | Task | Statement |
   |---|---|
   | 1 | `IF NOT EXISTS … EXEC('CREATE SCHEMA …')` |
   | 2 | `IF OBJECT_ID … DROP TABLE [dim_…]` |
   | 3 | `CREATE TABLE [dim_…]` |
   | 4 | `IF OBJECT_ID … DROP TABLE [fact_…]` |
   | 5 | `CREATE TABLE [fact_…]` |
   | 6 | `INSERT INTO [dim_…] SELECT …` |
   | 7 | `INSERT INTO [fact_…] SELECT …` |

2. Add one **Execute SQL Task** per batch on the Control Flow canvas.
3. For each task, set:
   - **Connection** → your OLE DB or ADO.NET connection manager pointing to the target database.
   - **SQLStatement** → paste the batch text (without the surrounding `GO` lines).
4. Wire the tasks sequentially with Precedence Constraints.
5. Wrap all tasks for a single package file in a **Sequence Container** to keep the canvas organised.

---

### Option C — One-time schema setup, then Data Flow Tasks

If you only need the generated scripts to create the `datawarehouse` schema and tables, and want to build proper incremental Data Flow logic yourself:

1. **Run the scripts once** in SSMS (File → Open → `package_01.sql`, then execute) to create the dim/fact tables.
2. In your SSIS package, build **Data Flow Tasks** that:
   - Use an **OLE DB Source** pointing to the source table (e.g. `[AW].[Sales_Store]`).
   - Apply any lookups or transformations.
   - Use an **OLE DB Destination** pointing to the generated `[datawarehouse].[fact_…]` table.
3. Use a separate Execute SQL Task before the Data Flow to truncate/reload the dim table if needed.

This approach gives you incremental loads, error row handling, and full Data Flow logging that the raw SQL scripts cannot provide.

---

### Recommended SSIS project layout

**Biml workflow (`.biml` → compile → `.dtsx`):**
```
MyWarehouse.sln
└── MyWarehouse (Integration Services Project)
    ├── package_01.biml              ← generated Biml source
    ├── package_02.biml
    ├── …
    ├── package_01.dtsx              ← compiled by BimlExpress
    ├── package_02.dtsx
    └── …
```

**SQL workflow (`.sql` → Execute Process Tasks):**
```
MyWarehouse.sln
└── MyWarehouse (Integration Services Project)
    ├── Connection Managers
    │   └── DW_Conn.conmgr          ← OLE DB to target DB
    ├── Master.dtsx                  ← calls child packages in order
    ├── package_01.dtsx              ← Execute Process Task → sqlcmd package_01.sql
    ├── package_02.dtsx
    └── …
```

---

## Caveats

- Scripts are generated in a **drop-and-recreate** pattern. Do not run against production without reviewing the output first.
- The tool uses `ODBC Driver 18 for SQL Server` with `Encrypt=no`. Adjust the connection string in `build_connection_string` if your environment requires encryption.
- `--include-views` collects view metadata but the current generator only uses user tables as fact sources.
- The `datawarehouse` schema and all generated tables are created in the **same database** as the source tables.
