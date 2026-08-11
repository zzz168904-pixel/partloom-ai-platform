from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents_config import AgentsOrchestratorConfig


class RecoveryAgent:
    """Reads pipeline reports and proposes non-destructive recovery actions."""

    def __init__(self, config: AgentsOrchestratorConfig | None = None) -> None:
        self.config = config or AgentsOrchestratorConfig.detect()
        self.sdk_agent = self.config.create_sdk_agent(
            "RecoveryAgent",
            "Read CAD pipeline reports and propose non-destructive recovery actions.",
        )

    def recover(self, pipeline_report: str | Path | None, validation: dict[str, Any] | None = None) -> dict[str, Any]:
        suggestions: list[str] = []
        failed_steps: list[dict[str, Any]] = []
        if pipeline_report and Path(pipeline_report).is_file():
            report = json.loads(Path(pipeline_report).read_text(encoding="utf-8"))
            unexecutable = report.get("design_json", {}).get("execution_policy", {}).get("unexecutable_required_features", [])
            if unexecutable:
                feature_names = ", ".join(str(item.get("name") or item.get("type")) for item in unexecutable)
                suggestions.append(
                    f"Do not start or retry CAD execution. Required production executors are missing for: {feature_names}."
                )
            for step in report.get("skill_pipeline", []):
                if step.get("status") in {"failed", "error"}:
                    failed_steps.append(step)
                    suggestions.append(f"Retry non-destructive step {step.get('skill_key')}:{step.get('action')} after checking {step.get('error') or step.get('message')}.")
        else:
            suggestions.append("pipeline_report.json is missing; rerun PipelineExecutorTool with full traceback logging enabled.")

        missing = (validation or {}).get("missing", [])
        if missing:
            suggestions.append(f"Missing outputs: {', '.join(missing)}. Check whether the corresponding skill is supported by the current stable pipeline.")
            for item in missing:
                suggestions.append(f"Non-destructive retry candidate: rerun the producing step for {item} after fixing its upstream dependency.")
        if not suggestions:
            suggestions.append("No recovery needed.")
        return {"failed_steps": failed_steps, "suggestions": suggestions}
