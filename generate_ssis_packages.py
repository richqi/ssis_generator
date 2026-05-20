"""Generate SQL-based SSIS-style ETL package scripts for SQL Server.

This script connects to a SQL Server database, inspects user tables, views,
functions, and stored procedures, and generates a set of SQL package files
that create a simple star schema under the `datawarehouse` schema.

Usage:
    python generate_ssis_packages.py \
        --server SERVER \
        --database DBNAME \
        [--username USERNAME --password PASSWORD | --trusted] \
        --package-count N \
        [--output-dir output_sql_packages]

Requires:
    pyodbc
"""

import argparse
import os
import pathlib
import re
import sys

try:
    import pyodbc
except ImportError:
    raise SystemExit(
        "pyodbc is required. Install with: pip install pyodbc"
    )

SYSTEM_SCHEMAS = {
    "sys",
    "INFORMATION_SCHEMA",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "guest",
    "dbo",
}

def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate SQL ETL package scripts for SQL Server data warehouse building."
    )
    parser.add_argument("--server", required=True, help="SQL Server instance name or address")
    parser.add_argument("--database", required=True, help="Target database name")
    auth_group = parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("--trusted", action="store_true", help="Use Windows Integrated Authentication")
    auth_group.add_argument("--username", help="SQL login user name")
    parser.add_argument("--password", help="SQL login password")
    parser.add_argument("--package-count", required=True, type=int, help="Number of SQL package scripts to generate")
    parser.add_argument("--output-dir", default="ssis_sql_packages", help="Output folder for generated .sql packages")
    parser.add_argument("--include-views", action="store_true", help="Also analyze views when building package metadata")
    return parser.parse_args()


def build_connection_string(server: str, database: str, trusted: bool, username: str | None, password: str | None) -> str:
    if trusted:
        return f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;Encrypt=no"
    if not username or password is None:
        raise ValueError("Both --username and --password are required for SQL authentication.")
    return f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};UID={username};PWD={password};Encrypt=no"


def _fetchall_as_dicts(cursor):
    cols = [col[0] for col in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def query_metadata(cursor):
    metadata = {
        "tables": [],
        "views": [],
        "functions": [],
        "procedures": [],
    }

    table_sql = """
        SELECT s.name AS schema_name,
               t.name AS object_name,
               t.object_id
          FROM sys.tables t
          JOIN sys.schemas s ON t.schema_id = s.schema_id
         WHERE s.name NOT IN ({})
    """.format(
        ",".join("?" for _ in SYSTEM_SCHEMAS)
    )
    cursor.execute(table_sql, tuple(SYSTEM_SCHEMAS))
    metadata["tables"] = _fetchall_as_dicts(cursor)

    view_sql = """
        SELECT s.name AS schema_name,
               v.name AS object_name,
               v.object_id
          FROM sys.views v
          JOIN sys.schemas s ON v.schema_id = s.schema_id
         WHERE s.name NOT IN ({})
    """.format(
        ",".join("?" for _ in SYSTEM_SCHEMAS)
    )
    cursor.execute(view_sql, tuple(SYSTEM_SCHEMAS))
    metadata["views"] = _fetchall_as_dicts(cursor)

    scalar_fn_sql = """
        SELECT s.name AS schema_name,
               o.name AS object_name,
               o.object_id
          FROM sys.objects o
          JOIN sys.schemas s ON o.schema_id = s.schema_id
         WHERE o.type IN ('FN', 'TF', 'IF')
           AND s.name NOT IN ({})
    """.format(
        ",".join("?" for _ in SYSTEM_SCHEMAS)
    )
    cursor.execute(scalar_fn_sql, tuple(SYSTEM_SCHEMAS))
    metadata["functions"] = _fetchall_as_dicts(cursor)

    proc_sql = """
        SELECT s.name AS schema_name,
               o.name AS object_name,
               o.object_id
          FROM sys.objects o
          JOIN sys.schemas s ON o.schema_id = s.schema_id
         WHERE o.type = 'P'
           AND s.name NOT IN ({})
    """.format(
        ",".join("?" for _ in SYSTEM_SCHEMAS)
    )
    cursor.execute(proc_sql, tuple(SYSTEM_SCHEMAS))
    metadata["procedures"] = _fetchall_as_dicts(cursor)

    return metadata


def get_table_columns(cursor, schema_name, table_name):
    sql = """
        SELECT c.name AS column_name,
               t.name AS data_type,
               c.max_length,
               c.precision,
               c.scale,
               c.is_nullable,
               c.is_identity
          FROM sys.columns c
          JOIN sys.types t ON c.user_type_id = t.user_type_id
         WHERE c.object_id = OBJECT_ID(?)
         ORDER BY c.column_id
    """
    cursor.execute(sql, f"[{schema_name}].[{table_name}]")
    return _fetchall_as_dicts(cursor)


def simplify_type(column):
    data_type = column["data_type"].upper()
    if data_type in ("NVARCHAR", "VARCHAR", "CHAR", "NCHAR"):
        length = column["max_length"]
        if length < 0:
            return "NVARCHAR(MAX)"
        if data_type.startswith("N"):
            length = length // 2
        return f"{data_type}({length})"
    if data_type in ("DECIMAL", "NUMERIC"):
        return f"{data_type}({column['precision']},{column['scale']})"
    if data_type in ("DATETIME2", "DATETIMEOFFSET", "TIME"):
        return f"{data_type}({column['scale']})" if column["scale"] is not None else data_type
    return data_type


def render_column_def(column):
    name = column["column_name"]
    col_type = simplify_type(column)
    nullable = "NULL" if column["is_nullable"] else "NOT NULL"
    return f"    [{name}] {col_type} {nullable}"


def choose_fact_source(tables, package_index):
    if not tables:
        return None
    return tables[package_index % len(tables)]


def choose_dimension_columns(columns):
    dims = []
    for col in columns:
        dtype = col["data_type"].lower()
        if dtype in ("nvarchar", "varchar", "char", "nchar", "int", "bigint", "smallint", "tinyint"):
            if col["column_name"].lower().endswith("id"):
                continue
            dims.append(col)
        if len(dims) >= 2:
            break
    if not dims and columns:
        dims = columns[:1]
    return dims


def generate_package_sql(package_index, source_table, source_columns):
    schema_name = "datawarehouse"
    package_id = package_index + 1
    table_base = re.sub(r"[^0-9A-Za-z_]+", "_", source_table["object_name"]).lower()
    dim_name = f"dim_{table_base}"
    fact_name = f"fact_{table_base}"
    dims = choose_dimension_columns(source_columns)

    create_schema = f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema_name}')\nEXEC('CREATE SCHEMA [{schema_name}]');"
    create_dim = [
        f"CREATE TABLE [{schema_name}].[{dim_name}] (",
        "    [DimKey] INT IDENTITY(1,1) NOT NULL",
        "    [SourceId] INT NOT NULL",
        "    [LoadDate] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()",
    ]
    for col in dims:
        create_dim.append(f"    ,[{col['column_name']}] {simplify_type(col)} NULL")
    create_dim.append(f"    ,CONSTRAINT [PK_{dim_name}] PRIMARY KEY CLUSTERED ([DimKey])")
    create_dim.append(") ON [PRIMARY];")

    create_fact = [
        f"CREATE TABLE [{schema_name}].[{fact_name}] (",
        "    [FactKey] BIGINT IDENTITY(1,1) NOT NULL",
        "    [DimKey] INT NOT NULL",
        "    [LoadDate] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()",
    ]
    for col in source_columns:
        if col["column_name"] in [d["column_name"] for d in dims]:
            continue
        create_fact.append(f"    ,[{col['column_name']}] {simplify_type(col)} NULL")
    create_fact.append(f"    ,CONSTRAINT [PK_{fact_name}] PRIMARY KEY CLUSTERED ([FactKey])")
    create_fact.append(f"    ,CONSTRAINT [FK_{fact_name}_{dim_name}] FOREIGN KEY ([DimKey]) REFERENCES [{schema_name}].[{dim_name}]([DimKey])")
    create_fact.append(") ON [PRIMARY];")

    insert_dim = [f"INSERT INTO [{schema_name}].[{dim_name}] ([SourceId], [LoadDate]"
                  + ("" if not dims else ", " + ", ".join(f"[{c['column_name']}]" for c in dims))
                  + ")",
                  f"SELECT DISTINCT [ID] AS [SourceId], SYSUTCDATETIME() AS [LoadDate]"
                  + ("" if not dims else ", " + ", ".join(f"[{c['column_name']}]" for c in dims)),
                  f"FROM [{source_table['schema_name']}].[{source_table['object_name']}]",
                  "WHERE [ID] IS NOT NULL;"]

    insert_fact = [f"INSERT INTO [{schema_name}].[{fact_name}] ([DimKey], [LoadDate]"
                   + ("" if not source_columns else ", " + ", ".join(f"[{c['column_name']}]" for c in source_columns if c["column_name"] not in [d["column_name"] for d in dims]))
                   + ")",
                   "SELECT d.[DimKey], SYSUTCDATETIME() AS [LoadDate]",
                   ", ".join(f"s.[{c['column_name']}]" for c in source_columns if c["column_name"] not in [d["column_name"] for d in dims]),
                   f"FROM [{source_table['schema_name']}].[{source_table['object_name']}] AS s",
                   f"JOIN [{schema_name}].[{dim_name}] AS d ON d.[SourceId] = s.[ID]"]

    if not any(col["column_name"].upper() == "ID" for col in source_columns):
        insert_dim[1] = f"SELECT DISTINCT ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS [SourceId], SYSUTCDATETIME() AS [LoadDate]"
        insert_dim[2] = f"FROM [{source_table['schema_name']}].[{source_table['object_name']}]"
        insert_dim[3] = "WHERE 1=1;"
        insert_fact[0] = f"INSERT INTO [{schema_name}].[{fact_name}] ([DimKey], [LoadDate]"
        insert_fact[1] = "SELECT d.[DimKey], SYSUTCDATETIME() AS [LoadDate]"
        insert_fact[2] = ", ".join(f"s.[{c['column_name']}]" for c in source_columns if c["column_name"] not in [d["column_name"] for d in dims])
        insert_fact[3] = f"FROM [{source_table['schema_name']}].[{source_table['object_name']}] AS s"
        insert_fact[4] = f"CROSS JOIN (SELECT TOP 1 [DimKey] FROM [{schema_name}].[{dim_name}] ORDER BY [DimKey]) AS d"

    script_lines = [
        "SET NOCOUNT ON;",
        "GO",
        create_schema,
        "GO",
        "IF OBJECT_ID(N'[{0}].[{1}]', N'U') IS NOT NULL DROP TABLE [{0}].[{1}];".format(schema_name, dim_name),
        "GO",
        *create_dim,
        "GO",
        "IF OBJECT_ID(N'[{0}].[{1}]', N'U') IS NOT NULL DROP TABLE [{0}].[{1}];".format(schema_name, fact_name),
        "GO",
        *create_fact,
        "GO",
        "-- Load dimension data",
        *insert_dim,
        "GO",
        "-- Load fact data",
        *insert_fact,
        "GO",
    ]

    return "\n".join(script_lines)


def write_package(output_dir, index, sql_text):
    filename = os.path.join(output_dir, f"package_{index + 1:02d}.sql")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(sql_text)
    return filename


def main():
    args = parse_args()
    if args.username and args.password is None:
        raise SystemExit("Error: --password is required when using --username.")
    conn_str = build_connection_string(
        args.server,
        args.database,
        args.trusted,
        args.username,
        args.password,
    )
    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with pyodbc.connect(conn_str, autocommit=True) as conn:
        cursor = conn.cursor()
        metadata = query_metadata(cursor)

        print(f"Found {len(metadata['tables'])} user tables, {len(metadata['views'])} views, {len(metadata['functions'])} functions, {len(metadata['procedures'])} stored procedures.")
        if not metadata["tables"]:
            raise SystemExit("No user tables found in the target database to generate ETL packages.")

        package_paths = []
        for package_index in range(args.package_count):
            source_table = choose_fact_source(metadata["tables"], package_index)
            columns = get_table_columns(cursor, source_table["schema_name"], source_table["object_name"])
            if not columns:
                print(f"Skipping {source_table['schema_name']}.{source_table['object_name']} because it has no columns.")
                continue

            package_sql = generate_package_sql(package_index, source_table, columns)
            package_path = write_package(output_dir, package_index, package_sql)
            package_paths.append(package_path)
            print(f"Generated: {package_path}")

        print("\nGeneration complete.")
        print(f"Scripts written to: {output_dir.resolve()}")
        print("Recommended next step: open the .sql files and validate object names before executing on production.")

if __name__ == "__main__":
    main()
