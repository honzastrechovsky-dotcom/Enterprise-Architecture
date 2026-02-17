"""Enterprise connectors for external system integration.

This module provides read-only connectors to enterprise systems:
- SAP (ERP data: purchase orders, inventory, materials)
- MES (Manufacturing Execution System: production, quality, machine status)

All connectors:
- Are read-only by default (no writes without explicit approval)
- Enforce tenant isolation
- Include universal audit logging
- Support connection pooling and health checks
- Have configurable caching with 5-minute TTL

Production-ready connectors for enterprise system integration.
"""

from __future__ import annotations

from src.connectors.approval import ToolApprovalWorkflow
from src.connectors.base import (
    BaseConnector,
    ConnectorConfig,
    ConnectorResult,
)
from src.connectors.cache import ConnectorCache
from src.connectors.mes import MESConnector
from src.connectors.sap import SAPConnector
from src.connectors.sql_guard import SQLGuard

__all__ = [
    "BaseConnector",
    "ConnectorConfig",
    "ConnectorResult",
    "SAPConnector",
    "MESConnector",
    "ConnectorCache",
    "SQLGuard",
    "ToolApprovalWorkflow",
]
