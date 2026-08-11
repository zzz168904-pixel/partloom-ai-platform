from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..pipeline import AgentPipelineRunner
from ..registry import CADAgentSkillManager


class PipelineExecutorTool:
    """Agents tool wrapper that only calls the existing Pipeline Runner."""

    def __init__(
        self,
        output_root: Path,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        execute_real_skills: bool = False,
        stage_mode: str = "auto",
        model_provider: str = "agents_orchestrator",
    ) -> None:
        self.output_root = output_root
        self.event_callback = event_callback
        self.execute_real_skills = execute_real_skills
        self.stage_mode = stage_mode
        self.model_provider = model_provider

    def run(self, prompt: str, stage_mode: str | None = None, design_json: dict[str, Any] | None = None) -> dict[str, Any]:
        manager = CADAgentSkillManager(self.output_root)
        runner = AgentPipelineRunner(
            self.output_root,
            skill_manager=manager,
            execute_real_skills=self.execute_real_skills,
            event_callback=self.event_callback,
            stage_mode=stage_mode or self.stage_mode,
        )
        context = runner.run(prompt, design_json=design_json, model_provider=self.model_provider)
        data = context.as_dict()
        data["executor_tool"] = {
            "name": "PipelineExecutorTool",
            "called_existing_runner": True,
            "execute_real_skills": self.execute_real_skills,
            "stage_mode": stage_mode or self.stage_mode,
            "precomputed_design_json_injected": design_json is not None,
            "model_provider": self.model_provider,
        }
        return data

    def as_agents_sdk_tool(self) -> Any | None:
        try:
            from agents import function_tool  # type: ignore
        except Exception:
            return None

        @function_tool(name_override="pipeline_executor_tool")
        def pipeline_executor_tool(prompt: str) -> str:
            """Run the existing CAD Pipeline Runner and return its JSON report."""

            import json

            return json.dumps(self.run(prompt), ensure_ascii=False)

        return pipeline_executor_tool
