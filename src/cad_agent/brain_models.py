from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelProvider(Protocol):
    """Unified interface for OpenAI, DeepSeek, Claude, Gemini, Qwen, or local models."""

    name: str

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a Design JSON object for the supplied prompt and schema."""


class CandidatePlanProvider(Protocol):
    """Optional provider interface for model-neutral candidate planning.

    Providers return raw content only. Normalization, validation, authorization,
    routing, and CAD execution remain deterministic local responsibilities.
    """

    name: str
    model: str

    def generate_candidate_output(
        self,
        prompt: str,
        schema: dict[str, Any],
        repair_feedback: str | None = None,
    ) -> Any:
        """Return one candidate-plan JSON object or its raw JSON text."""


@dataclass(frozen=True)
class PlannedSkillStep:
    skill_key: str
    action: str
    reason: str
    inputs: dict[str, Any] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class BrainPlan:
    source_prompt: str
    design_json: dict[str, Any]
    skill_pipeline: list[PlannedSkillStep]
    model_provider: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_prompt": self.source_prompt,
            "design_json": self.design_json,
            "skill_pipeline": [
                {
                    "skill_key": step.skill_key,
                    "action": step.action,
                    "reason": step.reason,
                    "inputs": step.inputs,
                    "required": step.required,
                }
                for step in self.skill_pipeline
            ],
            "model_provider": self.model_provider,
        }
