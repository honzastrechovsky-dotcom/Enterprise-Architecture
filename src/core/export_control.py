"""Export control guard for Class IV / export-controlled data.

Prevents export-controlled technical information from entering AI prompts
without explicit approval. Class IV data requires a GTS export approval
check before it can be included in LLM context.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.core.classification import DataClassification

log = structlog.get_logger(__name__)


@dataclass
class ExportCheckResult:
    """Result of export control check."""

    allowed: bool
    blocked_documents: list[str]
    reason: str
    requires_gts_approval: bool


class ExportControlGuard:
    """Enforces export control restrictions on Class IV and flagged documents.

    Usage:
        guard = ExportControlGuard()
        result = guard.check_document(
            classification=DataClassification.CLASS_IV,
            metadata={"export_approval_id": "GTS-2024-001"},
        )
        if not result.allowed:
            raise HTTPException(403, detail=result.reason)
    """

    def check_document(
        self,
        classification: DataClassification,
        metadata: dict,
    ) -> ExportCheckResult:
        """Check whether a document can be accessed based on export controls.

        Class IV documents are export-controlled by definition and require
        explicit GTS approval. Documents can also be flagged as export-controlled
        via metadata.

        Args:
            classification: Document's data classification level
            metadata: Document metadata (may contain export_controlled flag or approval ID)

        Returns:
            ExportCheckResult indicating whether access is allowed
        """
        # Check if document is Class IV (export-controlled by definition)
        if classification == DataClassification.CLASS_IV:
            # Check for approval in metadata
            approval_id = metadata.get("export_approval_id")
            if approval_id:
                log.info(
                    "export_control.class_iv_approved",
                    approval_id=approval_id,
                    classification=classification,
                )
                return ExportCheckResult(
                    allowed=True,
                    blocked_documents=[],
                    reason=f"Class IV access approved via GTS approval {approval_id}",
                    requires_gts_approval=False,
                )

            log.warning(
                "export_control.class_iv_blocked",
                classification=classification,
                reason="no_approval",
            )
            return ExportCheckResult(
                allowed=False,
                blocked_documents=[],
                reason="Class IV data requires GTS export approval",
                requires_gts_approval=True,
            )

        # Check metadata flag for export-controlled content
        if metadata.get("export_controlled", False):
            approval_id = metadata.get("export_approval_id")
            if approval_id:
                log.info(
                    "export_control.metadata_approved",
                    approval_id=approval_id,
                    classification=classification,
                )
                return ExportCheckResult(
                    allowed=True,
                    blocked_documents=[],
                    reason=f"Export-controlled document approved via GTS approval {approval_id}",
                    requires_gts_approval=False,
                )

            log.warning(
                "export_control.metadata_blocked",
                classification=classification,
                export_controlled=True,
                reason="no_approval",
            )
            return ExportCheckResult(
                allowed=False,
                blocked_documents=[],
                reason="Export-controlled document requires GTS approval (GTS-GP-04)",
                requires_gts_approval=True,
            )

        # Not export-controlled
        return ExportCheckResult(
            allowed=True,
            blocked_documents=[],
            reason="Document is not export-controlled",
            requires_gts_approval=False,
        )

    def check_prompt(
        self,
        text: str,
        documents_used: list[dict],
    ) -> ExportCheckResult:
        """Check whether a prompt can proceed based on export controls.

        If any document in the context is export-controlled and not approved,
        the entire prompt is blocked.

        Args:
            text: The prompt text (not currently used for export control logic)
            documents_used: List of documents in context, each with:
                - document_id: str
                - filename: str
                - classification: DataClassification
                - metadata: dict

        Returns:
            ExportCheckResult for the prompt as a whole
        """
        blocked_docs: list[str] = []
        requires_approval = False

        for doc in documents_used:
            classification = doc.get("classification", DataClassification.CLASS_II)
            metadata = doc.get("metadata", {})
            filename = doc.get("filename", "unknown")

            result = self.check_document(classification, metadata)

            if not result.allowed:
                blocked_docs.append(filename)
                if result.requires_gts_approval:
                    requires_approval = True

        if blocked_docs:
            log.warning(
                "export_control.prompt_blocked",
                blocked_count=len(blocked_docs),
                blocked_documents=blocked_docs,
            )
            return ExportCheckResult(
                allowed=False,
                blocked_documents=blocked_docs,
                reason=f"Prompt blocked: {len(blocked_docs)} export-controlled document(s) without GTS approval",
                requires_gts_approval=requires_approval,
            )

        log.debug("export_control.prompt_allowed", document_count=len(documents_used))
        return ExportCheckResult(
            allowed=True,
            blocked_documents=[],
            reason="No export control restrictions",
            requires_gts_approval=False,
        )
