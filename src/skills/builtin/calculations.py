"""Calculations skill - engineering calculations with unit conversion.

This skill provides safe mathematical evaluation with engineering unit
conversion. Supports:
- Arithmetic expressions with operator precedence
- Unit conversion (metric/imperial, engineering units)
- Multi-step calculations with intermediate results
- Formula validation and error handling

Uses the calculator tool for safe expression evaluation without eval().
"""

from __future__ import annotations

from typing import Any

import structlog

from src.agent.tools import ToolContext, ToolGateway, ToolResult
from src.models.user import UserRole
from src.skills.base import BaseSkill, SkillContext, SkillManifest, SkillResult

log = structlog.get_logger(__name__)


class CalculationsSkill(BaseSkill):
    """Engineering calculations with unit conversion.

    Provides safe mathematical expression evaluation combined with
    engineering unit conversion. Handles common engineering units
    (pressure, temperature, flow, distance, force, etc.) and supports
    both metric and imperial systems.
    """

    # Unit conversion factors (base unit: SI)
    CONVERSIONS = {
        # Length
        "m_to_ft": 3.28084,
        "ft_to_m": 0.3048,
        "m_to_in": 39.3701,
        "in_to_m": 0.0254,
        "km_to_mi": 0.621371,
        "mi_to_km": 1.60934,
        # Pressure
        "pa_to_psi": 0.000145038,
        "psi_to_pa": 6894.76,
        "pa_to_bar": 0.00001,
        "bar_to_pa": 100000,
        "bar_to_psi": 14.5038,
        "psi_to_bar": 0.0689476,
        # Temperature (handled separately - not linear)
        # Flow
        "m3s_to_gpm": 15850.3,
        "gpm_to_m3s": 0.0000630902,
        "m3s_to_cfm": 2118.88,
        "cfm_to_m3s": 0.000471947,
        # Force
        "n_to_lbf": 0.224809,
        "lbf_to_n": 4.44822,
        # Mass
        "kg_to_lb": 2.20462,
        "lb_to_kg": 0.453592,
        # Energy
        "j_to_btu": 0.000947817,
        "btu_to_j": 1055.06,
    }

    def __init__(self, tool_gateway: ToolGateway | None = None) -> None:
        """Initialize calculations skill.

        Args:
            tool_gateway: Tool gateway for calculator tool (injected or created)
        """
        self._tool_gateway = tool_gateway or ToolGateway()

        self.manifest = SkillManifest(
            skill_id="calculations",
            name="Engineering Calculations",
            description=(
                "Perform engineering calculations with unit conversion. "
                "Supports arithmetic expressions and common engineering units "
                "(pressure, temperature, flow, distance, force, mass, energy)."
            ),
            version="1.0.0",
            capabilities=["calculations", "unit_conversion", "engineering_math"],
            required_tools=["calculator"],
            required_role=UserRole.VIEWER,
            classification_access=["class_i", "class_ii"],
            audit_required=False,
            parameters_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression to evaluate (e.g., '(5 + 3) * 2', '100 / 3.14')",
                    },
                    "units_from": {
                        "type": "string",
                        "description": "Source unit (e.g., 'psi', 'm', 'kg'). Leave empty for unitless calculation.",
                        "default": "",
                    },
                    "units_to": {
                        "type": "string",
                        "description": "Target unit (e.g., 'bar', 'ft', 'lb'). Leave empty for unitless calculation.",
                        "default": "",
                    },
                },
                "required": ["expression"],
            },
        )

    async def execute(self, params: dict[str, Any], context: SkillContext) -> SkillResult:
        """Execute calculation with optional unit conversion.

        Args:
            params: Validated parameters (expression, units_from, units_to)
            context: Runtime context with tenant, user, agent, RAG data

        Returns:
            SkillResult with calculation result and conversion details
        """
        expression = params["expression"]
        units_from = params.get("units_from", "").strip().lower()
        units_to = params.get("units_to", "").strip().lower()

        log.info(
            "skill.calculations.executing",
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            expression=expression,
            units_from=units_from,
            units_to=units_to,
        )

        try:
            # Step 1: Evaluate the expression using calculator tool
            calc_result = await self._evaluate_expression(expression, context)

            if not calc_result.success:
                return SkillResult(
                    success=False,
                    content="",
                    error=f"Calculation failed: {calc_result.error}",
                )

            result_value = calc_result.data.get("result")

            # Step 2: Apply unit conversion if requested
            if units_from and units_to:
                conversion_result = self._convert_units(result_value, units_from, units_to)

                if not conversion_result["success"]:
                    return SkillResult(
                        success=False,
                        content="",
                        error=conversion_result["error"],
                    )

                converted_value = conversion_result["value"]
                content = (
                    f"Calculation: {expression} = {result_value:,.4f} {units_from}\n"
                    f"Converted: {converted_value:,.4f} {units_to}\n"
                    f"Conversion factor: {conversion_result['factor']}"
                )

                data = {
                    "expression": expression,
                    "result_raw": result_value,
                    "result_converted": converted_value,
                    "units_from": units_from,
                    "units_to": units_to,
                    "conversion_factor": conversion_result["factor"],
                }
            else:
                # No unit conversion
                content = f"Calculation: {expression} = {result_value:,.4f}"
                data = {
                    "expression": expression,
                    "result": result_value,
                }

            log.info(
                "skill.calculations.completed",
                tenant_id=str(context.tenant_id),
                result=result_value,
                converted=units_to if units_to else None,
            )

            return SkillResult(
                success=True,
                content=content,
                data=data,
                metadata={
                    "skill_id": self.manifest.skill_id,
                    "version": self.manifest.version,
                    "agent_id": context.agent_id,
                },
            )

        except Exception as exc:
            log.error(
                "skill.calculations.failed",
                tenant_id=str(context.tenant_id),
                error=str(exc),
            )
            return SkillResult(
                success=False,
                content="",
                error=f"Calculation failed: {exc}",
            )

    async def _evaluate_expression(self, expression: str, context: SkillContext) -> ToolResult:
        """Evaluate mathematical expression using calculator tool."""
        tool_context = ToolContext(
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            user_role=context.user_role,
        )

        return await self._tool_gateway.execute(
            tool_name="calculator",
            params={"expression": expression},
            context=tool_context,
        )

    def _convert_units(self, value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
        """Convert value from one unit to another.

        Args:
            value: Numeric value to convert
            from_unit: Source unit
            to_unit: Target unit

        Returns:
            Dict with success, value, factor, or error
        """
        # Special handling for temperature (non-linear conversion)
        if from_unit in ["c", "f", "k"] and to_unit in ["c", "f", "k"]:
            return self._convert_temperature(value, from_unit, to_unit)

        # Look up conversion factor
        conversion_key = f"{from_unit}_to_{to_unit}"
        factor = self.CONVERSIONS.get(conversion_key)

        if factor is None:
            return {
                "success": False,
                "error": f"Unsupported conversion: {from_unit} to {to_unit}. Supported conversions: {list(self.CONVERSIONS.keys())}",
            }

        converted = value * factor

        return {
            "success": True,
            "value": converted,
            "factor": factor,
        }

    def _convert_temperature(self, value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
        """Convert temperature between C, F, K.

        Args:
            value: Temperature value
            from_unit: c, f, or k
            to_unit: c, f, or k

        Returns:
            Dict with success, value, factor (N/A for temp), or error
        """
        # Convert to Celsius first
        if from_unit == "c":
            celsius = value
        elif from_unit == "f":
            celsius = (value - 32) * 5 / 9
        elif from_unit == "k":
            celsius = value - 273.15
        else:
            return {"success": False, "error": f"Unknown temperature unit: {from_unit}"}

        # Convert from Celsius to target
        if to_unit == "c":
            result = celsius
        elif to_unit == "f":
            result = celsius * 9 / 5 + 32
        elif to_unit == "k":
            result = celsius + 273.15
        else:
            return {"success": False, "error": f"Unknown temperature unit: {to_unit}"}

        return {
            "success": True,
            "value": result,
            "factor": "N/A (non-linear conversion)",
        }
