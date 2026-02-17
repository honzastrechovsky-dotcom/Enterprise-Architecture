"""Tests for sql_guard module - LLM-based SQL generation with safety validation.

Tests cover:
- SQL generation from natural language
- SQL injection detection and blocking
- Allowed vs blocked SQL patterns
- Parameterized query output
- Read-only enforcement
- Table whitelist validation
- Query complexity limits
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.connectors.sql_guard import QueryValidationResult, SQLGuard, TableSchema


@pytest.fixture
def mock_llm():
    """Mock LLM client."""
    llm = AsyncMock()
    llm.complete = AsyncMock()
    llm.extract_text = MagicMock()
    return llm


@pytest.fixture
def tenant_id():
    """Test tenant ID."""
    return uuid.uuid4()


@pytest.fixture
def tenant_schemas():
    """Test tenant schemas."""
    return {
        uuid.uuid4(): [
            TableSchema(
                table_name="orders",
                allowed_columns=["id", "customer_id", "total", "created_at"],
                description="Customer orders",
            ),
            TableSchema(
                table_name="customers",
                allowed_columns=["id", "name", "email"],
                description="Customer records",
            ),
            TableSchema(
                table_name="products",
                allowed_columns=["id", "name", "price"],
                description="Product catalog",
            ),
        ],
    }


@pytest.fixture
def sql_guard(mock_llm, tenant_schemas):
    """SQLGuard instance with mocks."""
    return SQLGuard(llm_client=mock_llm, tenant_schemas=tenant_schemas)


class TestSQLGeneration:
    """Test SQL generation from natural language."""

    @pytest.mark.asyncio
    async def test_generate_simple_select(self, sql_guard, mock_llm, tenant_id, tenant_schemas):
        """Test generating a simple SELECT query."""
        # Override tenant_schemas for this test
        sql_guard.tenant_schemas = {tenant_id: tenant_schemas[list(tenant_schemas.keys())[0]]}

        mock_llm.complete = AsyncMock(return_value=MagicMock())
        mock_llm.extract_text = MagicMock(return_value="SELECT id, name FROM customers WHERE id = 123")

        result = await sql_guard.query(
            tenant_id=tenant_id,
            natural_language_query="Get customer with ID 123",
        )

        assert result.success is True
        assert "SELECT" in result.data["sql"]

    @pytest.mark.asyncio
    async def test_generate_with_join(self, sql_guard, mock_llm, tenant_id, tenant_schemas):
        """Test generating a query with JOIN."""
        sql_guard.tenant_schemas = {tenant_id: tenant_schemas[list(tenant_schemas.keys())[0]]}

        mock_llm.complete = AsyncMock(return_value=MagicMock())
        mock_llm.extract_text = MagicMock(
            return_value="SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"
        )

        result = await sql_guard.query(
            tenant_id=tenant_id,
            natural_language_query="Get all orders with customer names",
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_generate_with_markdown_cleanup(self, sql_guard, mock_llm, tenant_id, tenant_schemas):
        """Test LLM response with markdown formatting is cleaned up."""
        sql_guard.tenant_schemas = {tenant_id: tenant_schemas[list(tenant_schemas.keys())[0]]}

        mock_llm.complete = AsyncMock(return_value=MagicMock())
        mock_llm.extract_text = MagicMock(
            return_value="```sql\nSELECT * FROM customers\n```"
        )

        # Generate SQL (will fail validation for SELECT *)
        await sql_guard._generate_sql(
            natural_language_query="Get all customers",
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        # Verify markdown was stripped
        generated_sql = await sql_guard._generate_sql(
            natural_language_query="Get all customers",
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )
        assert "```" not in generated_sql


class TestSQLInjectionDetection:
    """Test SQL injection pattern detection."""

    def test_detect_drop_table(self, sql_guard, tenant_schemas):
        """Test detection of DROP TABLE injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; DROP TABLE customers;"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_detect_delete(self, sql_guard, tenant_schemas):
        """Test detection of DELETE injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; DELETE FROM customers WHERE 1=1;"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_detect_insert(self, sql_guard, tenant_schemas):
        """Test detection of INSERT injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; INSERT INTO customers (name) VALUES ('hacker');"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_detect_update(self, sql_guard, tenant_schemas):
        """Test detection of UPDATE injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; UPDATE customers SET name='hacked' WHERE 1=1;"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_detect_union_select(self, sql_guard, tenant_schemas):
        """Test detection of UNION SELECT injection."""
        # UNION is not explicitly blocked but complexity check may catch it
        malicious_sql = "SELECT id FROM customers UNION SELECT id FROM admin_users"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        # Should fail due to unauthorized table 'admin_users'
        assert validation.valid is False

    def test_detect_truncate(self, sql_guard, tenant_schemas):
        """Test detection of TRUNCATE injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; TRUNCATE TABLE customers;"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_detect_grant(self, sql_guard, tenant_schemas):
        """Test detection of GRANT injection (caught by semicolon or DML check)."""
        malicious_sql = "SELECT * FROM customers; GRANT ALL ON customers TO hacker;"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False


class TestReadOnlyEnforcement:
    """Test read-only operation enforcement."""

    def test_allow_select(self, sql_guard, tenant_schemas):
        """Test that SELECT queries are allowed."""
        safe_sql = "SELECT id, name FROM customers"

        validation = sql_guard._validate_query(
            sql=safe_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is True

    def test_block_non_select(self, sql_guard, tenant_schemas):
        """Test that non-SELECT queries are blocked."""
        test_cases = [
            "INSERT INTO customers (name) VALUES ('test')",
            "UPDATE customers SET name='test' WHERE id=1",
            "DELETE FROM customers WHERE id=1",
            "DROP TABLE customers",
            "CREATE TABLE test (id INT)",
            "ALTER TABLE customers ADD COLUMN test VARCHAR(255)",
        ]

        for malicious_sql in test_cases:
            validation = sql_guard._validate_query(
                sql=malicious_sql,
                schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
            )
            assert validation.valid is False


class TestTableWhitelist:
    """Test table whitelist validation."""

    def test_allow_whitelisted_tables(self, sql_guard, tenant_schemas):
        """Test queries against whitelisted tables are allowed."""
        valid_sql = "SELECT id, name FROM customers"

        validation = sql_guard._validate_query(
            sql=valid_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is True

    def test_block_unauthorized_tables(self, sql_guard, tenant_schemas):
        """Test queries against non-whitelisted tables are blocked."""
        malicious_sql = "SELECT * FROM admin_users"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False
        assert "Unauthorized tables" in validation.error

    def test_allow_join_on_whitelisted_tables(self, sql_guard, tenant_schemas):
        """Test JOIN queries with whitelisted tables are allowed."""
        valid_sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"

        validation = sql_guard._validate_query(
            sql=valid_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is True

    def test_block_join_with_unauthorized_table(self, sql_guard, tenant_schemas):
        """Test JOIN with unauthorized table is blocked."""
        malicious_sql = "SELECT o.id, a.password FROM orders o JOIN admin a ON o.user_id = a.id"

        validation = sql_guard._validate_query(
            sql=malicious_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False


class TestComplexityLimits:
    """Test query complexity limits."""

    def test_allow_simple_query(self, sql_guard, tenant_schemas):
        """Test simple queries pass complexity check."""
        simple_sql = "SELECT id, name FROM customers WHERE id = 123"

        validation = sql_guard._validate_query(
            sql=simple_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is True
        assert validation.complexity_score < 10

    def test_block_excessive_joins(self, sql_guard, tenant_schemas):
        """Test queries with too many JOINs are blocked."""
        # 4 JOINs exceeds MAX_JOINS = 3
        complex_sql = """
            SELECT * FROM orders o
            JOIN customers c ON o.customer_id = c.id
            JOIN products p1 ON o.product_id = p1.id
            JOIN products p2 ON o.related_id = p2.id
            JOIN products p3 ON o.another_id = p3.id
        """

        validation = sql_guard._validate_query(
            sql=complex_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False
        assert "Too many JOINs" in validation.error

    def test_calculate_complexity_score(self, sql_guard):
        """Test complexity score calculation."""
        test_cases = [
            ("SELECT * FROM customers", 0),  # Minimal
            ("SELECT * FROM customers WHERE id = 1", 1),  # +1 for WHERE
            ("SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id", 2),  # +2 for JOIN
        ]

        for sql, expected_min_score in test_cases:
            score = sql_guard._calculate_complexity(sql)
            assert score >= expected_min_score


class TestQueryExecution:
    """Test SQL query execution."""

    @pytest.mark.asyncio
    async def test_execute_query_success(self, sql_guard, tenant_id, tenant_schemas):
        """Test successful query execution."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[
            MagicMock(_mapping={"id": 1, "name": "Customer 1"}),
            MagicMock(_mapping={"id": 2, "name": "Customer 2"}),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        sql_guard.tenant_schemas = {tenant_id: tenant_schemas[list(tenant_schemas.keys())[0]]}
        sql_guard.llm_client.complete = AsyncMock(return_value=MagicMock())
        sql_guard.llm_client.extract_text = MagicMock(return_value="SELECT id, name FROM customers")

        result = await sql_guard.query(
            tenant_id=tenant_id,
            natural_language_query="Get all customers",
            db_session=mock_db,
        )

        assert result.success is True
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_execute_query_adds_limit(self, sql_guard):
        """Test that LIMIT is added to queries without one."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        mock_db.execute = AsyncMock(return_value=mock_result)

        sql = "SELECT id FROM customers"
        await sql_guard._execute_query(mock_db, sql, max_rows=100)

        # Verify LIMIT was added
        call_args = mock_db.execute.call_args
        executed_sql = str(call_args[0][0])
        assert "LIMIT" in executed_sql.upper()


class TestTenantIsolation:
    """Test tenant schema isolation."""

    @pytest.mark.asyncio
    async def test_no_schemas_for_tenant(self, sql_guard, mock_llm):
        """Test query fails when tenant has no schemas configured."""
        unknown_tenant = uuid.uuid4()

        result = await sql_guard.query(
            tenant_id=unknown_tenant,
            natural_language_query="Get customers",
        )

        assert result.success is False
        assert "No schemas configured" in result.error

    def test_add_tenant_schema(self, sql_guard):
        """Test adding schemas for a new tenant."""
        new_tenant = uuid.uuid4()
        new_schemas = [
            TableSchema(
                table_name="test_table",
                allowed_columns=["id", "value"],
                description="Test table",
            ),
        ]

        sql_guard.add_tenant_schema(tenant_id=new_tenant, schemas=new_schemas)

        assert new_tenant in sql_guard.tenant_schemas
        assert len(sql_guard.tenant_schemas[new_tenant]) == 1


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_llm_generation_failure(self, sql_guard, mock_llm, tenant_id, tenant_schemas):
        """Test handling of LLM generation failure."""
        sql_guard.tenant_schemas = {tenant_id: tenant_schemas[list(tenant_schemas.keys())[0]]}
        mock_llm.complete = AsyncMock(side_effect=Exception("LLM error"))

        result = await sql_guard.query(
            tenant_id=tenant_id,
            natural_language_query="Get customers",
        )

        assert result.success is False

    def test_invalid_sql_parsing(self, sql_guard, tenant_schemas):
        """Test handling of unparseable SQL."""
        invalid_sql = "This is not SQL"

        validation = sql_guard._validate_query(
            sql=invalid_sql,
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False

    def test_empty_sql(self, sql_guard, tenant_schemas):
        """Test handling of empty SQL."""
        validation = sql_guard._validate_query(
            sql="",
            schemas=tenant_schemas[list(tenant_schemas.keys())[0]],
        )

        assert validation.valid is False
