"""ORM models package.

Import all models here so that SQLAlchemy's metadata is fully populated
when Alembic runs autogenerate. The order of imports matters for foreign
key resolution.
"""

from src.models.analytics import DailySummary, MetricType, UsageMetric
from src.models.api_key import APIKey
from src.models.audit import AuditLog
from src.models.conversation import Conversation, Message
from src.models.document import Document, DocumentChunk
from src.models.feedback import (
    DatasetStatus,
    FeedbackRating,
    FinetuningDataset,
    FinetuningRecord,
    ResponseFeedback,
)
from src.models.fine_tuning import FineTuningJobRecord
from src.models.gdpr_request import GDPRRequestRecord
from src.models.idp_config import IdPConfig
from src.models.ingestion import FileType, IngestionJob, IngestionStatus
from src.models.memory import Memory
from src.models.plan import PlanRecord
from src.models.plugin import PluginRegistration
from src.models.tenant import Tenant
from src.models.tenant_settings import TenantSettings
from src.models.token_budget import (
    RoutingDecisionRecord,
    TokenBudgetRecord,
    TokenUsageRecord,
)
from src.models.trace import AgentStep, AgentTrace, StepType, TraceStatus
from src.models.user import User
from src.models.monitoring import (
    AlertSeverity,
    AlertStatus,
    AlertThreshold,
    MonitoringAlert,
)
from src.models.write_operation import WriteOperationRecord

__all__ = [
    "Tenant",
    "User",
    "Conversation",
    "Message",
    "Document",
    "DocumentChunk",
    "Memory",
    "AuditLog",
    "AgentTrace",
    "AgentStep",
    "TraceStatus",
    "StepType",
    "ResponseFeedback",
    "FinetuningDataset",
    "FinetuningRecord",
    "FeedbackRating",
    "DatasetStatus",
    "FineTuningJobRecord",
    "GDPRRequestRecord",
    "IdPConfig",
    "UsageMetric",
    "DailySummary",
    "MetricType",
    "PluginRegistration",
    "IngestionJob",
    "FileType",
    "IngestionStatus",
    "APIKey",
    "TenantSettings",
    "TokenBudgetRecord",
    "TokenUsageRecord",
    "RoutingDecisionRecord",
    "PlanRecord",
    "WriteOperationRecord",
    "AlertThreshold",
    "MonitoringAlert",
    "AlertSeverity",
    "AlertStatus",
]
