from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agents_config import AgentsOrchestratorConfig
from .cad_planner_agent import CADPlannerAgent
from .pipeline_executor_tool import PipelineExecutorTool
from .recovery_agent import RecoveryAgent
from .skill_router_agent import SkillRouterAgent
from .validator_agent import ValidatorAgent
from ..cad_ir import CADIRCompiler
from ..planner_validator import PlannerValidator
from ..provider_registry import ProviderRegistry
from ..vibecad_skill import VibeCADSkill


class AgentsOrchestratorRunner:
    """Experimental OpenAI Agents SDK orchestration facade."""

    def __init__(
        self,
        output_root: Path,
        config: AgentsOrchestratorConfig | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        provider_id: str = "auto",
    ) -> None:
        self.output_root = output_root
        self.config = config or AgentsOrchestratorConfig.detect()
        self.event_callback = event_callback
        self.provider, self.provider_status = ProviderRegistry(VibeCADSkill(output_root / "provider_probe")).resolve(provider_id)
        self.planner = CADPlannerAgent(output_root, self.config, provider=self.provider)
        self.router = SkillRouterAgent(self.config)
        self.validator = ValidatorAgent(self.config)
        self.recovery = RecoveryAgent(self.config)

    def run(
        self,
        prompt: str,
        execute_real_skills: bool = False,
        run_pipeline: bool = True,
        stage_mode: str = "auto",
        design_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_dir = self.output_root / f"agents_orchestrator_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        design = dict(design_json) if design_json is not None else self.planner.plan(prompt, stage_mode=stage_mode)
        design = CADIRCompiler().compile(design).design
        planner_gate = PlannerValidator.execution_gate(design)
        route = self.router.route(design) if planner_gate["success"] else {
            "skill_pipeline": [],
            "unsupported_features": list(design.get("unsupported_features") or []),
            "needs_confirmation": True,
            "blocked_by": "planner_validation_failed",
        }
        (run_dir / "agents_design_plan.json").write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "agents_skill_pipeline.json").write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
        agent_statuses = {
            "planner": "success" if design.get("features") else "failed",
            "router": "success" if planner_gate["success"] and route.get("skill_pipeline") else ("blocked" if not planner_gate["success"] else "failed"),
            "executor": "skipped",
            "validator": "skipped",
            "recovery": "skipped",
        }

        pipeline_result: dict[str, Any] = {}
        validation: dict[str, Any] = {"status": "skipped", "missing": [], "present": {}, "checked_paths": {}}
        recovery: dict[str, Any] = {"suggestions": ["Pipeline execution skipped."], "failed_steps": []}
        if not planner_gate["success"]:
            pipeline_result = {
                "status": "failed",
                "report_path": None,
                "skill_pipeline": [],
                "final_model_validation": {
                    "status": "failed",
                    "reason": "planner_validation_failed",
                    "planner_gate": planner_gate,
                },
            }
            validation = {
                "status": "failed",
                "reason": "planner_validation_failed",
                "errors": list(planner_gate.get("errors") or []),
            }
            recovery = {
                "suggestions": ["Revise or confirm the candidate plan before Pipeline execution."],
                "failed_steps": ["planner_validation"],
            }
            agent_statuses.update({"executor": "blocked", "validator": "failed", "recovery": "success"})
        elif run_pipeline:
            executor = PipelineExecutorTool(
                self.output_root,
                event_callback=self.event_callback,
                execute_real_skills=execute_real_skills,
                stage_mode=stage_mode,
                model_provider=self.provider_status.id,
            )
            pipeline_result = executor.run(prompt, stage_mode=stage_mode, design_json=design)
            agent_statuses["executor"] = "success" if pipeline_result.get("status") == "success" else "failed"
            validation = self.validator.validate(pipeline_result.get("report_path"))
            agent_statuses["validator"] = validation.get("status", "failed")
            recovery = self.recovery.recover(pipeline_result.get("report_path"), validation)
            agent_statuses["recovery"] = "success" if recovery.get("suggestions") else "failed"

        planning_provider = str(
            (design.get("llm_provider") or {}).get("id")
            or design.get("planning_mode")
            or self.provider_status.id
        )
        direct_cad_ir = planning_provider == "direct_cad_ir"
        report = {
            "status": "success" if pipeline_result.get("status") == "success" and validation.get("status") in {"success", "partial", "skipped"} else "failed",
            "prompt": prompt,
            "orchestrator": {
                "mode": "provider_registry",
                "provider": planning_provider if direct_cad_ir else self.provider_status.id,
                "planning_provider": planning_provider,
                "provider_label": "Direct CAD-IR" if direct_cad_ir else self.provider_status.label,
                "model": "deterministic" if direct_cad_ir else self.provider_status.model,
                "configured": True if direct_cad_ir else self.provider_status.configured,
                "sdk_available": self.config.sdk_available,
                "api_key_configured": False if direct_cad_ir else self.provider_status.configured,
                "llm_enabled": not direct_cad_ir,
                "source": (
                    "Direct CAD-IR -> deterministic validation -> existing Pipeline Runner"
                    if direct_cad_ir
                    else "ProviderRegistry -> CADPlannerAgent -> existing Pipeline Runner"
                ),
            },
            "agent_statuses": agent_statuses,
            "design_json": design,
            "precomputed_design_json": design_json is not None,
            "skill_pipeline": route.get("skill_pipeline", []),
            "task_type": design.get("task_type"),
            "requested_stages": design.get("requested_stages", []),
            "forbidden_stages": design.get("forbidden_stages", []),
            "stop_after": design.get("stop_after"),
            "needs_confirmation": design.get("needs_confirmation", False),
            "planner_gate": planner_gate,
            "unsupported_features": route.get("unsupported_features", []),
            "pipeline_result": pipeline_result,
            "validation": validation,
            "recovery": recovery,
            "artifacts": {
                "agents_design_plan": str(run_dir / "agents_design_plan.json"),
                "agents_skill_pipeline": str(run_dir / "agents_skill_pipeline.json"),
                "agents_orchestrator_report": str(run_dir / "agents_orchestrator_report.json"),
                "pipeline_report": pipeline_result.get("report_path"),
            },
        }
        (run_dir / "agents_orchestrator_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report
