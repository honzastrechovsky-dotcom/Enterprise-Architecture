"""Built-in skills - auto-registration of core capabilities.

This module automatically registers all built-in skills with the global
skill registry when imported. Built-in skills provide:
- Document analysis and comparison
- Procedure lookup with safety guidance
- Engineering calculations with unit conversion
- Report generation from multiple sources

Import this module to make all built-in skills available to agents.
"""

from __future__ import annotations

import structlog

from src.skills.builtin.calculations import CalculationsSkill
from src.skills.builtin.document_analysis import DocumentAnalysisSkill
from src.skills.builtin.procedure_lookup import ProcedureLookupSkill
from src.skills.builtin.report_generation import ReportGenerationSkill
from src.skills.registry import get_skill_registry

log = structlog.get_logger(__name__)


def register_builtin_skills() -> None:
    """Register all built-in skills with the global registry.

    This function is called automatically when this module is imported.
    Skills can also be registered individually for testing or custom
    configurations.
    """
    registry = get_skill_registry()

    # Create and register all built-in skills
    skills = [
        DocumentAnalysisSkill(),
        ProcedureLookupSkill(),
        CalculationsSkill(),
        ReportGenerationSkill(),
    ]

    for skill in skills:
        try:
            registry.register(skill)
        except ValueError as exc:
            # Skill already registered (e.g., in tests with multiple imports)
            log.debug("builtin_skills.already_registered", skill_id=skill.manifest.skill_id, error=str(exc))

    log.info("builtin_skills.registered", count=len(skills), skill_ids=[s.manifest.skill_id for s in skills])


# Auto-register on import
register_builtin_skills()


__all__ = [
    "DocumentAnalysisSkill",
    "ProcedureLookupSkill",
    "CalculationsSkill",
    "ReportGenerationSkill",
    "register_builtin_skills",
]
