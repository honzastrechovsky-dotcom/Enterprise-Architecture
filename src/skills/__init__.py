"""Skills framework - modular capabilities for agents.

Skills are the enterprise equivalent of composable capabilities. Each skill
is self-describing (manifest), access-controlled (role requirements), and
auditable (execution logging). Agents invoke skills to accomplish complex
tasks that go beyond simple tool calls.
"""

from __future__ import annotations

from src.skills.base import (
    BaseSkill,
    SkillContext,
    SkillManifest,
    SkillResult,
)
from src.skills.registry import SkillRegistry, get_skill_registry

__all__ = [
    "BaseSkill",
    "SkillContext",
    "SkillManifest",
    "SkillResult",
    "SkillRegistry",
    "get_skill_registry",
]
