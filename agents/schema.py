"""
Strict JSON schema validation for LLM outputs.
"""

from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class Action:
    """A single tool invocation."""
    tool: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    """Validated LLM output."""
    reasoning: Optional[str]
    actions: list[Action]


class ValidationError(Exception):
    """Schema validation failed."""
    pass


def validate_llm_response(data: Any) -> LLMResponse:
    """
    Validate LLM output against strict schema.

    Raises:
        ValidationError: If data doesn't match schema
    """
    if not isinstance(data, dict):
        raise ValidationError(f"Expected dict, got {type(data).__name__}")

    # Check for unknown fields
    allowed_fields = {"reasoning", "actions"}
    unknown = set(data.keys()) - allowed_fields
    if unknown:
        raise ValidationError(f"Unknown fields: {unknown}")

    # Validate reasoning (optional)
    reasoning = data.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValidationError(f"reasoning must be string, got {type(reasoning).__name__}")

    # Validate actions (required)
    if "actions" not in data:
        raise ValidationError("Missing required field: actions")

    actions_raw = data["actions"]
    if not isinstance(actions_raw, list):
        raise ValidationError(f"actions must be list, got {type(actions_raw).__name__}")

    # Validate each action
    actions = []
    for i, action_raw in enumerate(actions_raw):
        if not isinstance(action_raw, dict):
            raise ValidationError(f"Action {i} must be dict, got {type(action_raw).__name__}")

        if "tool" not in action_raw:
            raise ValidationError(f"Action {i} missing required field: tool")

        if "args" not in action_raw:
            raise ValidationError(f"Action {i} missing required field: args")

        tool = action_raw["tool"]
        args = action_raw["args"]

        if not isinstance(tool, str):
            raise ValidationError(f"Action {i} tool must be string, got {type(tool).__name__}")

        if not isinstance(args, dict):
            raise ValidationError(f"Action {i} args must be dict, got {type(args).__name__}")

        # Check for unknown fields in action
        unknown_action_fields = set(action_raw.keys()) - {"tool", "args"}
        if unknown_action_fields:
            raise ValidationError(f"Action {i} has unknown fields: {unknown_action_fields}")

        actions.append(Action(tool=tool, args=args))

    return LLMResponse(reasoning=reasoning, actions=actions)
