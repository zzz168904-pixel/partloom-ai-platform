from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents_orchestrator.agents_config import AgentsOrchestratorConfig
from ..agents_orchestrator.cad_planner_agent import CADPlannerAgent
from ..agents_orchestrator.skill_router_agent import SkillRouterAgent
from ..cad_ir import CADIRCompiler
from ..planner_validator import PlannerValidator
from ..file2cad import CADFilePipelineRunner
from ..pdf2cad import PDF2CADPipelineRunner
from ..pipeline import AgentPipelineRunner
from ..provider_registry import ProviderRegistry
from ..registry import CADAgentSkillManager
from ..stage_planner import stage_summary
from ..vibecad_skill import VibeCADSkill
from .models import PlanTaskRequest


TERMINAL_STATES = {"success", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class GatewayTask:
    task_id: str
    request: PlanTaskRequest
    task_dir: Path
    status: str = "planning"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    provider: dict[str, Any] = field(default_factory=dict)
    design_json: dict[str, Any] = field(default_factory=dict)
    route: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    report_path: str | None = None
    error: str | None = None
    traceback_text: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def event(self, name: str, **payload: Any) -> None:
        with self.lock:
            item = {"index": len(self.events), "timestamp": _now(), "event": name, **payload}
            self.events.append(item)
            self.updated_at = item["timestamp"]
            self.persist()

    def persist(self) -> None:
        self.task_dir.mkdir(parents=True, exist_ok=True)
        path = self.task_dir / "gateway_task.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.snapshot(include_events=True), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def snapshot(self, include_events: bool = False) -> dict[str, Any]:
        with self.lock:
            data = {
                "task_id": self.task_id,
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "request": self.request.model_dump(),
                "provider": self.provider,
                "design_json": self.design_json,
                "route": self.route,
                "summary": self.summary,
                "artifacts": self.artifacts,
                "report_path": self.report_path,
                "error": self.error,
                "traceback": self.traceback_text,
                "result": self.result,
                "cancel_requested": self.cancel_requested,
                "waiting_for_confirmation": self.status == "awaiting_confirmation",
                "waiting_for_user_continue": self.status == "success",
            }
            if include_events:
                data["events"] = list(self.events)
            return data


class GatewayTaskCoordinator:
    """Shared task boundary for the desktop GUI and native CAD add-ins."""

    def __init__(self, output_root: str | Path, autocad_skill_dir: str | Path | None = None) -> None:
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.autocad_skill_dir = Path(autocad_skill_dir).resolve() if autocad_skill_dir else None
        self.tasks: dict[str, GatewayTask] = {}
        self.tasks_lock = threading.RLock()
        self.cad_execution_lock = threading.Lock()

    def providers(self) -> list[dict[str, Any]]:
        vibecad = VibeCADSkill(self.output_root / "provider_probe")
        return [item.as_dict() for item in ProviderRegistry(vibecad).statuses()]

    def plan(self, request: PlanTaskRequest) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        task = GatewayTask(task_id, request, self.output_root / task_id)
        with self.tasks_lock:
            self.tasks[task_id] = task
        task.event("planning_started", source_type=request.source_type)
        try:
            if request.source_type == "text":
                self._plan_text(task)
            elif request.source_type == "pdf":
                self._plan_pdf(task)
            else:
                self._plan_cad_file(task)
            task.status = "awaiting_confirmation"
            task.event(
                "planning_finished",
                needs_confirmation=bool(task.summary.get("needs_confirmation")),
                provider=task.provider.get("id"),
            )
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.traceback_text = traceback.format_exc()
            task.event("planning_failed", error=repr(exc), traceback=task.traceback_text)
        task.persist()
        return task.snapshot(include_events=True)

    def _plan_text(self, task: GatewayTask) -> None:
        registry = ProviderRegistry(VibeCADSkill(task.task_dir / "planner"))
        provider, status = registry.resolve(task.request.provider)
        task.provider = status.as_dict()
        config = AgentsOrchestratorConfig.detect()
        planner = CADPlannerAgent(task.task_dir / "planner", config=config, provider=provider)
        design = CADIRCompiler().compile(
            planner.plan(task.request.prompt, stage_mode=task.request.stage_mode)
        ).design
        route = SkillRouterAgent(config).route(design)
        task.design_json = design
        task.route = route
        task.summary = stage_summary(design)
        task.artifacts["gateway_design_plan"] = str(task.task_dir / "design_plan.json")
        task.artifacts["gateway_skill_route"] = str(task.task_dir / "skill_route.json")
        (task.task_dir / "design_plan.json").write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
        (task.task_dir / "skill_route.json").write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")

    def _plan_pdf(self, task: GatewayTask) -> None:
        runner = PDF2CADPipelineRunner(task.task_dir, self.autocad_skill_dir)
        planned = runner.plan(task.request.source_path or "", mode=task.request.conversion_mode)
        task.provider = {"id": "pdf_parser", "label": planned.get("parser", "PDF parser"), "configured": True, "available": True}
        task.design_json = dict(planned["design_json"])
        task.route = {"skill_pipeline": list(task.design_json.get("skill_pipeline", [])), "unsupported_features": task.design_json.get("unsupported_features", [])}
        task.summary = dict(planned["summary"])
        task.artifacts.update({key: str(value) for key, value in planned.get("artifacts", {}).items()})

    def _plan_cad_file(self, task: GatewayTask) -> None:
        runner = CADFilePipelineRunner(task.task_dir, self.autocad_skill_dir)
        planned = runner.plan(
            task.request.source_path or "",
            requested_outputs=task.request.requested_outputs,
            prompt=task.request.prompt,
        )
        task.provider = {"id": "cad_file_router", "label": "CAD file router", "configured": True, "available": True}
        task.design_json = dict(planned["design_json"])
        task.route = {"skill_pipeline": list(task.design_json.get("skill_pipeline", [])), "unsupported_features": task.design_json.get("unsupported_features", [])}
        task.summary = dict(planned["summary"])
        task.artifacts.update({key: str(value) for key, value in planned.get("artifacts", {}).items()})

    def confirm(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        with task.lock:
            if task.status != "awaiting_confirmation":
                raise RuntimeError(f"Task cannot be confirmed from status {task.status}.")
            if task.design_json.get("planning_contract"):
                try:
                    task.design_json = PlannerValidator.authorize_after_confirmation(task.design_json)
                    task.summary = stage_summary(task.design_json)
                except ValueError as exc:
                    raise RuntimeError(
                        task.summary.get("confirmation_reason")
                        or f"Task requires a revised plan before execution: {exc}"
                    ) from None
            elif task.summary.get("needs_confirmation"):
                raise RuntimeError(task.summary.get("confirmation_reason") or "Task requires a revised plan before execution.")
            task.status = "queued"
            task.event("task_confirmed")
        thread = threading.Thread(target=self._execute, args=(task,), name=f"cad-agent-{task_id[:8]}", daemon=True)
        thread.start()
        return task.snapshot(include_events=True)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        with task.lock:
            if task.status in TERMINAL_STATES:
                return task.snapshot(include_events=True)
            task.cancel_requested = True
            if task.status in {"planning", "awaiting_confirmation", "queued"}:
                task.status = "cancelled"
            task.event("cancellation_requested", status=task.status)
        return task.snapshot(include_events=True)

    def retry(self, task_id: str) -> dict[str, Any]:
        previous = self.get_task(task_id)
        if previous.status not in {"failed", "cancelled"}:
            raise RuntimeError("Only failed or cancelled tasks can be retried.")
        return self.plan(previous.request)

    def _execute(self, task: GatewayTask) -> None:
        if task.cancel_requested:
            with task.lock:
                task.status = "cancelled"
                task.event("task_cancelled")
            return
        task.event("waiting_for_cad_execution_lock")
        with self.cad_execution_lock:
            if task.cancel_requested:
                with task.lock:
                    task.status = "cancelled"
                    task.event("task_cancelled")
                return
            task.status = "running"
            task.event("task_started")
            try:
                self._co_initialize()
                if task.request.source_type == "text":
                    result = self._execute_text(task)
                elif task.request.source_type == "pdf":
                    result = self._execute_pdf(task)
                else:
                    result = self._execute_cad_file(task)
                task.result = result
                task.artifacts.update({key: str(value) for key, value in result.get("artifacts", {}).items() if value})
                task.report_path = str(result.get("report_path") or task.artifacts.get("pipeline_report") or "") or None
                final_status = str(result.get("status") or ("success" if result.get("success") else "failed"))
                if final_status not in TERMINAL_STATES:
                    final_status = "success" if result.get("success") else "failed"
                if task.cancel_requested and final_status == "success":
                    task.event("cancellation_arrived_after_completion")
                with task.lock:
                    task.status = final_status
                    task.event("task_finished", status=final_status, report_path=task.report_path)
            except Exception as exc:
                task.error = str(exc)
                task.traceback_text = traceback.format_exc()
                with task.lock:
                    task.status = "failed"
                    task.event("task_failed", error=repr(exc), traceback=task.traceback_text)
            finally:
                self._co_uninitialize()

    def _execute_text(self, task: GatewayTask) -> dict[str, Any]:
        runner = AgentPipelineRunner(
            self.output_root,
            skill_manager=CADAgentSkillManager(self.output_root),
            execute_real_skills=task.request.execute_real_skills,
            event_callback=lambda event: task.event("pipeline_event", payload=event),
            stage_mode=task.request.stage_mode,
            cancel_requested=lambda: task.cancel_requested,
        )
        context = runner.run(
            task.request.prompt,
            design_json=task.design_json,
            model_provider=task.provider.get("id") or "gateway",
        )
        return context.as_dict()

    def _execute_pdf(self, task: GatewayTask) -> dict[str, Any]:
        result = PDF2CADPipelineRunner(self.output_root, self.autocad_skill_dir).run(
            task.request.source_path or "",
            mode=task.request.conversion_mode,
            design_json=task.design_json,
            event_callback=lambda event: task.event("pipeline_event", payload=event),
        )
        return {"status": "success" if result.success else "failed", **result.as_dict()}

    def _execute_cad_file(self, task: GatewayTask) -> dict[str, Any]:
        return CADFilePipelineRunner(self.output_root, self.autocad_skill_dir).run(
            task.request.source_path or "",
            design_json=task.design_json,
            event_callback=lambda event: task.event("pipeline_event", payload=event),
        )

    def get_task(self, task_id: str) -> GatewayTask:
        with self.tasks_lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def snapshot(self, task_id: str, include_events: bool = False) -> dict[str, Any]:
        return self.get_task(task_id).snapshot(include_events=include_events)

    def events(self, task_id: str, after: int = -1) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        with task.lock:
            return [dict(item) for item in task.events if int(item.get("index", -1)) > after]

    @staticmethod
    def _co_initialize() -> None:
        try:
            import pythoncom  # type: ignore

            pythoncom.CoInitialize()
        except Exception:
            pass

    @staticmethod
    def _co_uninitialize() -> None:
        try:
            import pythoncom  # type: ignore

            pythoncom.CoUninitialize()
        except Exception:
            pass
