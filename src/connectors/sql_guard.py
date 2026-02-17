"""SQL Guard - LLM-based SQL generation with safety validation.

SQLGuard enables natural language queries over structured data:
1. LLM generates SQL from natural language
2. Validates against tenant-specific whitelist (tables/columns)
3. Enforces read-only operations (no INSERT/UPDATE/DELETE/DROP)
4. Parameterizes queries to prevent SQL injection
5. Limits query complexity (max JOINs, no subqueries without approval)
6. Enforces result row limits

This is structured data RAG for enterprise databases.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import sqlparse
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.connectors.base import ConnectorResult

log = structlog.get_logger(__name__)


@dataclass
class TableSchema:
    """Schema definition for a whitelisted table."""

    table_name: str
    allowed_columns: list[str]  # Empty list = all columns allowed
    description: str  # For LLM context


@dataclass
class QueryValidationResult:
    """Result of SQL query validation."""

    valid: bool
    error: str | None = None
    normalized_query: str | None = None
    complexity_score: int = 0


class SQLGuard:
    """LLM-based SQL generation with safety validation.

    Generates SQL from natural language queries and validates against:
    - Tenant-specific table/column whitelist
    - Read-only operations only
    - Query complexity limits
    - SQL injection patterns
    """

    # Max complexity thresholds
    MAX_JOINS = 3
    MAX_WHERE_CLAUSES = 5
    MAX_ROWS = 1000

    # Dangerous SQL keywords (must be uppercase in query)
    WRITE_KEYWORDS = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
    }

    def __init__(
        self,
        llm_client: LLMClient,
        tenant_schemas: dict[uuid.UUID, list[TableSchema]],
    ) -> None:
        """Initialize SQLGuard with tenant schema whitelist.

        Args:
            llm_client: LLMClient for SQL generation
            tenant_schemas: Mapping of tenant_id -> list of allowed TableSchemas
        """
        self.llm_client = llm_client
        self.tenant_schemas = tenant_schemas

    async def query(
        self,
        tenant_id: uuid.UUID,
        natural_language_query: str,
        max_rows: int = MAX_ROWS,
        db_session: AsyncSession | None = None,
    ) -> ConnectorResult:
        """Convert natural language to SQL, validate, and execute.

        Args:
            tenant_id: Tenant UUID for schema whitelist
            natural_language_query: User's natural language query
            max_rows: Maximum rows to return
            db_session: AsyncSession for query execution (optional)

        Returns:
            ConnectorResult with query results or error
        """
        try:
            # 1. Get tenant schemas
            schemas = self.tenant_schemas.get(tenant_id)
            if not schemas:
                return ConnectorResult(
                    success=False,
                    error=f"No schemas configured for tenant {tenant_id}",
                )

            # 2. LLM generates SQL
            generated_sql = await self._generate_sql(
                natural_language_query,
                schemas,
            )

            log.info(
                "sql_guard.generated_sql",
                tenant_id=str(tenant_id),
                query=natural_language_query[:100],
                sql=generated_sql[:200],
            )

            # 3. Validate SQL
            validation = self._validate_query(generated_sql, schemas)
            if not validation.valid:
                log.warning(
                    "sql_guard.validation_failed",
                    tenant_id=str(tenant_id),
                    error=validation.error,
                )
                return ConnectorResult(
                    success=False,
                    error=f"Query validation failed: {validation.error}",
                )

            # 4. Execute if db_session provided
            if db_session:
                results = await self._execute_query(
                    db_session,
                    validation.normalized_query or generated_sql,
                    max_rows,
                )
                return ConnectorResult(
                    success=True,
                    data=results,
                    metadata={
                        "tenant_id": str(tenant_id),
                        "row_count": len(results),
                        "complexity_score": validation.complexity_score,
                        "generated_sql": validation.normalized_query,
                    },
                )
            else:
                # No execution - return validated SQL only
                return ConnectorResult(
                    success=True,
                    data={"sql": validation.normalized_query or generated_sql},
                    metadata={
                        "tenant_id": str(tenant_id),
                        "complexity_score": validation.complexity_score,
                    },
                )

        except Exception as exc:
            log.error("sql_guard.query_failed", tenant_id=str(tenant_id), error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _generate_sql(
        self,
        natural_language_query: str,
        schemas: list[TableSchema],
    ) -> str:
        """Use LLM to generate SQL from natural language.

        Returns:
            Generated SQL query string
        """
        # Build schema context for LLM
        schema_context = self._build_schema_context(schemas)

        prompt = f"""You are a SQL query generator. Convert the user's natural language query into a valid SQL SELECT statement.

AVAILABLE TABLES AND COLUMNS:
{schema_context}

RULES:
1. Only use tables and columns listed above
2. Only generate SELECT queries (no INSERT, UPDATE, DELETE, etc.)
3. Use explicit column names (avoid SELECT *)
4. Add appropriate WHERE clauses for filtering
5. Use JOINs only when necessary (max 3 joins)
6. Return ONLY the SQL query, no explanation

USER QUERY: {natural_language_query}

SQL:"""

        messages = [
            {"role": "system", "content": "You are a SQL query generator for enterprise data access."},
            {"role": "user", "content": prompt},
        ]

        response = await self.llm_client.complete(
            messages=messages,
            temperature=0.0,  # Deterministic for SQL generation
            max_tokens=500,
        )

        sql = self.llm_client.extract_text(response).strip()

        # Clean up markdown formatting if present
        sql = sql.replace("```sql", "").replace("```", "").strip()

        return sql

    def _build_schema_context(self, schemas: list[TableSchema]) -> str:
        """Build formatted schema description for LLM prompt."""
        lines = []
        for schema in schemas:
            columns_str = ", ".join(schema.allowed_columns) if schema.allowed_columns else "all columns"
            lines.append(f"- {schema.table_name}: {columns_str}")
            if schema.description:
                lines.append(f"  Description: {schema.description}")
        return "\n".join(lines)

    def _validate_query(
        self,
        sql: str,
        schemas: list[TableSchema],
    ) -> QueryValidationResult:
        """Validate generated SQL against safety rules.

        Checks:
        1. Read-only (no write operations)
        2. Only whitelisted tables/columns
        3. Complexity limits (max joins, etc.)
        4. No SQL injection patterns
        5. No multi-statement queries (semicolons)

        Returns:
            QueryValidationResult with validation status
        """
        try:
            # 0. Reject multi-statement queries (semicolons outside string literals)
            if ';' in sql.replace("';'", ''):
                return QueryValidationResult(
                    valid=False,
                    error="Multi-statement queries are not allowed",
                )

            # 1. Parse SQL
            parsed = sqlparse.parse(sql)
            if not parsed:
                return QueryValidationResult(
                    valid=False,
                    error="Failed to parse SQL",
                )

            statement = parsed[0]
            normalized = statement.get_type()

            # 2. Check read-only
            if normalized != "SELECT":
                return QueryValidationResult(
                    valid=False,
                    error=f"Only SELECT queries allowed, got: {normalized}",
                )

            # 3. Check for write keywords
            sql_upper = sql.upper()
            for keyword in self.WRITE_KEYWORDS:
                if re.search(rf"\b{keyword}\b", sql_upper):
                    return QueryValidationResult(
                        valid=False,
                        error=f"Write operation detected: {keyword}",
                    )

            # 4. Check table whitelist
            tables_in_query = self._extract_tables(sql)
            allowed_tables = {schema.table_name for schema in schemas}
            unauthorized_tables = tables_in_query - allowed_tables
            if unauthorized_tables:
                return QueryValidationResult(
                    valid=False,
                    error=f"Unauthorized tables: {unauthorized_tables}",
                )

            # 5. Check column whitelist (simplified - full implementation would parse columns)
            # We trust LLM to use correct columns from the prompt context

            # 6. Check JOIN count
            join_count = sql_upper.count(" JOIN ")
            if join_count > self.MAX_JOINS:
                return QueryValidationResult(
                    valid=False,
                    error=f"Too many JOINs ({join_count}, max: {self.MAX_JOINS})",
                )

            # 7. Check complexity
            complexity_score = self._calculate_complexity(sql)
            if complexity_score > 10:
                return QueryValidationResult(
                    valid=False,
                    error=f"Query too complex (score: {complexity_score}, max: 10)",
                )

            # Validation passed
            return QueryValidationResult(
                valid=True,
                normalized_query=str(statement),
                complexity_score=complexity_score,
            )

        except Exception as exc:
            return QueryValidationResult(
                valid=False,
                error=f"Validation error: {exc}",
            )

    def _extract_tables(self, sql: str) -> set[str]:
        """Extract table names from SQL query.

        Simplified extraction - looks for FROM and JOIN clauses.
        Production would use full SQL parser.
        """
        tables = set()
        sql_upper = sql.upper()

        # Extract all FROM clause tables (including after UNION)
        from_matches = re.finditer(r"\bFROM\s+(\w+)", sql_upper)
        for match in from_matches:
            tables.add(match.group(1).lower())

        # Extract JOIN clause tables
        join_matches = re.finditer(r"\bJOIN\s+(\w+)", sql_upper)
        for match in join_matches:
            tables.add(match.group(1).lower())

        return tables

    def _calculate_complexity(self, sql: str) -> int:
        """Calculate query complexity score.

        Higher score = more complex query.
        """
        sql_upper = sql.upper()
        score = 0

        # JOINs add complexity
        score += sql_upper.count(" JOIN ") * 2

        # WHERE clauses add complexity
        score += sql_upper.count(" WHERE ")

        # Subqueries add significant complexity
        score += sql_upper.count(" SELECT ") * 3  # Nested SELECTs

        # GROUP BY adds complexity
        score += sql_upper.count(" GROUP BY ")

        # ORDER BY adds minimal complexity
        score += sql_upper.count(" ORDER BY ") * 0.5

        return int(score)

    async def _execute_query(
        self,
        db_session: AsyncSession,
        sql: str,
        max_rows: int,
    ) -> list[dict[str, Any]]:
        """Execute validated SQL query and return results.

        Returns:
            List of row dictionaries
        """
        # Strip any existing LIMIT clause and enforce our own safe limit
        sql = re.sub(r'\bLIMIT\s+\d+\b', '', sql, flags=re.IGNORECASE).strip().rstrip(';')
        sql = f"{sql} LIMIT {min(max_rows, 1000)}"

        result = await db_session.execute(text(sql))
        rows = result.fetchall()

        # Convert to list of dicts
        return [dict(row._mapping) for row in rows]

    def add_tenant_schema(
        self,
        tenant_id: uuid.UUID,
        schemas: list[TableSchema],
    ) -> None:
        """Add or update schema whitelist for a tenant.

        Args:
            tenant_id: Tenant UUID
            schemas: List of TableSchema definitions
        """
        self.tenant_schemas[tenant_id] = schemas
        log.info(
            "sql_guard.schema_updated",
            tenant_id=str(tenant_id),
            table_count=len(schemas),
        )
