from __future__ import annotations

import json
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..ai_brain import AIBrain
from ..brain_models import BrainPlan
from ..cad_ir import CADIRCompiler
from ..planner_validator import PlannerValidator
from ..registry import CADAgentSkillManager
from ..runtime_config import output_root as public_output_root
from ..system_resources import cad_resource_preflight
from .cad_execution_lock import CADExecutionLock
from .pipeline_context import PipelineContext, utc_now_iso
from .pipeline_executor import PipelineExecutor
from .pipeline_logger import PipelineLogger
from .pipeline_report import PipelineReportWriter


class AgentPipelineRunner:
    """Run the full AI CAD Agent pipeline from one natural-language prompt."""

    def __init__(
        self,
        output_root: Path,
        skill_manager: CADAgentSkillManager | None = None,
        brain: AIBrain | None = None,
        execute_real_skills: bool = True,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        stage_mode: str = "auto",
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.output_root = output_root
        self.skill_manager = skill_manager or CADAgentSkillManager(output_root)
        self.brain = brain or AIBrain(output_root)
        self.execute_real_skills = execute_real_skills
        self.event_callback = event_callback
        self.stage_mode = stage_mode
        self.cancel_requested = cancel_requested

    def run(self, prompt: str, design_json: dict[str, Any] | None = None, model_provider: str | None = None) -> PipelineContext:
        run_dir = self.output_root / f"pipeline_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        context = PipelineContext(prompt=prompt.strip(), output_root=self.output_root, run_dir=run_dir)
        context.log_path = run_dir / "pipeline.log.jsonl"
        context.report_path = run_dir / "pipeline_report.json"
        logger = PipelineLogger(context.log_path, event_callback=self.event_callback)
        reporter = PipelineReportWriter(context.report_path)

        logger.event("pipeline_started", prompt=context.prompt, execute_real_skills=self.execute_real_skills)
        try:
            if design_json is None:
                brain_plan = self.brain.plan(context.prompt, stage_mode=self.stage_mode)
            else:
                planner_gate = PlannerValidator.execution_gate(design_json)
                if not planner_gate["success"]:
                    brain_plan = BrainPlan(
                        source_prompt=context.prompt,
                        design_json=dict(design_json),
                        skill_pipeline=[],
                        model_provider=model_provider or "precomputed_agent_plan",
                    )
                    brain_plan = self._with_pipeline_identity(brain_plan, run_dir.name)
                    self._save_brain_outputs(run_dir, brain_plan)
                    context.brain_plan = brain_plan
                    context.design_json = brain_plan.design_json
                    context.add_artifact("brain_plan", run_dir / "brain_plan.json")
                    context.add_artifact("design_json", run_dir / "design_plan.json")
                    context.status = "failed"
                    context.final_model_validation = {
                        "status": "failed",
                        "reason": "planner_validation_failed",
                        "planner_gate": planner_gate,
                    }
                    logger.event(
                        "planner_validation_failed",
                        gate="agent_pipeline_runner_boundary",
                        **planner_gate,
                    )
                    self._publish_outputs(context, logger)
                    return context
                design_json = CADIRCompiler().compile(design_json).design
                brain_plan = BrainPlan(
                    source_prompt=context.prompt,
                    design_json=dict(design_json),
                    skill_pipeline=self.brain.skill_planner.plan(design_json),
                    model_provider=model_provider or "precomputed_agent_plan",
                )
            brain_plan = self._with_pipeline_identity(brain_plan, run_dir.name)
            self._save_brain_outputs(run_dir, brain_plan)
            context.brain_plan = brain_plan
            context.design_json = brain_plan.design_json
            context.add_artifact("brain_plan", run_dir / "brain_plan.json")
            context.add_artifact("design_json", run_dir / "design_plan.json")
            logger.event(
                "brain_completed",
                model_provider=brain_plan.model_provider,
                steps=len(brain_plan.skill_pipeline),
                precomputed_design_json=design_json is not None,
            )

            executor = PipelineExecutor(
                self.skill_manager,
                logger,
                execute_real_skills=self.execute_real_skills,
                cancel_requested=self.cancel_requested,
            )
            if self.execute_real_skills:
                resource_status = cad_resource_preflight()
                logger.event("cad_resource_preflight", **resource_status)
                if not resource_status.get("ok"):
                    context.status = "failed"
                    context.final_model_validation = {
                        "status": "failed",
                        "reason": "cad_resource_preflight_failed",
                        "resource_status": resource_status,
                    }
                    logger.event("cad_resource_preflight_failed", **resource_status)
                    self._publish_outputs(context, logger)
                    return context
                logger.event("cad_execution_lock_waiting")
                with CADExecutionLock() as execution_lock:
                    logger.event(
                        "cad_execution_lock_acquired",
                        lock_path=str(execution_lock.path),
                        waited_s=execution_lock.waited_s,
                    )
                    executor.execute(context)
                logger.event("cad_execution_lock_released")
            else:
                executor.execute(context)
            self._publish_outputs(context, logger)
        except Exception as exc:
            context.status = "failed"
            logger.event("pipeline_exception", error=repr(exc), traceback=traceback.format_exc())
        finally:
            context.ended_at = utc_now_iso()
            reporter.write(context)
            self._publish_report(context, logger)
            reporter.write(context)
            logger.event("pipeline_finished", status=context.status, report_path=str(context.report_path))
        return context

    @staticmethod
    def _publish_outputs(context: PipelineContext, logger: PipelineLogger) -> None:
        delivery_root = public_output_root()
        delivery_dir = delivery_root / context.run_dir.name
        delivery_dir.mkdir(parents=True, exist_ok=True)
        context.add_artifact("delivery_dir", delivery_dir)
        copied: dict[str, str] = {}
        for key, value in list(context.artifacts.items()):
            source = Path(value)
            if not source.is_file():
                continue
            target = delivery_dir / source.name
            try:
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                copied[f"delivery_{key}"] = str(target)
                logger.event("artifact_published", artifact_key=key, source=str(source), target=str(target))
            except Exception as exc:
                logger.event("artifact_publish_failed", artifact_key=key, source=str(source), error=repr(exc))
        if context.log_path and context.log_path.exists():
            target = delivery_dir / context.log_path.name
            try:
                shutil.copy2(context.log_path, target)
                copied["delivery_pipeline_log"] = str(target)
            except Exception as exc:
                logger.event("artifact_publish_failed", artifact_key="pipeline_log", source=str(context.log_path), error=repr(exc))
        for key, value in copied.items():
            context.add_artifact(key, value)

    @staticmethod
    def _publish_report(context: PipelineContext, logger: PipelineLogger) -> None:
        if not context.report_path or not context.report_path.exists():
            return
        delivery_dir_value = context.artifacts.get("delivery_dir")
        if not delivery_dir_value:
            return
        target = Path(delivery_dir_value) / context.report_path.name
        try:
            shutil.copy2(context.report_path, target)
            context.add_artifact("delivery_pipeline_report", target)
            logger.event("artifact_published", artifact_key="pipeline_report", source=str(context.report_path), target=str(target))
        except Exception as exc:
            logger.event("artifact_publish_failed", artifact_key="pipeline_report", source=str(context.report_path), error=repr(exc))

    @staticmethod
    def _with_pipeline_identity(brain_plan: BrainPlan, run_id: str) -> BrainPlan:
        design = dict(brain_plan.design_json)
        original_family = str(design.get("part_family") or "vibecad_part")
        design.setdefault("execution", {})
        design["execution"]["pipeline_run_id"] = run_id
        design["execution"]["source_part_family"] = original_family
        safe_run = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in run_id)
        task_id = safe_run.removeprefix("pipeline_")
        design["part_family"] = f"{original_family}_task_{task_id}"
        return BrainPlan(
            source_prompt=brain_plan.source_prompt,
            design_json=design,
            skill_pipeline=brain_plan.skill_pipeline,
            model_provider=brain_plan.model_provider,
        )

    @staticmethod
    def _save_brain_outputs(run_dir: Path, brain_plan: BrainPlan) -> None:
        (run_dir / "brain_plan.json").write_text(
            json.dumps(brain_plan.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "design_plan.json").write_text(
            json.dumps(brain_plan.design_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
