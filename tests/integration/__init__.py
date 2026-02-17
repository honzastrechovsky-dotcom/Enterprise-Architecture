"""Integration tests package.

Integration tests use REAL database connections (either SQLite in-memory
or PostgreSQL when available) and real ASGI transport (not mocked).

Run with:
    pytest -m integration tests/integration/

Or exclude integration tests:
    pytest -m "not integration"
"""
