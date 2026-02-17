"""SDK generator: produce typed client libraries from an OpenAPI spec.

Supported target languages:
  - Python (async httpx client with Pydantic models)
  - TypeScript (Fetch-based client with TypeScript interfaces)
  - Go (net/http client with struct types)

The generator uses Jinja2 templates stored alongside this module in
``src/sdk/templates/``.  Each template receives a cleaned ``spec``
context derived from the raw OpenAPI 3.x JSON/dict.

Usage::

    from src.sdk.generator import SDKGenerator

    generator = SDKGenerator()
    python_src = generator.generate_python_client(openapi_spec)
    ts_src     = generator.generate_typescript_client(openapi_spec)
    go_src     = generator.generate_go_client(openapi_spec)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Lazy Jinja2 import to avoid hard dependency at import time
try:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    _JINJA2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JINJA2_AVAILABLE = False


def _require_jinja2() -> None:
    if not _JINJA2_AVAILABLE:
        raise RuntimeError(
            "Jinja2 is required for SDK generation. "
            "Install it with: pip install jinja2"
        )


# ------------------------------------------------------------------ #
# Type mapping tables
# ------------------------------------------------------------------ #

_OA_TO_PYTHON: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict[str, Any]",
    "null": "None",
}

_OA_TO_TYPESCRIPT: dict[str, str] = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "array": "Array<unknown>",
    "object": "Record<string, unknown>",
    "null": "null",
}

_OA_TO_GO: dict[str, str] = {
    "string": "string",
    "integer": "int64",
    "number": "float64",
    "boolean": "bool",
    "array": "[]interface{}",
    "object": "map[string]interface{}",
    "null": "interface{}",
}


# ------------------------------------------------------------------ #
# Spec parsing helpers
# ------------------------------------------------------------------ #


def _extract_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a flat list of operation descriptors from the OpenAPI spec.

    Each descriptor contains everything a template needs to generate a
    single client method:
    - ``operation_id``   - camelCase method name
    - ``method``         - HTTP verb (get/post/put/delete/patch)
    - ``path``           - URL path string
    - ``summary``        - Short description
    - ``tags``           - List of tags
    - ``parameters``     - Path/query parameters
    - ``request_body``   - Parsed request body schema (or None)
    - ``response_schema``- Schema for the 2xx response (or None)
    """
    operations: list[dict[str, Any]] = []
    paths: dict[str, Any] = spec.get("paths", {})

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch", "head"}:
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId") or _generate_op_id(method, path)
            summary = operation.get("summary", "")
            tags = operation.get("tags", [])
            parameters = operation.get("parameters", [])
            request_body = _extract_request_body(operation, spec)
            response_schema = _extract_response_schema(operation, spec)

            operations.append(
                {
                    "operation_id": op_id,
                    "method": method.upper(),
                    "path": path,
                    "summary": summary,
                    "tags": tags,
                    "parameters": parameters,
                    "request_body": request_body,
                    "response_schema": response_schema,
                }
            )

    return operations


def _generate_op_id(method: str, path: str) -> str:
    """Derive a camelCase operation ID from method + path."""
    parts = [method] + [
        p for p in path.split("/") if p and not p.startswith("{")
    ]
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    # Strip non-alphanumeric chars
    return re.sub(r"[^a-zA-Z0-9]", "", camel)


def _extract_request_body(
    operation: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Return parsed request body info, or None if not present."""
    rb = operation.get("requestBody")
    if not rb:
        return None
    content = rb.get("content", {})
    json_content = content.get("application/json", {})
    schema = _resolve_ref(json_content.get("schema", {}), spec)
    return {
        "required": rb.get("required", False),
        "schema": schema,
        "description": rb.get("description", ""),
    }


def _extract_response_schema(
    operation: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the JSON schema for the primary 2xx response, or None."""
    responses = operation.get("responses", {})
    for code in ("200", "201", "202"):
        resp = responses.get(code)
        if resp is None:
            continue
        resp = _resolve_ref(resp, spec)
        content = resp.get("content", {})
        json_content = content.get("application/json", {})
        schema = _resolve_ref(json_content.get("schema", {}), spec)
        if schema:
            return schema
    return None


def _resolve_ref(obj: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve ``$ref`` pointers within the spec."""
    if not isinstance(obj, dict):
        return obj
    ref = obj.get("$ref")
    if ref and isinstance(ref, str) and ref.startswith("#/"):
        parts = ref.lstrip("#/").split("/")
        resolved: Any = spec
        for part in parts:
            resolved = resolved.get(part, {})
        return _resolve_ref(resolved, spec)
    return obj


def _extract_schemas(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the components/schemas map from the spec."""
    return spec.get("components", {}).get("schemas", {})


def _schema_to_python_type(schema: dict[str, Any]) -> str:
    """Map an OpenAPI schema to a Python type annotation string."""
    if not schema:
        return "Any"
    oa_type = schema.get("type", "object")
    if oa_type == "array":
        items = schema.get("items", {})
        item_type = _schema_to_python_type(items)
        return f"list[{item_type}]"
    ref = schema.get("$ref", "")
    if ref:
        return ref.split("/")[-1]
    return _OA_TO_PYTHON.get(oa_type, "Any")


def _schema_to_typescript_type(schema: dict[str, Any]) -> str:
    """Map an OpenAPI schema to a TypeScript type annotation string."""
    if not schema:
        return "unknown"
    oa_type = schema.get("type", "object")
    if oa_type == "array":
        items = schema.get("items", {})
        item_type = _schema_to_typescript_type(items)
        return f"Array<{item_type}>"
    ref = schema.get("$ref", "")
    if ref:
        return ref.split("/")[-1]
    return _OA_TO_TYPESCRIPT.get(oa_type, "unknown")


def _schema_to_go_type(schema: dict[str, Any]) -> str:
    """Map an OpenAPI schema to a Go type string."""
    if not schema:
        return "interface{}"
    oa_type = schema.get("type", "object")
    if oa_type == "array":
        items = schema.get("items", {})
        item_type = _schema_to_go_type(items)
        return f"[]{item_type}"
    ref = schema.get("$ref", "")
    if ref:
        return "*" + ref.split("/")[-1]
    return _OA_TO_GO.get(oa_type, "interface{}")


def _build_template_context(spec: dict[str, Any]) -> dict[str, Any]:
    """Produce the common Jinja2 context shared across all language templates."""
    info = spec.get("info", {})
    servers = spec.get("servers", [{}])
    base_url = servers[0].get("url", "https://api.example.com") if servers else "https://api.example.com"

    return {
        "title": info.get("title", "Enterprise Agent Platform"),
        "version": info.get("version", "1.0.0"),
        "description": info.get("description", ""),
        "base_url": base_url,
        "operations": _extract_operations(spec),
        "schemas": _extract_schemas(spec),
        "spec": spec,
        # Type helpers available in templates
        "python_type": _schema_to_python_type,
        "ts_type": _schema_to_typescript_type,
        "go_type": _schema_to_go_type,
    }


# ------------------------------------------------------------------ #
# SDKGenerator
# ------------------------------------------------------------------ #


class SDKGenerator:
    """Generate typed client SDKs from an OpenAPI 3.x specification.

    Each ``generate_*`` method accepts the raw OpenAPI spec as a dict and
    returns the generated source code as a string.
    """

    def __init__(self) -> None:
        _require_jinja2()
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        # Register custom filters
        self._env.filters["snake_case"] = _to_snake_case
        self._env.filters["pascal_case"] = _to_pascal_case
        self._env.filters["camel_case"] = _to_camel_case
        self._env.filters["repr"] = repr

    def generate_python_client(self, openapi_spec: dict[str, Any]) -> str:
        """Generate a Python async client from the OpenAPI spec.

        The generated client:
        - Uses ``httpx.AsyncClient`` for all requests
        - Provides typed async methods for every endpoint
        - Includes Pydantic v2 models for all request/response schemas
        - Handles Bearer JWT and X-API-Key authentication
        - Raises ``EAPAPIError`` on non-2xx responses

        Args:
            openapi_spec: Full OpenAPI 3.x specification dict.

        Returns:
            Python source code as a string.
        """
        context = _build_template_context(openapi_spec)
        template = self._env.get_template("python_client.py.j2")
        source = template.render(**context)
        log.info(
            "sdk.python_generated",
            operations=len(context["operations"]),
            schemas=len(context["schemas"]),
        )
        return source

    def generate_typescript_client(self, openapi_spec: dict[str, Any]) -> str:
        """Generate a TypeScript client from the OpenAPI spec.

        The generated client:
        - Uses the browser/Node ``fetch`` API
        - Provides typed async methods for every endpoint
        - Includes TypeScript interfaces for all schemas
        - Manages Bearer JWT and X-API-Key authentication
        - Throws ``EAPAPIError`` on non-2xx responses

        Args:
            openapi_spec: Full OpenAPI 3.x specification dict.

        Returns:
            TypeScript source code as a string.
        """
        context = _build_template_context(openapi_spec)
        template = self._env.get_template("typescript_client.ts.j2")
        source = template.render(**context)
        log.info(
            "sdk.typescript_generated",
            operations=len(context["operations"]),
            schemas=len(context["schemas"]),
        )
        return source

    def generate_go_client(self, openapi_spec: dict[str, Any]) -> str:
        """Generate a Go client from the OpenAPI spec.

        The generated client:
        - Uses ``net/http`` for all requests
        - Provides typed methods for every endpoint
        - Includes Go struct types for all schemas
        - Handles Bearer JWT and X-API-Key authentication
        - Returns typed errors on non-2xx responses

        Args:
            openapi_spec: Full OpenAPI 3.x specification dict.

        Returns:
            Go source code as a string.
        """
        context = _build_template_context(openapi_spec)
        template = self._env.get_template("go_client.go.j2")
        source = template.render(**context)
        log.info(
            "sdk.go_generated",
            operations=len(context["operations"]),
            schemas=len(context["schemas"]),
        )
        return source


# ------------------------------------------------------------------ #
# String helpers (exposed as Jinja2 filters)
# ------------------------------------------------------------------ #


def _to_snake_case(value: str) -> str:
    """Convert camelCase or PascalCase to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _to_pascal_case(value: str) -> str:
    """Convert snake_case or camelCase to PascalCase."""
    return "".join(w.capitalize() for w in re.split(r"[_\-\s]+", value))


def _to_camel_case(value: str) -> str:
    """Convert snake_case to camelCase."""
    parts = re.split(r"[_\-\s]+", value)
    return parts[0].lower() + "".join(w.capitalize() for w in parts[1:])
