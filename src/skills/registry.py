"""Skill registry - discovery and access control for skills.

The registry maintains a catalog of all available skills and provides
filtered access based on user role and capabilities. Mirrors the pattern
from agent/registry.py with skill-specific concerns.
"""

from __future__ import annotations

import structlog

from src.models.user import UserRole
from src.skills.base import BaseSkill, SkillManifest

log = structlog.get_logger(__name__)


class SkillRegistry:
    """Registry for discovering and accessing skills.

    This is a singleton that maintains the catalog of all available skills.
    Agents query this registry to find skills that match required capabilities.

    Features:
    - Role-based access filtering
    - Capability-based discovery
    - Classification-level enforcement
    - Audit-required skill tracking
    """

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        """Register a skill in the registry.

        Args:
            skill: The skill instance to register

        Raises:
            ValueError: If a skill with this ID is already registered
        """
        if skill.manifest.skill_id in self._skills:
            raise ValueError(
                f"Skill '{skill.manifest.skill_id}' is already registered. "
                "Use a unique skill_id."
            )

        self._skills[skill.manifest.skill_id] = skill
        log.info(
            "registry.skill_registered",
            skill_id=skill.manifest.skill_id,
            name=skill.manifest.name,
            capabilities=skill.manifest.capabilities,
            required_role=skill.manifest.required_role,
            audit_required=skill.manifest.audit_required,
        )

    def get(self, skill_id: str) -> BaseSkill | None:
        """Get a skill by ID.

        Args:
            skill_id: The unique identifier for the skill

        Returns:
            BaseSkill instance if found, None otherwise
        """
        return self._skills.get(skill_id)

    def list_skills(self, user_role: UserRole | None = None) -> list[SkillManifest]:
        """List all registered skills, optionally filtered by role access.

        Args:
            user_role: If provided, only return skills this role can access

        Returns:
            List of skill manifests the user can access
        """
        if user_role is None:
            return [skill.manifest for skill in self._skills.values()]

        from src.core.policy import _role_level
        user_level = _role_level(user_role)

        return [
            skill.manifest
            for skill in self._skills.values()
            if _role_level(skill.manifest.required_role) <= user_level
        ]

    def find_by_capability(self, capability: str, user_role: UserRole | None = None) -> list[BaseSkill]:
        """Find all skills that provide a specific capability.

        Args:
            capability: The capability tag to search for
            user_role: If provided, filter by role access

        Returns:
            List of skills that have this capability
        """
        candidates = [
            skill for skill in self._skills.values()
            if capability in skill.manifest.capabilities
        ]

        if user_role is None:
            return candidates

        from src.core.policy import _role_level
        user_level = _role_level(user_role)

        return [
            skill for skill in candidates
            if _role_level(skill.manifest.required_role) <= user_level
        ]

    def find_by_capabilities(self, capabilities: list[str], user_role: UserRole | None = None) -> list[BaseSkill]:
        """Find skills that provide ALL of the specified capabilities.

        Args:
            capabilities: List of capability tags (skill must have ALL)
            user_role: If provided, filter by role access

        Returns:
            List of skills that have all requested capabilities
        """
        candidates = [
            skill for skill in self._skills.values()
            if all(cap in skill.manifest.capabilities for cap in capabilities)
        ]

        if user_role is None:
            return candidates

        from src.core.policy import _role_level
        user_level = _role_level(user_role)

        return [
            skill for skill in candidates
            if _role_level(skill.manifest.required_role) <= user_level
        ]

    def get_audit_required_skills(self) -> list[str]:
        """Return skill IDs that require audit logging.

        Returns:
            List of skill IDs where manifest.audit_required=True
        """
        return [
            skill.manifest.skill_id
            for skill in self._skills.values()
            if skill.manifest.audit_required
        ]

    def clear(self) -> None:
        """Clear all registered skills. Used for testing."""
        self._skills.clear()
        log.debug("registry.cleared")


# Module-level singleton instance
_registry = SkillRegistry()


def get_skill_registry() -> SkillRegistry:
    """Get the global skill registry instance."""
    return _registry
