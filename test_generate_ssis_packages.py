"""Tests for generate_ssis_packages.py SQL output generation.

Covers pure functions — no database connection required:
  simplify_type, render_column_def, choose_dimension_columns,
  choose_fact_source, generate_package_sql, build_connection_string.

Known bug documented in TestGeneratePackageSqlWithoutId:
  generate_package_sql raises IndexError when the source table has no
  column named exactly "ID" (case-insensitive match via .upper() == "ID").
  The no-ID branch sets insert_dim[4] but the list has only indices 0–3.
"""

import pytest

from generate_ssis_packages import (
    build_connection_string,
    choose_dimension_columns,
    choose_fact_source,
    generate_package_sql,
    render_column_def,
    simplify_type,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_col(name, data_type, *, max_length=50, precision=18, scale=0,
             is_nullable=True, is_identity=False):
    return {
        "column_name": name,
        "data_type": data_type,
        "max_length": max_length,
        "precision": precision,
        "scale": scale,
        "is_nullable": is_nullable,
        "is_identity": is_identity,
    }


def make_table(schema, name, object_id=1):
    return {"schema_name": schema, "object_name": name, "object_id": object_id}


# ---------------------------------------------------------------------------
# simplify_type
# ---------------------------------------------------------------------------

class TestSimplifyType:
    def test_nvarchar_fixed_length(self):
        # sys.columns stores max_length in bytes; NVARCHAR(50) → max_length=100
        assert simplify_type(make_col("x", "nvarchar", max_length=100)) == "NVARCHAR(50)"

    def test_nvarchar_max(self):
        assert simplify_type(make_col("x", "nvarchar", max_length=-1)) == "NVARCHAR(MAX)"

    def test_varchar_fixed_length(self):
        assert simplify_type(make_col("x", "varchar", max_length=50)) == "VARCHAR(50)"

    def test_varchar_max_returns_nvarchar_max(self):
        # Code returns "NVARCHAR(MAX)" for VARCHAR(MAX) — documents current behaviour
        assert simplify_type(make_col("x", "varchar", max_length=-1)) == "NVARCHAR(MAX)"

    def test_nchar(self):
        # NCHAR(10) → max_length=20 (2 bytes per char)
        assert simplify_type(make_col("x", "nchar", max_length=20)) == "NCHAR(10)"

    def test_char(self):
        assert simplify_type(make_col("x", "char", max_length=8)) == "CHAR(8)"

    def test_decimal_with_scale(self):
        assert simplify_type(make_col("x", "decimal", precision=18, scale=4)) == "DECIMAL(18,4)"

    def test_numeric_with_scale(self):
        assert simplify_type(make_col("x", "numeric", precision=10, scale=2)) == "NUMERIC(10,2)"

    def test_datetime2_with_scale(self):
        assert simplify_type(make_col("x", "datetime2", scale=7)) == "DATETIME2(7)"

    def test_datetimeoffset_with_scale(self):
        assert simplify_type(make_col("x", "datetimeoffset", scale=3)) == "DATETIMEOFFSET(3)"

    def test_time_with_zero_scale(self):
        assert simplify_type(make_col("x", "time", scale=0)) == "TIME(0)"

    def test_plain_int(self):
        assert simplify_type(make_col("x", "int")) == "INT"

    def test_plain_bigint(self):
        assert simplify_type(make_col("x", "bigint")) == "BIGINT"

    def test_plain_bit(self):
        assert simplify_type(make_col("x", "bit")) == "BIT"

    def test_plain_datetime_no_scale_suffix(self):
        # DATETIME has no precision argument in SQL Server; scale=0 in helper default
        assert simplify_type(make_col("x", "datetime")) == "DATETIME"


# ---------------------------------------------------------------------------
# render_column_def
# ---------------------------------------------------------------------------

class TestRenderColumnDef:
    def test_nullable_produces_null_keyword(self):
        c = make_col("CustomerName", "nvarchar", max_length=100, is_nullable=True)
        assert render_column_def(c) == "    [CustomerName] NVARCHAR(50) NULL"

    def test_not_nullable_produces_not_null_keyword(self):
        c = make_col("Email", "varchar", max_length=200, is_nullable=False)
        assert render_column_def(c) == "    [Email] VARCHAR(200) NOT NULL"

    def test_output_indented_four_spaces(self):
        c = make_col("Col", "int", is_nullable=True)
        assert render_column_def(c).startswith("    ")


# ---------------------------------------------------------------------------
# choose_fact_source
# ---------------------------------------------------------------------------

class TestChooseFactSource:
    def test_empty_list_returns_none(self):
        assert choose_fact_source([], 0) is None

    def test_single_table_always_chosen_regardless_of_index(self):
        tables = [make_table("dbo", "Orders")]
        for i in range(6):
            assert choose_fact_source(tables, i) is tables[0]

    def test_wraps_around_with_modulo(self):
        tables = [make_table("dbo", "A"), make_table("dbo", "B"), make_table("dbo", "C")]
        assert choose_fact_source(tables, 0) is tables[0]
        assert choose_fact_source(tables, 1) is tables[1]
        assert choose_fact_source(tables, 2) is tables[2]
        assert choose_fact_source(tables, 3) is tables[0]
        assert choose_fact_source(tables, 4) is tables[1]


# ---------------------------------------------------------------------------
# choose_dimension_columns
# ---------------------------------------------------------------------------

class TestChooseDimensionColumns:
    def test_empty_input_returns_empty(self):
        assert choose_dimension_columns([]) == []

    def test_id_suffix_columns_skipped_then_falls_back_to_first(self):
        columns = [make_col("CustomerID", "int"), make_col("OrderID", "int")]
        result = choose_dimension_columns(columns)
        # Both qualify by dtype but end with "id" → no dims found → fallback to first col
        assert len(result) == 1
        assert result[0]["column_name"] == "CustomerID"

    def test_picks_non_id_string_columns(self):
        columns = [
            make_col("ID", "int"),
            make_col("CustomerName", "nvarchar"),
            make_col("Region", "varchar"),
        ]
        names = [c["column_name"] for c in choose_dimension_columns(columns)]
        assert "CustomerName" in names
        assert "Region" in names
        assert "ID" not in names

    def test_returns_at_most_two(self):
        columns = [
            make_col("Name", "nvarchar"),
            make_col("Category", "varchar"),
            make_col("Status", "char"),
            make_col("Type", "nchar"),
        ]
        assert len(choose_dimension_columns(columns)) == 2

    def test_non_qualifying_dtype_falls_back_to_first_column(self):
        # DATETIME is not in the qualifying dtype set
        columns = [make_col("CreatedAt", "datetime"), make_col("UpdatedAt", "datetime")]
        result = choose_dimension_columns(columns)
        assert len(result) == 1
        assert result[0]["column_name"] == "CreatedAt"

    def test_int_column_qualifies(self):
        columns = [make_col("Quantity", "int"), make_col("Notes", "decimal")]
        names = [c["column_name"] for c in choose_dimension_columns(columns)]
        assert "Quantity" in names

    def test_bigint_qualifies(self):
        columns = [make_col("ExternalRef", "bigint")]
        result = choose_dimension_columns(columns)
        assert result[0]["column_name"] == "ExternalRef"

    def test_single_qualifying_column_returned(self):
        columns = [make_col("ID", "int"), make_col("Label", "nvarchar")]
        result = choose_dimension_columns(columns)
        assert len(result) == 1
        assert result[0]["column_name"] == "Label"


# ---------------------------------------------------------------------------
# generate_package_sql — with ID column (happy path)
# ---------------------------------------------------------------------------

_TABLE_WITH_ID = make_table("sales", "Orders")
_COLS_WITH_ID = [
    make_col("ID", "int", is_nullable=False, is_identity=True),
    make_col("CustomerName", "nvarchar", max_length=200),
    make_col("Region", "varchar", max_length=100),
    make_col("Amount", "decimal", precision=18, scale=2),
]


class TestGeneratePackageSqlWithId:
    def setup_method(self):
        self.sql = generate_package_sql(0, _TABLE_WITH_ID, _COLS_WITH_ID)

    def test_set_nocount_on(self):
        assert "SET NOCOUNT ON;" in self.sql

    def test_schema_creation_guarded(self):
        assert "CREATE SCHEMA [datawarehouse]" in self.sql

    def test_dim_table_name_derived_from_source(self):
        assert "[datawarehouse].[dim_orders]" in self.sql

    def test_fact_table_name_derived_from_source(self):
        assert "[datawarehouse].[fact_orders]" in self.sql

    def test_dim_dropped_before_created(self):
        drop_pos = self.sql.index("DROP TABLE [datawarehouse].[dim_orders]")
        create_pos = self.sql.index("CREATE TABLE [datawarehouse].[dim_orders]")
        assert drop_pos < create_pos

    def test_fact_dropped_before_created(self):
        drop_pos = self.sql.index("DROP TABLE [datawarehouse].[fact_orders]")
        create_pos = self.sql.index("CREATE TABLE [datawarehouse].[fact_orders]")
        assert drop_pos < create_pos

    def test_dim_key_is_int_identity(self):
        assert "[DimKey] INT IDENTITY(1,1)" in self.sql

    def test_fact_key_is_bigint_identity(self):
        assert "[FactKey] BIGINT IDENTITY(1,1)" in self.sql

    def test_dim_primary_key_constraint(self):
        assert "CONSTRAINT [PK_dim_orders] PRIMARY KEY CLUSTERED ([DimKey])" in self.sql

    def test_fact_primary_key_constraint(self):
        assert "CONSTRAINT [PK_fact_orders] PRIMARY KEY CLUSTERED ([FactKey])" in self.sql

    def test_fact_foreign_key_references_dim(self):
        assert (
            "CONSTRAINT [FK_fact_orders_dim_orders] FOREIGN KEY ([DimKey]) "
            "REFERENCES [datawarehouse].[dim_orders]([DimKey])"
        ) in self.sql

    def test_dim_insert_selects_distinct_by_id(self):
        assert "SELECT DISTINCT [ID] AS [SourceId]" in self.sql

    def test_dim_insert_filtered_by_id_not_null(self):
        assert "WHERE [ID] IS NOT NULL;" in self.sql

    def test_fact_insert_joins_dim_on_source_id(self):
        assert "JOIN [datawarehouse].[dim_orders] AS d ON d.[SourceId] = s.[ID]" in self.sql

    def test_source_table_in_from_clause(self):
        assert "FROM [sales].[Orders]" in self.sql

    def test_load_date_uses_sysutcdatetime(self):
        assert "SYSUTCDATETIME()" in self.sql

    def test_dimension_columns_included_in_dim_insert(self):
        assert "[CustomerName]" in self.sql
        assert "[Region]" in self.sql

    def test_go_batch_separators_present(self):
        assert self.sql.count("\nGO") >= 4

    def test_section_comments_present(self):
        assert "-- Load dimension data" in self.sql
        assert "-- Load fact data" in self.sql

    def test_dim_load_precedes_fact_load(self):
        dim_pos = self.sql.index("-- Load dimension data")
        fact_pos = self.sql.index("-- Load fact data")
        assert dim_pos < fact_pos


# ---------------------------------------------------------------------------
# generate_package_sql — without ID column (known IndexError bug)
# ---------------------------------------------------------------------------

_TABLE_NO_ID = make_table("crm", "Contacts")
_COLS_NO_ID = [
    make_col("ContactName", "nvarchar", max_length=200),
    make_col("Phone", "varchar", max_length=50),
    make_col("Revenue", "decimal", precision=18, scale=2),
]


class TestGeneratePackageSqlWithoutId:
    def test_row_number_used_in_dim_insert(self):
        sql = generate_package_sql(0, _TABLE_NO_ID, _COLS_NO_ID)
        assert "ROW_NUMBER() OVER (ORDER BY (SELECT 1))" in sql

    def test_cross_join_used_in_fact_insert(self):
        sql = generate_package_sql(0, _TABLE_NO_ID, _COLS_NO_ID)
        assert "CROSS JOIN" in sql

    def test_where_1_equals_1_in_dim_insert(self):
        sql = generate_package_sql(0, _TABLE_NO_ID, _COLS_NO_ID)
        assert "WHERE 1=1;" in sql

    def test_dim_and_fact_tables_still_generated(self):
        sql = generate_package_sql(0, _TABLE_NO_ID, _COLS_NO_ID)
        assert "[datawarehouse].[dim_contacts]" in sql
        assert "[datawarehouse].[fact_contacts]" in sql


# ---------------------------------------------------------------------------
# generate_package_sql — table name sanitisation
# ---------------------------------------------------------------------------

class TestGeneratePackageSqlTableNameSanitisation:
    def test_hyphens_replaced_with_underscores(self):
        src = make_table("dbo", "My-Table")
        cols = [make_col("ID", "int"), make_col("Label", "nvarchar", max_length=100)]
        sql = generate_package_sql(0, src, cols)
        assert "dim_my_table" in sql
        assert "fact_my_table" in sql

    def test_spaces_replaced_with_underscores(self):
        src = make_table("dbo", "My Table")
        cols = [make_col("ID", "int"), make_col("Label", "nvarchar", max_length=100)]
        sql = generate_package_sql(0, src, cols)
        assert "dim_my_table" in sql

    def test_uppercase_source_name_lowercased_in_dim_fact_names(self):
        src = make_table("dbo", "PRODUCTS")
        cols = [make_col("ID", "int"), make_col("Name", "nvarchar", max_length=100)]
        sql = generate_package_sql(0, src, cols)
        assert "dim_products" in sql
        assert "fact_products" in sql

    def test_source_schema_preserved_verbatim_in_from_clause(self):
        src = make_table("sales", "Orders")
        sql = generate_package_sql(0, src, _COLS_WITH_ID)
        assert "FROM [sales].[Orders]" in sql


# ---------------------------------------------------------------------------
# generate_package_sql — package index behaviour
# ---------------------------------------------------------------------------

class TestGeneratePackageSqlPackageIndex:
    def test_package_index_does_not_change_table_names(self):
        src = make_table("dbo", "Products")
        cols = [make_col("ID", "int"), make_col("Name", "nvarchar", max_length=100)]
        sql_0 = generate_package_sql(0, src, cols)
        sql_9 = generate_package_sql(9, src, cols)
        assert "dim_products" in sql_0
        assert "dim_products" in sql_9


# ---------------------------------------------------------------------------
# build_connection_string
# ---------------------------------------------------------------------------

class TestBuildConnectionString:
    def test_trusted_connection_flag(self):
        cs = build_connection_string("myserver", "mydb", trusted=True, username=None, password=None)
        assert "Trusted_Connection=yes" in cs

    def test_trusted_connection_includes_server_and_database(self):
        cs = build_connection_string("myserver", "mydb", trusted=True, username=None, password=None)
        assert "SERVER=myserver" in cs
        assert "DATABASE=mydb" in cs

    def test_sql_auth_includes_uid_and_pwd(self):
        cs = build_connection_string("srv", "db", trusted=False, username="sa", password="s3cr3t")
        assert "UID=sa" in cs
        assert "PWD=s3cr3t" in cs

    def test_sql_auth_excludes_trusted_connection(self):
        cs = build_connection_string("srv", "db", trusted=False, username="sa", password="s3cr3t")
        assert "Trusted_Connection" not in cs

    def test_missing_password_raises_value_error(self):
        with pytest.raises(ValueError):
            build_connection_string("s", "d", trusted=False, username="user", password=None)

    def test_missing_username_raises_value_error(self):
        with pytest.raises(ValueError):
            build_connection_string("s", "d", trusted=False, username=None, password="pass")

    def test_empty_string_password_is_not_rejected(self):
        # password="" passes the `is None` check — documents current permissive behaviour
        cs = build_connection_string("s", "d", trusted=False, username="user", password="")
        assert "UID=user" in cs
