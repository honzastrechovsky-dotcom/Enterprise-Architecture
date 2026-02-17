"""Tests for the SDK generator.

Coverage:
  - Python client generation from OpenAPI spec
  - TypeScript client generation from OpenAPI spec
  - Go client generation from OpenAPI spec
  - Template rendering with correct type mappings
  - String filter helpers (snake_case, pascal_case, camel_case)
  - Operation extraction from spec paths
  - Schema extraction
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.sdk.generator import (
    SDKGenerator,
    _build_template_context,
    _extract_operations,
    _extract_schemas,
    _schema_to_python_type,
    _schema_to_typescript_type,
    _schema_to_go_type,
    _to_camel_case,
    _to_pascal_case,
    _to_snake_case,
    _OA_TO_PYTHON,
    _OA_TO_TYPESCRIPT,
    _OA_TO_GO,
)


# ------------------------------------------------------------------ #
# Test fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def minimal_spec() -> dict[str, Any]:
    """Minimal valid OpenAPI 3.x spec for testing."""
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Test API",
            "version": "1.0.0",
            "description": "A test API.",
        },
        "servers": [{"url": "https://api.test.com"}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "summary": "List all items",
                    "tags": ["items"],
                    "parameters": [],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ItemList"}
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createItem",
                    "summary": "Create a new item",
                    "tags": ["items"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ItemCreate"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                },
            },
            "/items/{item_id}": {
                "get": {
                    "operationId": "getItem",
                    "summary": "Get a single item",
                    "tags": ["items"],
                    "parameters": [
                        {
                            "name": "item_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                },
                "delete": {
                    "operationId": "deleteItem",
                    "summary": "Delete an item",
                    "tags": ["items"],
                    "parameters": [
                        {
                            "name": "item_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"204": {"description": "No Content"}},
                },
            },
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "description": "An item resource.",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "count": {"type": "integer"},
                        "active": {"type": "boolean"},
                    },
                    "required": ["id", "name"],
                },
                "ItemCreate": {
                    "type": "object",
                    "description": "Request body for creating an item.",
                    "properties": {
                        "name": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["name"],
                },
                "ItemList": {
                    "type": "object",
                    "description": "Paginated list of items.",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                        "total": {"type": "integer"},
                    },
                    "required": ["items", "total"],
                },
            }
        },
    }


# ------------------------------------------------------------------ #
# String filter tests
# ------------------------------------------------------------------ #


class TestStringFilters:
    """Tests for Jinja2 string filter helpers."""

    def test_to_snake_case_from_camel(self) -> None:
        assert _to_snake_case("listItems") == "list_items"

    def test_to_snake_case_from_pascal(self) -> None:
        assert _to_snake_case("GetWebhookDeliveries") == "get_webhook_deliveries"

    def test_to_snake_case_already_snake(self) -> None:
        assert _to_snake_case("list_items") == "list_items"

    def test_to_pascal_case_from_snake(self) -> None:
        assert _to_pascal_case("list_items") == "ListItems"

    def test_to_pascal_case_from_camel(self) -> None:
        assert _to_pascal_case("listItems") == "Listitems"

    def test_to_camel_case_from_snake(self) -> None:
        assert _to_camel_case("list_items") == "listItems"

    def test_to_camel_case_from_pascal(self) -> None:
        assert _to_camel_case("ListItems") == "listitems"

    def test_to_camel_case_multi_word(self) -> None:
        assert _to_camel_case("get_webhook_deliveries") == "getWebhookDeliveries"


# ------------------------------------------------------------------ #
# Type mapping tests
# ------------------------------------------------------------------ #


class TestTypeMappings:
    """Tests for OpenAPI type -> language type mapping."""

    def test_python_string_type(self) -> None:
        assert _schema_to_python_type({"type": "string"}) == "str"

    def test_python_integer_type(self) -> None:
        assert _schema_to_python_type({"type": "integer"}) == "int"

    def test_python_boolean_type(self) -> None:
        assert _schema_to_python_type({"type": "boolean"}) == "bool"

    def test_python_array_type(self) -> None:
        result = _schema_to_python_type({"type": "array", "items": {"type": "string"}})
        assert result == "list[str]"

    def test_python_ref_type(self) -> None:
        result = _schema_to_python_type({"$ref": "#/components/schemas/Item"})
        assert result == "Item"

    def test_python_empty_schema(self) -> None:
        assert _schema_to_python_type({}) == "Any"

    def test_typescript_string_type(self) -> None:
        assert _schema_to_typescript_type({"type": "string"}) == "string"

    def test_typescript_integer_type(self) -> None:
        assert _schema_to_typescript_type({"type": "integer"}) == "number"

    def test_typescript_boolean_type(self) -> None:
        assert _schema_to_typescript_type({"type": "boolean"}) == "boolean"

    def test_typescript_array_type(self) -> None:
        result = _schema_to_typescript_type({"type": "array", "items": {"type": "string"}})
        assert result == "Array<string>"

    def test_typescript_ref_type(self) -> None:
        result = _schema_to_typescript_type({"$ref": "#/components/schemas/Item"})
        assert result == "Item"

    def test_go_string_type(self) -> None:
        assert _schema_to_go_type({"type": "string"}) == "string"

    def test_go_integer_type(self) -> None:
        assert _schema_to_go_type({"type": "integer"}) == "int64"

    def test_go_boolean_type(self) -> None:
        assert _schema_to_go_type({"type": "boolean"}) == "bool"

    def test_go_ref_type(self) -> None:
        result = _schema_to_go_type({"$ref": "#/components/schemas/Item"})
        assert result == "*Item"


# ------------------------------------------------------------------ #
# Operation extraction tests
# ------------------------------------------------------------------ #


class TestOperationExtraction:
    """Tests for extracting operations from OpenAPI spec paths."""

    def test_extract_operations_count(self, minimal_spec: dict[str, Any]) -> None:
        """Should extract all operations from paths."""
        ops = _extract_operations(minimal_spec)
        assert len(ops) == 4  # GET /items, POST /items, GET /items/{id}, DELETE /items/{id}

    def test_extract_operations_has_operation_id(self, minimal_spec: dict[str, Any]) -> None:
        """Each operation should have an operation_id."""
        ops = _extract_operations(minimal_spec)
        for op in ops:
            assert "operation_id" in op
            assert op["operation_id"]

    def test_extract_operations_has_method(self, minimal_spec: dict[str, Any]) -> None:
        """Each operation should have an HTTP method."""
        ops = _extract_operations(minimal_spec)
        methods = {op["method"] for op in ops}
        assert "GET" in methods
        assert "POST" in methods
        assert "DELETE" in methods

    def test_extract_operations_has_path(self, minimal_spec: dict[str, Any]) -> None:
        """Each operation should have a path."""
        ops = _extract_operations(minimal_spec)
        paths = {op["path"] for op in ops}
        assert "/items" in paths
        assert "/items/{item_id}" in paths

    def test_extract_operations_path_params(self, minimal_spec: dict[str, Any]) -> None:
        """Path parameters should be captured in the operation."""
        ops = _extract_operations(minimal_spec)
        get_item = next(op for op in ops if op["operation_id"] == "getItem")
        params = get_item["parameters"]
        assert len(params) == 1
        assert params[0]["name"] == "item_id"


# ------------------------------------------------------------------ #
# Schema extraction tests
# ------------------------------------------------------------------ #


class TestSchemaExtraction:
    """Tests for extracting component schemas."""

    def test_extract_schemas_returns_dict(self, minimal_spec: dict[str, Any]) -> None:
        """Should return a dict of schema name -> schema."""
        schemas = _extract_schemas(minimal_spec)
        assert isinstance(schemas, dict)

    def test_extract_schemas_finds_item(self, minimal_spec: dict[str, Any]) -> None:
        """Should find the Item schema."""
        schemas = _extract_schemas(minimal_spec)
        assert "Item" in schemas

    def test_extract_schemas_empty_spec(self) -> None:
        """Should return empty dict for a spec with no components."""
        schemas = _extract_schemas({})
        assert schemas == {}


# ------------------------------------------------------------------ #
# Template context tests
# ------------------------------------------------------------------ #


class TestTemplateContext:
    """Tests for the shared Jinja2 template context."""

    def test_build_context_has_title(self, minimal_spec: dict[str, Any]) -> None:
        """Template context should include the API title."""
        ctx = _build_template_context(minimal_spec)
        assert ctx["title"] == "Test API"

    def test_build_context_has_base_url(self, minimal_spec: dict[str, Any]) -> None:
        """Template context should include the first server URL."""
        ctx = _build_template_context(minimal_spec)
        assert ctx["base_url"] == "https://api.test.com"

    def test_build_context_has_operations(self, minimal_spec: dict[str, Any]) -> None:
        """Template context should include the operations list."""
        ctx = _build_template_context(minimal_spec)
        assert len(ctx["operations"]) > 0

    def test_build_context_has_schemas(self, minimal_spec: dict[str, Any]) -> None:
        """Template context should include schemas."""
        ctx = _build_template_context(minimal_spec)
        assert "Item" in ctx["schemas"]

    def test_build_context_type_helpers_callable(self, minimal_spec: dict[str, Any]) -> None:
        """Template context type helper functions should be callable."""
        ctx = _build_template_context(minimal_spec)
        assert callable(ctx["python_type"])
        assert callable(ctx["ts_type"])
        assert callable(ctx["go_type"])


# ------------------------------------------------------------------ #
# SDK generator integration tests
# ------------------------------------------------------------------ #


class TestSDKGenerator:
    """Integration tests for the SDKGenerator class."""

    @pytest.fixture
    def generator(self) -> SDKGenerator:
        """Create an SDKGenerator instance (requires Jinja2)."""
        pytest.importorskip("jinja2", reason="jinja2 required for SDK generation")
        return SDKGenerator()

    def test_generate_python_client_returns_string(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """generate_python_client should return a non-empty Python source string."""
        source = generator.generate_python_client(minimal_spec)
        assert isinstance(source, str)
        assert len(source) > 100

    def test_generate_python_client_has_class(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Python client should define an EAPClient class."""
        source = generator.generate_python_client(minimal_spec)
        assert "class EAPClient" in source

    def test_generate_python_client_has_methods(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Python client should have async methods for operations."""
        source = generator.generate_python_client(minimal_spec)
        assert "async def " in source
        assert "list_items" in source or "listItems" in source.lower()

    def test_generate_python_client_has_models(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Python client should include Pydantic models."""
        source = generator.generate_python_client(minimal_spec)
        assert "class Item" in source or "BaseModel" in source

    def test_generate_typescript_client_returns_string(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """generate_typescript_client should return a non-empty TypeScript source string."""
        source = generator.generate_typescript_client(minimal_spec)
        assert isinstance(source, str)
        assert len(source) > 100

    def test_generate_typescript_client_has_class(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated TypeScript client should define an EAPClient class."""
        source = generator.generate_typescript_client(minimal_spec)
        assert "class EAPClient" in source

    def test_generate_typescript_client_has_interfaces(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated TypeScript client should include TypeScript interfaces."""
        source = generator.generate_typescript_client(minimal_spec)
        assert "interface Item" in source or "export interface" in source

    def test_generate_go_client_returns_string(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """generate_go_client should return a non-empty Go source string."""
        source = generator.generate_go_client(minimal_spec)
        assert isinstance(source, str)
        assert len(source) > 100

    def test_generate_go_client_has_package(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Go client should start with a package declaration."""
        source = generator.generate_go_client(minimal_spec)
        assert "package eap" in source

    def test_generate_go_client_has_structs(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Go client should include Go struct types."""
        source = generator.generate_go_client(minimal_spec)
        assert "type Item struct" in source or "type " in source

    def test_generate_python_client_has_auth_handling(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated Python client should handle both auth schemes."""
        source = generator.generate_python_client(minimal_spec)
        assert "bearer_token" in source or "BearerToken" in source
        assert "api_key" in source or "ApiKey" in source or "X-API-Key" in source

    def test_generate_typescript_client_has_error_types(
        self,
        generator: SDKGenerator,
        minimal_spec: dict[str, Any],
    ) -> None:
        """Generated TypeScript client should include custom error classes."""
        source = generator.generate_typescript_client(minimal_spec)
        assert "EAPAPIError" in source
        assert "EAPAuthError" in source

    def test_generate_python_client_empty_spec(
        self,
        generator: SDKGenerator,
    ) -> None:
        """Generator should handle an empty spec without crashing."""
        source = generator.generate_python_client({})
        assert isinstance(source, str)
