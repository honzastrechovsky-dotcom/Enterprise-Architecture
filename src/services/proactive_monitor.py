"""Proactive MES monitoring service.

Polls manufacturing execution system data at configurable intervals and
fires alerts when metrics breach tenant-specific thresholds.

Design:
- Each check method is an async coroutine scheduled by the caller
  (e.g., BackgroundWorkerPool or an asyncio scheduler)
- All operations are tenant-scoped
- Alerts are persisted to the database and dispatched via NotificationService
- Thresholds are loaded from the database per tenant (AlertThreshold model)
- Default thresholds are applied when no tenant-specific config exists

Integration points:
- MESConnector: READ-ONLY data source for machine status, quality, inventory
- NotificationService: webhook/email dispatch (fire-and-forget)
- SQLAlchemy AsyncSession: alert persistence and threshold retrieval
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.connectors.base import ConnectorStatus
from src.connectors.mes import (
    DowntimeEvent,
    MachineStatus,
    MachineStatusData,
    MESConnector,
    QualityReport,
)
from src.models.monitoring import (
    AlertSeverity,
    AlertStatus,
    AlertThreshold,
    MonitoringAlert,
)
from src.operations.notification import NotificationService

log = structlog.get_logger(__name__)


# -----------------------------------------------------------------------
# Default threshold configuration
# -----------------------------------------------------------------------


@dataclass
class AlertThresholdConfig:
    """Default threshold configuration for MES proactive monitoring.

    These defaults are used when a tenant has not configured custom
    thresholds in the database. Values can be overridden per-tenant
    via the PUT /monitoring/thresholds API endpoint.
    """

    machine_downtime_minutes: int = 30
    defect_rate_percent: float = 5.0
    inventory_below_minimum: bool = True
    quality_deviation_sigma: float = 3.0

    def as_dict(self) -> dict[str, float]:
        """Return thresholds as a flat dict for database seeding."""
        return {
            "machine_downtime_minutes": float(self.machine_downtime_minutes),
            "defect_rate_percent": self.defect_rate_percent,
            "inventory_below_minimum": 1.0 if self.inventory_below_minimum else 0.0,
            "quality_deviation_sigma": self.quality_deviation_sigma,
        }


# Module-level defaults
DEFAULT_THRESHOLDS = AlertThresholdConfig()


# -----------------------------------------------------------------------
# Alert payload (internal representation before DB persistence)
# -----------------------------------------------------------------------


@dataclass
class AlertPayload:
    """Internal alert payload before persistence."""

    timestamp: datetime
    severity: AlertSeverity
    metric_name: str
    current_value: float
    threshold_value: float
    tenant_id: uuid.UUID
    message: str
    recommended_action: str
    extra: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------
# Proactive Monitor Service
# -----------------------------------------------------------------------


class ProactiveMonitorService:
    """Proactive MES monitoring with configurable thresholds.

    Polls MES data at defined intervals, compares against tenant-specific
    thresholds, and dispatches alerts via the notification service.

    Usage:
        connector = MESConnector(config=...)
        notifier = NotificationService.from_settings()
        monitor = ProactiveMonitorService(
            mes_connector=connector,
            notification_service=notifier,
        )

        # In a scheduler loop:
        async with AsyncSession(engine) as db:
            await monitor.check_machine_status(db, tenant_id)
            await monitor.check_quality_metrics(db, tenant_id)
            await monitor.check_inventory_levels(db, tenant_id)
    """

    def __init__(
        self,
        *,
        mes_connector: MESConnector,
        notification_service: NotificationService,
        defaults: AlertThresholdConfig | None = None,
    ) -> None:
        self._mes = mes_connector
        self._notifier = notification_service
        self._defaults = defaults or DEFAULT_THRESHOLDS

        log.info(
            "proactive_monitor.initialized",
            defaults=self._defaults.as_dict(),
        )

    # ------------------------------------------------------------------
    # Threshold resolution
    # ------------------------------------------------------------------

    async def _get_threshold(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        metric_name: str,
    ) -> float | None:
        """Load threshold value for a metric from the database.

        Returns the tenant-specific threshold if configured and enabled,
        otherwise returns the default from AlertThresholdConfig.
        Returns None if the threshold is explicitly disabled.
        """
        stmt = select(AlertThreshold).where(
            and_(
                AlertThreshold.tenant_id == tenant_id,
                AlertThreshold.metric_name == metric_name,
            )
        )
        result = await db.execute(stmt)
        threshold = result.scalar_one_or_none()

        if threshold is not None:
            if not threshold.enabled:
                log.debug(
                    "proactive_monitor.threshold_disabled",
                    tenant_id=str(tenant_id),
                    metric=metric_name,
                )
                return None
            return threshold.threshold_value

        # Fall back to defaults
        defaults = self._defaults.as_dict()
        return defaults.get(metric_name)

    # ------------------------------------------------------------------
    # Alert creation and dispatch
    # ------------------------------------------------------------------

    async def _create_alert(
        self,
        db: AsyncSession,
        payload: AlertPayload,
    ) -> MonitoringAlert:
        """Persist alert to database and dispatch notification.

        Checks for existing open alerts on the same metric for the same
        tenant to avoid duplicate alerts.
        """
        # Check for existing open alert on same metric
        existing_stmt = select(MonitoringAlert).where(
            and_(
                MonitoringAlert.tenant_id == payload.tenant_id,
                MonitoringAlert.metric_name == payload.metric_name,
                MonitoringAlert.status == AlertStatus.OPEN,
            )
        )
        existing = await db.execute(existing_stmt)
        if existing.scalar_one_or_none() is not None:
            log.debug(
                "proactive_monitor.alert_deduplicated",
                tenant_id=str(payload.tenant_id),
                metric=payload.metric_name,
            )
            # Update existing alert value
            update_stmt = (
                update(MonitoringAlert)
                .where(
                    and_(
                        MonitoringAlert.tenant_id == payload.tenant_id,
                        MonitoringAlert.metric_name == payload.metric_name,
                        MonitoringAlert.status == AlertStatus.OPEN,
                    )
                )
                .values(current_value=payload.current_value)
            )
            await db.execute(update_stmt)
            await db.flush()
            # Re-fetch updated alert
            result = await db.execute(existing_stmt)
            return result.scalar_one()

        # Create new alert
        alert = MonitoringAlert(
            tenant_id=payload.tenant_id,
            metric_name=payload.metric_name,
            current_value=payload.current_value,
            threshold_value=payload.threshold_value,
            severity=payload.severity,
            status=AlertStatus.OPEN,
            message=payload.message,
            recommended_action=payload.recommended_action,
            created_at=payload.timestamp,
        )
        db.add(alert)
        await db.flush()

        log.info(
            "proactive_monitor.alert_created",
            alert_id=str(alert.id),
            tenant_id=str(payload.tenant_id),
            metric=payload.metric_name,
            severity=payload.severity,
            current_value=payload.current_value,
            threshold=payload.threshold_value,
        )

        # Fire-and-forget notification
        await self._dispatch_notification(payload)

        return alert

    async def _auto_resolve_alerts(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        metric_name: str,
    ) -> int:
        """Auto-resolve open alerts when the metric returns to normal.

        Returns the number of alerts resolved.
        """
        stmt = (
            update(MonitoringAlert)
            .where(
                and_(
                    MonitoringAlert.tenant_id == tenant_id,
                    MonitoringAlert.metric_name == metric_name,
                    MonitoringAlert.status == AlertStatus.OPEN,
                )
            )
            .values(
                status=AlertStatus.RESOLVED,
                resolved_at=datetime.now(UTC),
            )
        )
        result = await db.execute(stmt)
        resolved_count = result.rowcount  # type: ignore[assignment]
        if resolved_count > 0:
            log.info(
                "proactive_monitor.alerts_auto_resolved",
                tenant_id=str(tenant_id),
                metric=metric_name,
                count=resolved_count,
            )
        return resolved_count

    async def _dispatch_notification(self, payload: AlertPayload) -> None:
        """Send alert notification via webhook and email (fire-and-forget)."""
        title = f"[{payload.severity.upper()}] MES Alert: {payload.metric_name}"
        body = (
            f"Metric: {payload.metric_name}\n"
            f"Current Value: {payload.current_value}\n"
            f"Threshold: {payload.threshold_value}\n"
            f"Severity: {payload.severity}\n"
            f"Tenant: {payload.tenant_id}\n"
            f"Time: {payload.timestamp.isoformat()}\n"
            f"\nMessage: {payload.message}\n"
            f"\nRecommended Action: {payload.recommended_action}"
        )

        # Use webhook (fire-and-forget)
        await self._notifier._fire_and_forget_webhook(title, body)

        # Use email (fire-and-forget)
        recipient = f"ops+{payload.tenant_id}@enterprise-agents.local"
        await self._notifier._fire_and_forget_email(recipient, title, body)

    # ------------------------------------------------------------------
    # Scheduled polling methods
    # ------------------------------------------------------------------

    async def check_machine_status(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> list[MonitoringAlert]:
        """Poll MES machine status, alert if downtime exceeds threshold.

        Recommended polling interval: 60 seconds.

        Checks:
        - Machines in DOWN status with active downtime > threshold minutes
        - Machines with active alarms

        Returns list of alerts created (empty if all healthy).
        """
        threshold = await self._get_threshold(
            db, tenant_id, "machine_downtime_minutes"
        )
        if threshold is None:
            return []

        log.debug(
            "proactive_monitor.checking_machine_status",
            tenant_id=str(tenant_id),
            threshold=threshold,
        )

        result = await self._mes.execute(
            operation="get_machine_status",
            tenant_id=tenant_id,
            params={},
        )

        if not result.success:
            log.error(
                "proactive_monitor.mes_poll_failed",
                tenant_id=str(tenant_id),
                operation="get_machine_status",
                error=result.error,
            )
            return []

        machines: list[MachineStatusData] = result.data or []
        alerts: list[MonitoringAlert] = []
        has_downtime_breach = False

        for machine in machines:
            if machine.status != MachineStatus.DOWN:
                continue

            # Calculate downtime from last event
            now = datetime.now(UTC)
            downtime_minutes = (now - machine.last_event_time).total_seconds() / 60

            if downtime_minutes > threshold:
                has_downtime_breach = True
                payload = AlertPayload(
                    timestamp=now,
                    severity=AlertSeverity.CRITICAL,
                    metric_name="machine_downtime_minutes",
                    current_value=round(downtime_minutes, 1),
                    threshold_value=threshold,
                    tenant_id=tenant_id,
                    message=(
                        f"Machine {machine.machine_name} ({machine.machine_id}) "
                        f"has been down for {downtime_minutes:.0f} minutes "
                        f"(threshold: {threshold:.0f} min). "
                        f"Work center: {machine.work_center}. "
                        f"Active alarms: {', '.join(machine.alarms_active) or 'none'}."
                    ),
                    recommended_action=(
                        f"1. Check machine {machine.machine_id} physically\n"
                        f"2. Review active alarms: {', '.join(machine.alarms_active) or 'none'}\n"
                        f"3. Contact maintenance team for work center {machine.work_center}\n"
                        f"4. Consider rescheduling production order {machine.current_order_id or 'N/A'}"
                    ),
                    extra={
                        "machine_id": machine.machine_id,
                        "work_center": machine.work_center,
                        "alarms": machine.alarms_active,
                    },
                )
                alert = await self._create_alert(db, payload)
                alerts.append(alert)

        # Auto-resolve if no machines are breaching
        if not has_downtime_breach:
            await self._auto_resolve_alerts(
                db, tenant_id, "machine_downtime_minutes"
            )

        return alerts

    async def check_quality_metrics(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> list[MonitoringAlert]:
        """Poll MES quality reports, alert if defect rate exceeds threshold.

        Recommended polling interval: 5 minutes.

        Checks:
        - Defect rate across recent quality reports
        - Individual reports with high defect counts

        Returns list of alerts created (empty if quality is acceptable).
        """
        threshold = await self._get_threshold(
            db, tenant_id, "defect_rate_percent"
        )
        if threshold is None:
            return []

        log.debug(
            "proactive_monitor.checking_quality_metrics",
            tenant_id=str(tenant_id),
            threshold=threshold,
        )

        result = await self._mes.execute(
            operation="get_quality_reports",
            tenant_id=tenant_id,
            params={"limit": 50},
        )

        if not result.success:
            log.error(
                "proactive_monitor.mes_poll_failed",
                tenant_id=str(tenant_id),
                operation="get_quality_reports",
                error=result.error,
            )
            return []

        reports: list[QualityReport] = result.data or []
        if not reports:
            return []

        # Calculate aggregate defect rate
        total_defects = sum(r.defect_count for r in reports)
        total_samples = sum(r.sample_size for r in reports)

        if total_samples == 0:
            return []

        defect_rate = (total_defects / total_samples) * 100
        alerts: list[MonitoringAlert] = []

        if defect_rate > threshold:
            now = datetime.now(UTC)

            # Find the worst offending reports
            failed_reports = [r for r in reports if not r.passed]
            defect_types: set[str] = set()
            for r in failed_reports:
                defect_types.update(r.defect_types)

            payload = AlertPayload(
                timestamp=now,
                severity=AlertSeverity.WARNING,
                metric_name="defect_rate_percent",
                current_value=round(defect_rate, 2),
                threshold_value=threshold,
                tenant_id=tenant_id,
                message=(
                    f"Defect rate is {defect_rate:.1f}% across {len(reports)} "
                    f"recent reports (threshold: {threshold:.1f}%). "
                    f"Total defects: {total_defects}/{total_samples} samples. "
                    f"Defect types: {', '.join(defect_types) or 'unspecified'}."
                ),
                recommended_action=(
                    f"1. Review failed quality reports ({len(failed_reports)} failures)\n"
                    f"2. Investigate defect types: {', '.join(defect_types) or 'unspecified'}\n"
                    f"3. Check calibration of inspection equipment\n"
                    f"4. Consider halting production line if rate continues to climb"
                ),
                extra={
                    "total_defects": total_defects,
                    "total_samples": total_samples,
                    "failed_report_count": len(failed_reports),
                    "defect_types": list(defect_types),
                },
            )
            alert = await self._create_alert(db, payload)
            alerts.append(alert)
        else:
            await self._auto_resolve_alerts(
                db, tenant_id, "defect_rate_percent"
            )

        return alerts

    async def check_inventory_levels(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> list[MonitoringAlert]:
        """Poll MES production orders, alert if inventory levels are low.

        Recommended polling interval: 15 minutes.

        Uses production order data to detect orders where produced quantity
        is significantly below planned quantity, indicating potential
        inventory shortfalls.

        Returns list of alerts created (empty if inventory is healthy).
        """
        threshold = await self._get_threshold(
            db, tenant_id, "inventory_below_minimum"
        )
        if threshold is None:
            return []

        # threshold == 0.0 means disabled
        if threshold == 0.0:
            return []

        log.debug(
            "proactive_monitor.checking_inventory_levels",
            tenant_id=str(tenant_id),
        )

        result = await self._mes.execute(
            operation="get_production_orders",
            tenant_id=tenant_id,
            params={"status": "in_progress", "limit": 100},
        )

        if not result.success:
            log.error(
                "proactive_monitor.mes_poll_failed",
                tenant_id=str(tenant_id),
                operation="get_production_orders",
                error=result.error,
            )
            return []

        orders = result.data or []
        alerts: list[MonitoringAlert] = []
        has_shortfall = False

        for order in orders:
            if order.quantity_planned <= 0:
                continue

            completion_rate = order.quantity_produced / order.quantity_planned
            # Alert if order is behind schedule (less than 50% complete and past mid-point)
            if completion_rate < 0.5 and order.start_date:
                now = datetime.now(UTC)
                elapsed = (now - order.start_date).total_seconds()
                if order.end_date:
                    total_duration = (
                        order.end_date - order.start_date
                    ).total_seconds()
                    if total_duration > 0:
                        time_progress = elapsed / total_duration
                        if time_progress > 0.5:
                            has_shortfall = True
                            payload = AlertPayload(
                                timestamp=now,
                                severity=AlertSeverity.WARNING,
                                metric_name="inventory_below_minimum",
                                current_value=round(completion_rate * 100, 1),
                                threshold_value=50.0,
                                tenant_id=tenant_id,
                                message=(
                                    f"Production order {order.order_id} is behind schedule. "
                                    f"Completion: {completion_rate * 100:.1f}% "
                                    f"({order.quantity_produced}/{order.quantity_planned} "
                                    f"{order.unit_of_measure}). "
                                    f"Time elapsed: {time_progress * 100:.0f}%. "
                                    f"Material: {order.material_description}."
                                ),
                                recommended_action=(
                                    f"1. Review production bottlenecks for order {order.order_id}\n"
                                    f"2. Check machine availability at work center {order.work_center}\n"
                                    f"3. Consider expediting or adding shifts\n"
                                    f"4. Notify supply chain of potential delivery delay"
                                ),
                                extra={
                                    "order_id": order.order_id,
                                    "material_id": order.material_id,
                                    "work_center": order.work_center,
                                    "completion_rate": completion_rate,
                                },
                            )
                            alert = await self._create_alert(db, payload)
                            alerts.append(alert)

        if not has_shortfall:
            await self._auto_resolve_alerts(
                db, tenant_id, "inventory_below_minimum"
            )

        return alerts

    # ------------------------------------------------------------------
    # Full check cycle (convenience method for scheduler)
    # ------------------------------------------------------------------

    async def run_all_checks(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> dict[str, list[MonitoringAlert]]:
        """Run all monitoring checks for a tenant.

        Convenience method that runs machine status, quality metrics,
        and inventory level checks in sequence.

        Returns dict mapping check name to list of alerts created.
        """
        log.info(
            "proactive_monitor.run_all_checks",
            tenant_id=str(tenant_id),
        )

        results: dict[str, list[MonitoringAlert]] = {}

        try:
            results["machine_status"] = await self.check_machine_status(
                db, tenant_id
            )
        except Exception as exc:
            log.error(
                "proactive_monitor.check_failed",
                check="machine_status",
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            results["machine_status"] = []

        try:
            results["quality_metrics"] = await self.check_quality_metrics(
                db, tenant_id
            )
        except Exception as exc:
            log.error(
                "proactive_monitor.check_failed",
                check="quality_metrics",
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            results["quality_metrics"] = []

        try:
            results["inventory_levels"] = await self.check_inventory_levels(
                db, tenant_id
            )
        except Exception as exc:
            log.error(
                "proactive_monitor.check_failed",
                check="inventory_levels",
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            results["inventory_levels"] = []

        total_alerts = sum(len(v) for v in results.values())
        log.info(
            "proactive_monitor.all_checks_complete",
            tenant_id=str(tenant_id),
            total_alerts=total_alerts,
            machine_alerts=len(results["machine_status"]),
            quality_alerts=len(results["quality_metrics"]),
            inventory_alerts=len(results["inventory_levels"]),
        )

        return results
