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
import xml.etree.ElementTree as ET

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
    parser.add_argument("--output-format", choices=["sql", "biml"], default="sql",
                        help="Output format: sql (default) or biml")
    parser.add_argument("--dw-connection",
                        help="OLE DB connection string embedded in Biml output (required when --output-format=biml)")
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


def _build_sql_batches(package_index, source_table, source_columns):
    """Return ordered list of (task_name, sql_text) pairs for one package.

    Each pair maps to one GO-separated batch in SQL output or one
    ExecuteSQL task in Biml output.
    """
    schema_name = "datawarehouse"
    table_base = re.sub(r"[^0-9A-Za-z_]+", "_", source_table["object_name"]).lower()
    dim_name = f"dim_{table_base}"
    fact_name = f"fact_{table_base}"
    dims = choose_dimension_columns(source_columns)

    create_schema = (
        f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema_name}')\n"
        f"EXEC('CREATE SCHEMA [{schema_name}]');"
    )

    create_dim = [
        f"CREATE TABLE [{schema_name}].[{dim_name}] (",
        "    [DimKey] INT IDENTITY(1,1) NOT NULL",
        "    ,[SourceId] INT NOT NULL",
        "    ,[LoadDate] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()",
    ]
    for col in dims:
        create_dim.append(f"    ,[{col['column_name']}] {simplify_type(col)} NULL")
    create_dim.append(f"    ,CONSTRAINT [PK_{dim_name}] PRIMARY KEY CLUSTERED ([DimKey])")
    create_dim.append(") ON [PRIMARY];")

    create_fact = [
        f"CREATE TABLE [{schema_name}].[{fact_name}] (",
        "    [FactKey] BIGINT IDENTITY(1,1) NOT NULL",
        "    ,[DimKey] INT NOT NULL",
        "    ,[LoadDate] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()",
    ]
    for col in source_columns:
        if col["column_name"] in [d["column_name"] for d in dims]:
            continue
        create_fact.append(f"    ,[{col['column_name']}] {simplify_type(col)} NULL")
    create_fact.append(f"    ,CONSTRAINT [PK_{fact_name}] PRIMARY KEY CLUSTERED ([FactKey])")
    create_fact.append(
        f"    ,CONSTRAINT [FK_{fact_name}_{dim_name}] FOREIGN KEY ([DimKey])"
        f" REFERENCES [{schema_name}].[{dim_name}]([DimKey])"
    )
    create_fact.append(") ON [PRIMARY];")

    insert_dim = [
        f"INSERT INTO [{schema_name}].[{dim_name}] ([SourceId], [LoadDate]"
        + ("" if not dims else ", " + ", ".join(f"[{c['column_name']}]" for c in dims))
        + ")",
        f"SELECT DISTINCT [ID] AS [SourceId], SYSUTCDATETIME() AS [LoadDate]"
        + ("" if not dims else ", " + ", ".join(f"[{c['column_name']}]" for c in dims)),
        f"FROM [{source_table['schema_name']}].[{source_table['object_name']}]",
        "WHERE [ID] IS NOT NULL;",
    ]

    insert_fact = [
        f"INSERT INTO [{schema_name}].[{fact_name}] ([DimKey], [LoadDate]"
        + ("" if not source_columns else ", " + ", ".join(
            f"[{c['column_name']}]" for c in source_columns
            if c["column_name"] not in [d["column_name"] for d in dims]
        ))
        + ")",
        "SELECT d.[DimKey], SYSUTCDATETIME() AS [LoadDate],",
        ", ".join(
            f"s.[{c['column_name']}]" for c in source_columns
            if c["column_name"] not in [d["column_name"] for d in dims]
        ),
        f"FROM [{source_table['schema_name']}].[{source_table['object_name']}] AS s",
        f"JOIN [{schema_name}].[{dim_name}] AS d ON d.[SourceId] = s.[ID]",
    ]

    if not any(col["column_name"].upper() == "ID" for col in source_columns):
        insert_dim[1] = (
            f"SELECT DISTINCT ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS [SourceId], SYSUTCDATETIME() AS [LoadDate]"
            + ("" if not dims else ", " + ", ".join(f"[{c['column_name']}]" for c in dims))
        )
        insert_dim[2] = f"FROM [{source_table['schema_name']}].[{source_table['object_name']}]"
        insert_dim[3] = "WHERE 1=1;"
        insert_fact[0] = (
            f"INSERT INTO [{schema_name}].[{fact_name}] ([DimKey], [LoadDate]"
            + ("" if not source_columns else ", " + ", ".join(
                f"[{c['column_name']}]" for c in source_columns
                if c["column_name"] not in [d["column_name"] for d in dims]
            ))
            + ")"
        )
        insert_fact[1] = "SELECT d.[DimKey], SYSUTCDATETIME() AS [LoadDate],"
        insert_fact[2] = ", ".join(
            f"s.[{c['column_name']}]" for c in source_columns
            if c["column_name"] not in [d["column_name"] for d in dims]
        )
        insert_fact[3] = f"FROM [{source_table['schema_name']}].[{source_table['object_name']}] AS s"
        insert_fact[4] = f"CROSS JOIN (SELECT TOP 1 [DimKey] FROM [{schema_name}].[{dim_name}] ORDER BY [DimKey]) AS d"

    return [
        ("Create datawarehouse schema", create_schema),
        (f"Drop {dim_name}",   f"IF OBJECT_ID(N'[{schema_name}].[{dim_name}]', N'U') IS NOT NULL DROP TABLE [{schema_name}].[{dim_name}];"),
        (f"Create {dim_name}", "\n".join(create_dim)),
        (f"Drop {fact_name}",  f"IF OBJECT_ID(N'[{schema_name}].[{fact_name}]', N'U') IS NOT NULL DROP TABLE [{schema_name}].[{fact_name}];"),
        (f"Create {fact_name}", "\n".join(create_fact)),
        (f"Load {dim_name}",  "-- Load dimension data\n" + "\n".join(insert_dim)),
        (f"Load {fact_name}", "-- Load fact data\n"      + "\n".join(insert_fact)),
    ]


def generate_package_sql(package_index, source_table, source_columns):
    batches = _build_sql_batches(package_index, source_table, source_columns)
    lines = ["SET NOCOUNT ON;", "GO"]
    for _name, sql in batches:
        lines.append(sql)
        lines.append("GO")
    return "\n".join(lines)


_BIML_NS = "http://schemas.varigence.com/biml.xsd"


def generate_package_biml(package_index, source_table, source_columns, connection_string):
    batches = _build_sql_batches(package_index, source_table, source_columns)
    package_name = f"package_{package_index + 1:02d}"
    conn_name = "DW"

    # Register as default namespace so serialised output uses no prefix.
    ET.register_namespace("", _BIML_NS)

    def tag(local):
        return f"{{{_BIML_NS}}}{local}"

    root = ET.Element(tag("Biml"))
    connections = ET.SubElement(root, tag("Connections"))
    ET.SubElement(connections, tag("OleDbConnection"), Name=conn_name, ConnectionString=connection_string)
    packages = ET.SubElement(root, tag("Packages"))
    pkg = ET.SubElement(packages, tag("Package"), Name=package_name, ConstraintMode="Linear")
    tasks = ET.SubElement(pkg, tag("Tasks"))
    for task_name, sql in batches:
        task = ET.SubElement(tasks, tag("ExecuteSQL"), Name=task_name, ConnectionName=conn_name)
        ET.SubElement(task, tag("DirectInput")).text = sql

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def write_package(output_dir, index, sql_text):
    filename = os.path.join(output_dir, f"package_{index + 1:02d}.sql")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(sql_text)
    return filename


def write_biml_package(output_dir, index, biml_text):
    filename = os.path.join(output_dir, f"package_{index + 1:02d}.biml")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(biml_text)
    return filename


def main():
    args = parse_args()
    if args.username and args.password is None:
        raise SystemExit("Error: --password is required when using --username.")
    if args.output_format == "biml" and not args.dw_connection:
        raise SystemExit("Error: --dw-connection is required when --output-format=biml.")
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

            if args.output_format == "biml":
                content = generate_package_biml(package_index, source_table, columns, args.dw_connection)
                package_path = write_biml_package(output_dir, package_index, content)
            else:
                content = generate_package_sql(package_index, source_table, columns)
                package_path = write_package(output_dir, package_index, content)
            package_paths.append(package_path)
            print(f"Generated: {package_path}")

        print("\nGeneration complete.")
        print(f"Scripts written to: {output_dir.resolve()}")
        if args.output_format == "biml":
            print("Next step: open the .biml files in BimlExpress (VS extension) or BimlStudio to compile into .dtsx packages.")
        else:
            print("Recommended next step: open the .sql files and validate object names before executing on production.")

if __name__ == "__main__":
    main()
