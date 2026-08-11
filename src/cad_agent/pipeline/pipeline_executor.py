from __future__ import annotations

import copy
import json
import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..brain_models import PlannedSkillStep
from ..cad_ir import CADIRCompiler
from ..model_integrity import ModelIntegrityGuard, ModelTransaction
from ..planner_validator import PlannerValidator
from ..models import SkillResult
from ..registry import CADAgentSkillManager
from .pipeline_context import PipelineContext, PipelineStepRecord, utc_now_iso
from .pipeline_logger import PipelineLogger


@dataclass
class LifecycleResult:
    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    files: list[str] = field(default_factory=list)


class SkillLifecycleAdapter:
    def prepare(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        return LifecycleResult(True, "prepared")

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        return LifecycleResult(True, "executed")

    def verify(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        return LifecycleResult(result.success, "verified" if result.success else "verify failed", result.data, result.path, result.files)

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        return LifecycleResult(True, "export recorded", result.data, result.path, result.files)

    def cleanup(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        return LifecycleResult(True, "cleanup complete")


class PlanningLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        return LifecycleResult(True, "Design JSON already produced by AI Brain.", {"design_json_ready": bool(context.design_json)})

    def verify(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        required = ("parameters", "features", "review_plan")
        missing = [key for key in required if key not in context.design_json]
        return LifecycleResult(not missing, "Design JSON verified" if not missing else f"Design JSON missing: {missing}", result.data)


class ExistingSkillLifecycleAdapter(SkillLifecycleAdapter):
    def __init__(self, runner: Callable[[dict[str, Any]], SkillResult], artifact_prefix: str) -> None:
        self.runner = runner
        self.artifact_prefix = artifact_prefix

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        result = self.runner(context.design_json)
        files = self._collect_files(result)
        return LifecycleResult(
            success=result.success,
            message=result.message,
            data=result.data,
            path=result.path,
            files=files,
        )

    def verify(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        if not result.success:
            return LifecycleResult(False, result.message, result.data, result.path, result.files)
        if result.path and not Path(result.path).exists():
            return LifecycleResult(False, f"Output path does not exist: {result.path}", result.data, result.path, result.files)
        return LifecycleResult(True, "Skill output verified", result.data, result.path, result.files)

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        if result.path:
            context.add_artifact(f"{self.artifact_prefix}_path", result.path)
        for file_path in result.files:
            self._register_file_artifact(context, file_path)
        return LifecycleResult(True, "Skill artifacts recorded", result.data, result.path, result.files)

    @staticmethod
    def _collect_files(result: SkillResult) -> list[str]:
        files = result.data.get("files", []) if isinstance(result.data, dict) else []
        collected = [str(path) for path in files]
        if result.path:
            path = Path(result.path)
            if path.is_dir():
                collected.extend(str(item) for item in path.glob("*") if item.is_file())
            elif path.exists():
                collected.append(str(path))
        return sorted(set(collected))

    @staticmethod
    def _register_file_artifact(context: PipelineContext, file_path: str) -> None:
        path = Path(file_path)
        if path.name.startswith("~$"):
            return
        suffix = path.suffix.lower()
        artifact_keys = {
            ".sldprt": "solidworks_model",
            ".sldasm": "solidworks_assembly",
            ".slddrw": "solidworks_drawing",
            ".step": "step",
            ".stp": "step",
            ".dwg": "dwg",
            ".dxf": "dxf",
            ".pdf": "pdf",
        }
        key = artifact_keys.get(suffix)
        if key == "solidworks_model" and key in context.artifacts:
            return
        if key:
            context.add_artifact(key, file_path)


def _plan_for_step(context: PipelineContext, step: PlannedSkillStep) -> dict[str, Any]:
    """Preserve the full CAD-IR gate while identifying this execution batch."""
    plan = copy.deepcopy(context.design_json)
    step_inputs = getattr(step, "inputs", {}) or {}
    feature_ids = {
        str(value).strip()
        for value in list(step_inputs.get("feature_ids") or [])
        if str(value).strip()
    }
    if not feature_ids:
        return plan
    plan["execution_feature_ids"] = sorted(feature_ids)
    return plan


def _source_part_clone_output_name(plan: dict[str, Any]) -> str:
    """Derive a task-specific clone name from the validated source Part."""
    for feature in list(plan.get("features") or []):
        if str(feature.get("type") or feature.get("operation") or "").strip() != "source_part_clone":
            continue
        params = dict(feature.get("params") or feature.get("parameters") or {})
        source_path = str(params.get("source_path") or "").strip()
        source_stem = Path(source_path).stem.strip()
        if source_stem:
            return f"{source_stem}_ExactClone.SLDPRT"
    return "source_part_ExactClone.SLDPRT"


class BasePlateLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(lambda _plan: skill_manager.run_base_plate_plan(_plan, Path(".")), "base_plate")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        result = self.skill_manager.run_base_plate_plan(context.design_json, context.run_dir / "model")
        files = self._collect_files(result)
        return LifecycleResult(result.success, result.message, result.data, result.path, files)


class SourcePartCloneLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_source_part_clone_plan, "source_part_clone")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        target_dir = context.run_dir / "model"
        target_dir.mkdir(parents=True, exist_ok=True)
        plan["execution_model_path"] = str(target_dir / _source_part_clone_output_name(plan))
        result = self.skill_manager.run_source_part_clone_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class RevolveLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_revolve_plan, "revolve")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        existing_model = context.artifacts.get("solidworks_model")
        if existing_model:
            plan["execution_model_path"] = existing_model
        else:
            target_dir = context.run_dir / "model"
            target_dir.mkdir(parents=True, exist_ok=True)
            plan["execution_model_path"] = str(target_dir / "revolved_task.SLDPRT")
        result = self.skill_manager.run_revolve_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class ProfileExtrudeLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_profile_extrude_plan, "profile_extrude")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        existing_model = context.artifacts.get("solidworks_model")
        if existing_model:
            plan["execution_model_path"] = existing_model
        else:
            target_dir = context.run_dir / "model"
            target_dir.mkdir(parents=True, exist_ok=True)
            plan["execution_model_path"] = str(target_dir / "profile_extruded_task.SLDPRT")
        result = self.skill_manager.run_profile_extrude_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class SweepLoftLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(
        self,
        skill_manager: CADAgentSkillManager,
        feature_type: str,
        runner: Callable[[dict[str, Any]], SkillResult],
    ) -> None:
        super().__init__(runner, feature_type)
        self.skill_manager = skill_manager
        self.feature_type = feature_type
        self.runner = runner

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        existing_model = context.artifacts.get("solidworks_model")
        if existing_model:
            plan["execution_model_path"] = existing_model
        else:
            target_dir = context.run_dir / "model"
            target_dir.mkdir(parents=True, exist_ok=True)
            plan["execution_model_path"] = str(target_dir / f"{self.feature_type}_task.SLDPRT")
        result = self.runner(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class GearPairLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_gear_pair_plan, "gear_pair")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        plan["execution_gear_pair_dir"] = str(context.run_dir / "gear_pair")
        result = self.skill_manager.run_gear_pair_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class AssemblyMateLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_assembly_mate_plan, "assembly_mate")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        target_dir = context.run_dir / "assembly"
        target_dir.mkdir(parents=True, exist_ok=True)
        plan["execution_assembly_path"] = str(target_dir / "assembly_task.SLDASM")
        result = self.skill_manager.run_assembly_mate_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class ProductionFilletLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_production_fillet_plan, "fillet")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        plan["execution_model_path"] = context.artifacts.get("solidworks_model")
        plan["execution_save_after_fillet"] = False
        result = self.skill_manager.run_production_fillet_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class ActiveModelThroughHoleLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(skill_manager.run_active_model_through_hole_plan, "through_hole")
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        plan["execution_model_path"] = context.artifacts.get("solidworks_model")
        result = self.skill_manager.run_active_model_through_hole_plan(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class ActiveModelFeatureLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(
        self,
        skill_manager: CADAgentSkillManager,
        feature_type: str,
        runner: Callable[[dict[str, Any]], SkillResult],
    ) -> None:
        super().__init__(runner, feature_type)
        self.skill_manager = skill_manager
        self.feature_type = feature_type
        self.runner = runner

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        plan = _plan_for_step(context, step)
        plan["execution_model_path"] = context.artifacts.get("solidworks_model")
        result = self.runner(plan)
        return LifecycleResult(result.success, result.message, result.data, result.path, self._collect_files(result))


class SaveSldprtLifecycleAdapter(ExistingSkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        super().__init__(
            lambda _plan: skill_manager.save_active_part(Path("."), validated_plan=_plan),
            "save_sldprt",
        )
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        result = self.skill_manager.save_active_part(
            context.run_dir / "model",
            context.artifacts.get("solidworks_model"),
            validated_plan=context.design_json,
        )
        files = self._collect_files(result)
        return LifecycleResult(result.success, result.message, result.data, result.path, files)


class ArtifactRequirementAdapter(SkillLifecycleAdapter):
    def __init__(self, artifact_key: str, label: str) -> None:
        self.artifact_key = artifact_key
        self.label = label

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        path = context.artifacts.get(self.artifact_key)
        if path:
            return LifecycleResult(True, f"{self.label} already available.", path=path, files=[path])
        return LifecycleResult(False, f"{self.label} artifact is not available yet.")


class AutoCADAnnotationLifecycleAdapter(SkillLifecycleAdapter):
    def __init__(self, skill_manager: CADAgentSkillManager) -> None:
        self.skill_manager = skill_manager

    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        source_dwg = self._source_dwg(context)
        if not source_dwg:
            return LifecycleResult(False, "SolidWorks-exported DWG artifact is not available for AutoCAD annotation.")
        plan = dict(context.design_json)
        plan["source_dwg_path"] = source_dwg
        plan["annotation_output_dir"] = str(context.run_dir / "autocad_annotation")
        result = self.skill_manager.run_autocad_annotation_plan(plan)
        files = ExistingSkillLifecycleAdapter._collect_files(result)
        return LifecycleResult(
            success=result.success,
            message=result.message,
            data=result.data,
            path=result.path,
            files=files,
        )

    def verify(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        if not result.success:
            return LifecycleResult(False, result.message, result.data, result.path, result.files)
        annotated_dwg = result.data.get("annotated_dwg") if isinstance(result.data, dict) else result.path
        if not annotated_dwg or not Path(annotated_dwg).exists():
            return LifecycleResult(False, f"Annotated DWG does not exist: {annotated_dwg}", result.data, result.path, result.files)
        if int(result.data.get("dimensions_created", 0)) <= 0:
            return LifecycleResult(False, "AutoCAD annotation created no native dimensions.", result.data, result.path, result.files)
        return LifecycleResult(True, "AutoCAD annotated DWG verified", result.data, result.path, result.files)

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        if result.data.get("source_dwg"):
            context.add_artifact("source_dwg", result.data["source_dwg"])
        if result.data.get("annotated_dwg"):
            context.add_artifact("annotated_dwg", result.data["annotated_dwg"])
            context.add_artifact("dwg", result.data["annotated_dwg"])
        if result.data.get("pdf_path"):
            context.add_artifact("annotated_pdf", result.data["pdf_path"])
            context.add_artifact("pdf", result.data["pdf_path"])
        if result.data.get("report_path"):
            context.add_artifact("autocad_annotation_report", result.data["report_path"])
        for file_path in result.files:
            ExistingSkillLifecycleAdapter._register_file_artifact(context, file_path)
        return LifecycleResult(True, "AutoCAD annotation artifacts recorded", result.data, result.path, result.files)

    @staticmethod
    def _source_dwg(context: PipelineContext) -> str | None:
        candidates: list[str] = []
        if context.artifacts.get("dwg"):
            candidates.append(context.artifacts["dwg"])
        exports = context.artifacts.get("solidworks_drawing_exports", "")
        candidates.extend(part for part in exports.split(";") if part)
        for candidate in candidates:
            path = Path(candidate)
            if path.suffix.lower() == ".dwg" and path.exists():
                return str(path)
        return None


class SolidWorksDrawingLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        _ensure_legacy_src_import_path()
        from sw_connector import SWConnector

        connector = SWConnector()
        model_path = context.artifacts.get("solidworks_path") or context.artifacts.get("solidworks_model")
        if model_path:
            open_result = connector.open_model(model_path)
            if not open_result.get("success"):
                activate_result = self._activate_open_model(connector, model_path)
                if not activate_result.success:
                    return LifecycleResult(False, f"Failed to open model for drawing: {open_result.get('message')}")
        elif connector.connect() is None:
            return LifecycleResult(False, "SolidWorks is not available for drawing generation.")

        drawing_result = connector.create_drawing_with_standard_views()
        if not drawing_result.get("success"):
            return LifecycleResult(False, f"SolidWorks drawing failed: {drawing_result.get('message')}", drawing_result)

        complex_result = {
            "success": True,
            "requested": 0,
            "created": 0,
            "views": [],
            "layout": {"status": "not_required"},
            "errors": [],
        }
        complex_report_path: Path | None = None
        special_views = context.design_json.get("drawing_plan", {}).get("special_views", [])
        if special_views:
            from ..solidworks_complex_drawing import SolidWorksComplexDrawingViewEngine

            complex_result = SolidWorksComplexDrawingViewEngine(connector).apply(
                connector.app.ActiveDoc,
                context.design_json.get("drawing_plan", {}),
            )
            complex_report_path = context.run_dir / "complex_drawing_report.json"
            complex_report_path.write_text(
                json.dumps(complex_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            drawing_result["complex_views"] = complex_result
            if not complex_result.get("success"):
                return LifecycleResult(
                    False,
                    "SolidWorks complex drawing views failed: " + "; ".join(complex_result.get("errors", [])),
                    data={
                        "drawing": drawing_result,
                        "complex_views": complex_result,
                        "complex_report_path": str(complex_report_path),
                    },
                    path=str(complex_report_path),
                    files=[str(complex_report_path)],
                )

        drawing_file = self._save_current_drawing(connector, context)
        files = [str(complex_report_path)] if complex_report_path else []
        if drawing_file:
            files.append(str(drawing_file))
        success = bool(files)
        if complex_report_path and not drawing_file:
            success = False
        data = {
            "drawing": drawing_result,
            "complex_views": complex_result,
            "complex_report_path": str(complex_report_path) if complex_report_path else None,
        }
        message = "SolidWorks drawing saved" if success else "SolidWorks drawing created but SLDDRW save failed"
        return LifecycleResult(success, message, data=data, files=files)

    @staticmethod
    def _save_current_drawing(connector: Any, context: PipelineContext) -> Path | None:
        try:
            doc = connector.app.ActiveDoc
            model_path = Path(context.artifacts.get("solidworks_model") or context.artifacts.get("solidworks_path") or "drawing")
            target = (context.run_dir / f"{model_path.stem}_drawing.SLDDRW").resolve()
            for save_call in (
                lambda: doc.SaveAs3(str(target), 0, 0),
                lambda: doc.SaveAs(str(target)),
            ):
                try:
                    save_call()
                    if target.exists() and target.stat().st_size > 0:
                        return target
                except Exception:
                    continue
        except Exception:
            return None
        return None

    @staticmethod
    def _activate_open_model(connector: Any, model_path: str) -> LifecycleResult:
        try:
            if connector.app is None and connector.connect() is None:
                return LifecycleResult(False, "SolidWorks is not connected.")
            errors = connector._byref_i4()
            activated = connector.app.ActivateDoc3(Path(model_path).name, False, 1, errors)
            if activated is None:
                return LifecycleResult(False, f"ActivateDoc3 returned no document: errors={errors.value}")
            return LifecycleResult(True, "Activated already-open model.")
        except Exception as exc:
            return LifecycleResult(False, repr(exc))

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        for file_path in result.files:
            ExistingSkillLifecycleAdapter._register_file_artifact(context, file_path)
        if result.files:
            context.add_artifact("solidworks_drawing_exports", ";".join(result.files))
        if result.data.get("complex_report_path"):
            context.add_artifact("complex_drawing_report", result.data["complex_report_path"])
        return LifecycleResult(True, "Drawing artifacts recorded", result.data, result.path, result.files)


class SolidWorksStepExportLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        existing = context.artifacts.get("step")
        model_path = context.artifacts.get("solidworks_model") or context.artifacts.get("solidworks_path")
        if not model_path or not Path(model_path).exists():
            if existing and Path(existing).exists():
                return LifecycleResult(True, "STEP already available.", path=existing, files=[existing])
            return LifecycleResult(False, "SolidWorks model artifact is not available for STEP export.")

        _ensure_legacy_src_import_path()
        from sw_connector import SWConnector

        connector = SWConnector()
        opened = connector.open_model(model_path)
        if not opened.get("success"):
            activated = SolidWorksDrawingLifecycleAdapter._activate_open_model(connector, model_path)
            if not activated.success:
                return LifecycleResult(False, f"Failed to open model for STEP export: {opened.get('message')}")
        target = (context.run_dir / f"{Path(model_path).stem}.STEP").resolve()
        errors: list[str] = []
        try:
            doc = connector.app.ActiveDoc
            for save_call in (
                lambda: doc.Extension.SaveAs(str(target), 0, 1, None, connector._byref_i4(), connector._byref_i4()),
                lambda: doc.SaveAs3(str(target), 0, 0),
                lambda: doc.SaveAs(str(target)),
            ):
                try:
                    save_call()
                    if target.exists() and target.stat().st_size > 0:
                        return LifecycleResult(True, f"STEP exported: {target}", path=str(target), files=[str(target)])
                except Exception as exc:
                    errors.append(repr(exc))
        except Exception as exc:
            errors.append(repr(exc))
        if existing and Path(existing).exists():
            return LifecycleResult(True, f"STEP fallback already available after export failure: {existing}", path=existing, files=[existing])
        return LifecycleResult(False, f"STEP export failed: {'; '.join(errors[-3:])}")

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        for file_path in result.files:
            ExistingSkillLifecycleAdapter._register_file_artifact(context, file_path)
        return LifecycleResult(True, "STEP artifact recorded", result.data, result.path, result.files)


class SolidWorksPDFExportLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        _ensure_legacy_src_import_path()
        from sw_connector import SWConnector

        connector = SWConnector()
        if connector.connect() is None:
            return LifecycleResult(False, "SolidWorks is not available for PDF export.")
        result = connector.export_current_pdf()
        path = result.get("path") if isinstance(result, dict) else None
        files = [str(path)] if result.get("success") and path else []
        return LifecycleResult(bool(files), result.get("message", "PDF export finished"), result, str(path) if path else None, files)

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        for file_path in result.files:
            ExistingSkillLifecycleAdapter._register_file_artifact(context, file_path)
        return LifecycleResult(True, "PDF artifact recorded", result.data, result.path, result.files)


class SolidWorksDWGExportLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        _ensure_legacy_src_import_path()
        from sw_connector import SWConnector

        connector = SWConnector()
        if connector.connect() is None:
            return LifecycleResult(False, "SolidWorks is not available for DWG export.")
        result = connector.export_current_dwg()
        path = result.get("path") if isinstance(result, dict) else None
        files = [str(path)] if result.get("success") and path else []
        return LifecycleResult(bool(files), result.get("message", "DWG export finished"), result, str(path) if path else None, files)

    def export(self, context: PipelineContext, step: PlannedSkillStep, result: LifecycleResult) -> LifecycleResult:
        for file_path in result.files:
            ExistingSkillLifecycleAdapter._register_file_artifact(context, file_path)
        return LifecycleResult(True, "DWG artifact recorded", result.data, result.path, result.files)


class SimulatedLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        path = context.run_dir / f"{step.skill_key}_{step.action}.json"
        path.write_text(
            '{"simulated": true, "skill_key": "%s", "action": "%s"}' % (step.skill_key, step.action),
            encoding="utf-8",
        )
        return LifecycleResult(True, "Simulated pipeline step completed.", {"simulated": True}, str(path), [str(path)])


class UnsupportedLifecycleAdapter(SkillLifecycleAdapter):
    def execute(self, context: PipelineContext, step: PlannedSkillStep) -> LifecycleResult:
        return LifecycleResult(False, f"No lifecycle adapter registered for {step.skill_key}/{step.action}.")


def _ensure_legacy_src_import_path() -> None:
    import sys

    src_dir = Path(__file__).resolve().parents[2]
    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)


class PipelineExecutor:
    ACTIVE_PART_SKILLS = {
        "fillet",
        "through_hole",
        "threaded_hole",
        "boss",
        "chamfer",
        "dome",
        "external_thread",
        "pocket",
        "slot",
        "linear_pattern",
        "circular_pattern",
        "mirror",
        "rib",
        "side_boss",
        "side_hole",
        "reference_geometry",
        "draft",
        "shell",
        "configuration",
        "equation",
        "save_sldprt",
    }

    def __init__(
        self,
        skill_manager: CADAgentSkillManager,
        logger: PipelineLogger,
        execute_real_skills: bool = True,
        cancel_requested: Callable[[], bool] | None = None,
        model_integrity_guard: ModelIntegrityGuard | None = None,
    ) -> None:
        self.skill_manager = skill_manager
        self.logger = logger
        self.execute_real_skills = execute_real_skills
        self.cancel_requested = cancel_requested
        self.model_integrity_guard = model_integrity_guard or ModelIntegrityGuard()
        self.adapters = self._build_adapters()

    def execute(self, context: PipelineContext) -> PipelineContext:
        if context.brain_plan is None:
            raise ValueError("PipelineContext.brain_plan is required before execution.")
        planner_gate = PlannerValidator.execution_gate(context.design_json)
        if not planner_gate["success"]:
            context.status = "failed"
            context.final_model_validation = {
                "status": "failed",
                "reason": "planner_validation_failed",
                "planner_gate": planner_gate,
            }
            self.logger.event("planner_validation_failed", gate="pipeline_executor_boundary", **planner_gate)
            return context
        cad_ir_result = CADIRCompiler().compile(context.design_json)
        context.design_json = cad_ir_result.design
        if not cad_ir_result.success:
            context.status = "failed"
            context.final_model_validation = {
                "status": "failed",
                "reason": "cad_ir_validation_failed",
                "errors": cad_ir_result.errors,
            }
            self.logger.event(
                "cad_ir_validation_failed",
                errors=cad_ir_result.errors,
                warnings=cad_ir_result.warnings,
            )
            return context
        self.logger.event(
            "cad_ir_validated",
            version=cad_ir_result.ir["version"],
            feature_count=len(cad_ir_result.ir["features"]),
            warning_count=len(cad_ir_result.warnings),
        )
        unexecutable = context.design_json.get("execution_policy", {}).get("unexecutable_required_features", [])
        if unexecutable:
            context.status = "failed"
            self.logger.event(
                "required_features_not_executable",
                features=unexecutable,
                error="required_features_not_executable",
            )
            return context
        for step in context.brain_plan.skill_pipeline:
            if self.cancel_requested and self.cancel_requested():
                context.status = "cancelled"
                self.logger.event("pipeline_cancelled", before_skill=step.skill_key)
                break
            record = PipelineStepRecord(
                skill_key=step.skill_key,
                action=step.action,
                required=step.required,
                reason=step.reason,
            )
            context.step_records.append(record)
            self._execute_step(context, step, record)
            if record.status == "failed" and step.required:
                context.status = "failed"
                self.logger.event("pipeline_halted", skill_key=step.skill_key, error=record.error)
                break
        if context.status not in {"failed", "cancelled"}:
            context.final_model_validation = self._validate_model_scope(context)
            if context.final_model_validation.get("status") == "failed":
                context.status = "failed"
                self.logger.event("final_model_scope_mismatch", validation=context.final_model_validation)
            else:
                self.logger.event(
                    "stop_after_reached",
                    stop_after=context.design_json.get("stop_after"),
                    no_further_skills_scheduled=True,
                    waiting_for_user_continue=True,
                )
                context.status = "success" if all(item.status in {"success", "skipped"} for item in context.step_records) else "partial"
        return context

    def _execute_step(self, context: PipelineContext, step: PlannedSkillStep, record: PipelineStepRecord) -> None:
        guard_error = self._allowed_skills_guard(context, step)
        if guard_error:
            record.started_at = utc_now_iso()
            record.ended_at = utc_now_iso()
            record.status = "failed"
            record.error = guard_error
            self.logger.event(
                "blocked_by_allowed_skills_guard",
                current_skill=step.skill_key,
                allowed_skills=context.design_json.get("execution_policy", {}).get("allowed_skills", []),
                error=guard_error,
            )
            return
        if self.execute_real_skills and self._requires_active_part(context, step):
            activation = self._activate_expected_part(context)
            self.logger.event(
                "active_model_guard",
                skill_key=step.skill_key,
                success=activation.success,
                message=activation.message,
                **activation.data,
            )
            if not activation.success:
                record.started_at = utc_now_iso()
                record.ended_at = utc_now_iso()
                record.status = "failed"
                record.error = activation.message
                return
        adapter = self._adapter_for(step)
        record.started_at = utc_now_iso()
        started = time.perf_counter()
        self.logger.event("step_started", skill_key=step.skill_key, action=step.action)
        transaction: ModelTransaction | None = None
        try:
            if self.execute_real_skills and step.skill_key == "save_sldprt":
                save_gate = self.model_integrity_guard.validate_final_save(context)
                record.data["final_save_gate"] = save_gate.as_dict()
                self.logger.event("final_save_gate", skill_key=step.skill_key, **save_gate.as_dict())
                if not save_gate.success:
                    record.lifecycle["prepare"] = "failed"
                    record.status = "failed"
                    record.error = save_gate.message
                    return
            if (
                self.execute_real_skills
                and step.skill_key != "base_plate"
                and self.model_integrity_guard.requires_transaction(step.skill_key)
            ):
                transaction, precheck = self.model_integrity_guard.begin(context, step.skill_key)
                record.data["geometry_precheck"] = precheck.as_dict()
                self.logger.event("geometry_precheck", skill_key=step.skill_key, **precheck.as_dict())
                if not precheck.success:
                    record.lifecycle["prepare"] = "failed"
                    record.status = "failed"
                    record.error = precheck.message
                    return
            execute_result = LifecycleResult(True)
            postcondition_checked = False
            for stage in ("prepare", "execute", "verify", "export", "cleanup"):
                stage_method = getattr(adapter, stage)
                if stage == "verify":
                    stage_result = stage_method(context, step, execute_result)
                elif stage == "export":
                    stage_result = stage_method(context, step, execute_result)
                else:
                    stage_result = stage_method(context, step)
                record.lifecycle[stage] = "success" if stage_result.success else "failed"
                self.logger.event(
                    "lifecycle_stage",
                    skill_key=step.skill_key,
                    action=step.action,
                    stage=stage,
                    success=stage_result.success,
                    message=stage_result.message,
                )
                if stage == "execute":
                    execute_result = stage_result
                    record.message = stage_result.message
                    record.output_path = stage_result.path
                    record.output_files = stage_result.files
                    boundary_data = dict(record.data)
                    record.data = dict(stage_result.data)
                    record.data.update(boundary_data)
                    unexpected = self._unexpected_output_paths(context, stage_result.files)
                    if unexpected:
                        context.unexpected_outputs.extend(unexpected)
                        record.status = "failed"
                        record.error = f"unexpected_output_generated: {unexpected}"
                        self.logger.event("unexpected_output_generated", skill_key=step.skill_key, paths=unexpected)
                        self._rollback_transaction(context, step, record, transaction, record.error)
                        return
                if not stage_result.success:
                    record.status = "failed" if step.required else "skipped"
                    record.error = stage_result.message
                    self._rollback_transaction(context, step, record, transaction, record.error)
                    return
                if (
                    stage == "verify"
                    and self.execute_real_skills
                    and self.model_integrity_guard.requires_transaction(step.skill_key)
                ):
                    postcondition = self.model_integrity_guard.verify_after(transaction, step.skill_key)
                    record.data["geometry_postcondition"] = postcondition.as_dict()
                    self.logger.event("geometry_postcondition", skill_key=step.skill_key, **postcondition.as_dict())
                    postcondition_checked = True
                    if not postcondition.success:
                        record.status = "failed" if step.required else "skipped"
                        record.error = postcondition.message
                        self._rollback_transaction(context, step, record, transaction, record.error)
                        return
            if self.execute_real_skills and self.model_integrity_guard.requires_transaction(step.skill_key):
                if not postcondition_checked:
                    record.status = "failed" if step.required else "skipped"
                    record.error = "Feature geometry postcondition was not evaluated."
                    self._rollback_transaction(context, step, record, transaction, record.error)
                    return
                committed = self.model_integrity_guard.commit(context, transaction, step.skill_key)
                record.data["checkpoint_commit"] = committed.as_dict()
                self.logger.event("checkpoint_commit", skill_key=step.skill_key, **committed.as_dict())
                if not committed.success:
                    record.status = "failed" if step.required else "skipped"
                    record.error = committed.message
                    self._rollback_transaction(context, step, record, transaction, record.error)
                    return
            record.status = "success"
            if not self.execute_real_skills or not self.model_integrity_guard.requires_transaction(step.skill_key):
                self._save_last_valid_checkpoint(context, step, record)
        except Exception as exc:
            record.status = "failed" if step.required else "skipped"
            record.error = repr(exc)
            self._rollback_transaction(context, step, record, transaction, record.error)
            self.logger.event(
                "step_exception",
                skill_key=step.skill_key,
                action=step.action,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
        finally:
            record.ended_at = utc_now_iso()
            record.duration_s = round(time.perf_counter() - started, 3)
            self.logger.event(
                "step_finished",
                skill_key=step.skill_key,
                action=step.action,
                status=record.status,
                duration_s=record.duration_s,
            )

    def _rollback_transaction(
        self,
        context: PipelineContext,
        step: PlannedSkillStep,
        record: PipelineStepRecord,
        transaction: ModelTransaction | None,
        reason: str,
    ) -> None:
        if not self.execute_real_skills or not self.model_integrity_guard.requires_transaction(step.skill_key):
            return
        rollback = self.model_integrity_guard.rollback(context, transaction, reason)
        record.data["rollback"] = rollback.as_dict()
        self.logger.event("model_rollback", skill_key=step.skill_key, **rollback.as_dict())

    def _save_last_valid_checkpoint(self, context: PipelineContext, step: PlannedSkillStep, record: PipelineStepRecord) -> None:
        """Persist the latest valid Part without presenting it as final delivery."""
        model_value = context.artifacts.get("solidworks_model") or record.output_path
        if not model_value:
            return
        source = Path(model_value)
        if source.suffix.lower() != ".sldprt" or not source.is_file():
            return
        target = context.run_dir / "last_valid_checkpoint.sldprt"
        try:
            shutil.copy2(source, target)
            context.add_artifact("last_valid_checkpoint", target)
            self.logger.event("last_valid_checkpoint_saved", skill_key=step.skill_key, path=str(target), final_delivery=False)
        except Exception as exc:
            self.logger.event("last_valid_checkpoint_failed", skill_key=step.skill_key, source=str(source), error=repr(exc))

    @staticmethod
    def _activate_expected_part(context: PipelineContext) -> LifecycleResult:
        expected_value = context.artifacts.get("solidworks_model")
        if not expected_value:
            if context.design_json.get("task_type") == "modify_3d":
                return LifecycleResult(True, "Active-model modification uses the user-selected Part.", {"expected_model": None})
            return LifecycleResult(False, "Active-model Skill has no SolidWorks model artifact to activate.")

        expected = Path(expected_value).resolve()
        if not expected.is_file() or expected.stat().st_size <= 0:
            return LifecycleResult(False, f"Expected SolidWorks Part does not exist: {expected}")
        try:
            _ensure_legacy_src_import_path()
            from sw_connector import SWConnector

            connector = SWConnector()
            if connector.connect() is None:
                return LifecycleResult(False, "SolidWorks is not available for active-model execution.")
            active = connector.app.ActiveDoc
            active_path = Path(str(SWConnector._read_com_value(active.GetPathName))).resolve() if active is not None else None
            if active_path is None or str(active_path).casefold() != str(expected).casefold():
                opened = connector.open_model(expected)
                if not opened.get("success"):
                    return LifecycleResult(
                        False,
                        f"Could not activate the task model before active-model execution: {expected}",
                        {"expected_model": str(expected), "active_model": str(active_path) if active_path else None},
                    )
                active = connector.app.ActiveDoc
                active_path = Path(str(SWConnector._read_com_value(active.GetPathName))).resolve() if active is not None else None
            if active is None or int(SWConnector._read_int(active.GetType)) != 1:
                return LifecycleResult(False, "Activated document is not a SolidWorks Part.")
            if active_path is None or str(active_path).casefold() != str(expected).casefold():
                return LifecycleResult(
                    False,
                    "ActiveDoc path does not match the current pipeline model.",
                    {"expected_model": str(expected), "active_model": str(active_path) if active_path else None},
                )
            return LifecycleResult(
                True,
                "Current pipeline Part activated and verified.",
                {"expected_model": str(expected), "active_model": str(active_path)},
            )
        except Exception as exc:
            return LifecycleResult(
                False,
                f"Failed to activate the current pipeline Part: {exc}",
                {"expected_model": str(expected), "error": repr(exc)},
            )

    def _adapter_for(self, step: PlannedSkillStep) -> SkillLifecycleAdapter:
        if not self.execute_real_skills:
            return self.adapters.get(step.skill_key, SimulatedLifecycleAdapter())
        return self.adapters.get(step.skill_key, UnsupportedLifecycleAdapter())

    def _build_adapters(self) -> dict[str, SkillLifecycleAdapter]:
        real_adapters: dict[str, SkillLifecycleAdapter] = {
            "source_part_clone": SourcePartCloneLifecycleAdapter(self.skill_manager),
            "base_plate": BasePlateLifecycleAdapter(self.skill_manager),
            "gear": SweepLoftLifecycleAdapter(self.skill_manager, "gear", self.skill_manager.run_gear_plan),
            "gear_pair": GearPairLifecycleAdapter(self.skill_manager),
            "revolve": RevolveLifecycleAdapter(self.skill_manager),
            "profile_extrude": ProfileExtrudeLifecycleAdapter(self.skill_manager),
            "sweep": SweepLoftLifecycleAdapter(self.skill_manager, "sweep", self.skill_manager.run_sweep_plan),
            "loft": SweepLoftLifecycleAdapter(self.skill_manager, "loft", self.skill_manager.run_loft_plan),
            "sheet_metal": SweepLoftLifecycleAdapter(self.skill_manager, "sheet_metal", self.skill_manager.run_sheet_metal_plan),
            "weldment": SweepLoftLifecycleAdapter(self.skill_manager, "weldment", self.skill_manager.run_weldment_plan),
            "freeform_surface": SweepLoftLifecycleAdapter(self.skill_manager, "freeform_surface", self.skill_manager.run_freeform_surface_plan),
            "assembly_mate": AssemblyMateLifecycleAdapter(self.skill_manager),
            "fillet": ProductionFilletLifecycleAdapter(self.skill_manager),
            "through_hole": ActiveModelThroughHoleLifecycleAdapter(self.skill_manager),
            "threaded_hole": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "threaded_hole", self.skill_manager.run_thread_plan),
            "boss": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "boss", self.skill_manager.run_active_model_boss_plan),
            "chamfer": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "chamfer", self.skill_manager.run_active_model_chamfer_plan),
            "dome": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "dome", self.skill_manager.run_active_model_dome_plan),
            "external_thread": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "external_thread", self.skill_manager.run_active_model_external_thread_plan),
            "pocket": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "pocket", self.skill_manager.run_active_model_pocket_plan),
            "slot": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "slot", self.skill_manager.run_active_model_slot_plan),
            "linear_pattern": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "linear_pattern", self.skill_manager.run_active_model_linear_pattern_plan),
            "circular_pattern": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "circular_pattern", self.skill_manager.run_active_model_circular_pattern_plan),
            "mirror": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "mirror", self.skill_manager.run_active_model_mirror_plan),
            "rib": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "rib", self.skill_manager.run_rib_plan),
            "side_boss": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "side_boss", self.skill_manager.run_side_boss_plan),
            "side_hole": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "side_hole", self.skill_manager.run_side_hole_plan),
            "reference_geometry": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "reference_geometry", self.skill_manager.run_reference_geometry_plan),
            "draft": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "draft", self.skill_manager.run_draft_plan),
            "shell": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "shell", self.skill_manager.run_shell_plan),
            "configuration": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "configuration", self.skill_manager.run_configuration_plan),
            "equation": ActiveModelFeatureLifecycleAdapter(self.skill_manager, "equation", self.skill_manager.run_equation_plan),
            "save_sldprt": SaveSldprtLifecycleAdapter(self.skill_manager),
            "solidworks_automation": ExistingSkillLifecycleAdapter(self.skill_manager.run_solidworks_plan, "solidworks"),
            "solidworks_threaded_holes": ExistingSkillLifecycleAdapter(self.skill_manager.run_thread_plan, "thread"),
            "solidworks_cnc_fillet": ExistingSkillLifecycleAdapter(self.skill_manager.run_fillet_chamfer_plan, "fillet_chamfer"),
            "autocad": ExistingSkillLifecycleAdapter(self.skill_manager.run_autocad_plan, "autocad"),
            "autocad_annotation": AutoCADAnnotationLifecycleAdapter(self.skill_manager),
            "solidworks_drawing": SolidWorksDrawingLifecycleAdapter(),
            "step_export": SolidWorksStepExportLifecycleAdapter(),
            "pdf_export": SolidWorksPDFExportLifecycleAdapter(),
            "dwg_export": SolidWorksDWGExportLifecycleAdapter(),
        }
        if self.execute_real_skills:
            return real_adapters
        return {key: SimulatedLifecycleAdapter() for key in real_adapters}

    @classmethod
    def _requires_active_part(cls, context: PipelineContext, step: PlannedSkillStep) -> bool:
        if step.skill_key in cls.ACTIVE_PART_SKILLS:
            return True
        if step.skill_key not in {"revolve", "profile_extrude", "sweep", "loft"}:
            return False
        if step.skill_key == "revolve":
            from ..revolve_skill import RevolveSkill

            normalizer = lambda feature: RevolveSkill.normalize_request(
                feature.get("params", {}), context.design_json.get("task_type")
            )
        elif step.skill_key == "profile_extrude":
            from ..profile_extrude_skill import ProfileExtrudeSkill

            normalizer = lambda feature: ProfileExtrudeSkill.normalize_request(
                feature.get("params", {}), context.design_json.get("task_type")
            )
        else:
            from ..sweep_loft_skill import SweepLoftSkill

            normalizer = lambda feature: SweepLoftSkill.normalize_request(
                step.skill_key, feature.get("params", {}), context.design_json.get("task_type")
            )
        step_inputs = getattr(step, "inputs", {}) or {}
        feature_ids = {
            str(value).strip()
            for value in list(step_inputs.get("feature_ids") or [])
            if str(value).strip()
        }
        matching = [
            feature
            for feature in context.design_json.get("features", [])
            if feature.get("type") == step.skill_key
            and (
                not feature_ids
                or str(feature.get("id") or feature.get("name") or "").strip() in feature_ids
            )
        ]
        return bool(matching) and all(
            normalizer(feature).get("mode") == "active_model"
            for feature in matching
        )

    def _allowed_skills_guard(self, context: PipelineContext, step: PlannedSkillStep) -> str | None:
        policy = context.design_json.get("execution_policy", {})
        allowed = set(policy.get("allowed_skills", []))
        if allowed and step.skill_key not in allowed:
            return f"blocked_by_allowed_skills_guard: {step.skill_key!r} is not in {sorted(allowed)!r}"
        contract = self.skill_manager.skill_contract(step.skill_key)
        if contract is None:
            return None
        if contract.get("uses_test_template"):
            return f"blocked_by_allowed_skills_guard: {step.skill_key!r} uses a test template"
        expected_outputs = set(policy.get("expected_outputs", []))
        output_types = set(contract.get("output_types", []))
        if output_types and not output_types.issubset(expected_outputs):
            return f"blocked_by_allowed_skills_guard: {step.skill_key!r} can output {sorted(output_types)!r}, expected {sorted(expected_outputs)!r}"
        if contract.get("exports_files") and not output_types.issubset(expected_outputs):
            return f"blocked_by_allowed_skills_guard: export side effect exceeds requested outputs"
        return None

    @staticmethod
    def _unexpected_output_paths(context: PipelineContext, files: list[str]) -> list[str]:
        expected = set(context.design_json.get("execution_policy", {}).get("expected_outputs", []))
        allowed_suffixes = {
            ".json", ".jsonl", ".log",
            ".sldprt" if "SLDPRT" in expected else "",
            ".sldasm" if "SLDASM" in expected else "",
        }
        output_by_suffix = {
            ".step": "STEP", ".stp": "STEP", ".slddrw": "SLDDRW", ".pdf": "PDF",
            ".dwg": "DWG", ".dxf": "DXF",
        }
        unexpected: list[str] = []
        for value in files:
            path = Path(value)
            suffix = path.suffix.lower()
            if suffix in allowed_suffixes:
                continue
            if output_by_suffix.get(suffix) not in expected:
                unexpected.append(str(path))
        return unexpected

    def _validate_model_scope(self, context: PipelineContext) -> dict[str, Any]:
        if "model_3d" not in set(context.design_json.get("requested_stages", [])):
            return {"status": "not_applicable", "checks": {}}
        if not self.execute_real_skills:
            return {"status": "simulated", "checks": {"scope_guard": True}}
        expected_features = {str(item.get("type")) for item in context.design_json.get("features", [])}
        record_groups: dict[str, list[PipelineStepRecord]] = {}
        for record in context.step_records:
            record_groups.setdefault(record.skill_key, []).append(record)
        records = {skill_key: group[-1] for skill_key, group in record_groups.items()}
        for skill_key, group in record_groups.items():
            if len(group) > 1 and all(isinstance(record.data.get("operations"), list) for record in group):
                records[skill_key] = self._aggregate_operation_records(
                    group,
                    skill_key=skill_key,
                    action=group[0].action,
                )
        params = context.design_json.get("parameters", {})
        modifies_current = context.design_json.get("task_type") == "modify_3d"
        expected_outputs = set(context.design_json.get("execution_policy", {}).get("expected_outputs", []))
        expected_artifacts = {
            "SLDPRT": "solidworks_model",
            "SLDASM": "solidworks_assembly",
            "STEP": "step",
            "PDF": "pdf",
            "DWG": "dwg",
            "DXF": "dxf",
            "SLDDRW": "solidworks_drawing",
        }
        base = records.get("base_plate")
        source_part_clone = records.get("source_part_clone")
        fillet = records.get("fillet")
        hole = records.get("through_hole")
        boss = records.get("boss")
        pocket = records.get("pocket")
        slot = records.get("slot")
        chamfer = records.get("chamfer")
        dome = records.get("dome")
        external_thread = records.get("external_thread")
        linear_pattern = records.get("linear_pattern")
        circular_pattern = records.get("circular_pattern")
        mirror = records.get("mirror")
        rib = records.get("rib")
        side_boss = records.get("side_boss")
        side_hole = records.get("side_hole")
        gear = records.get("gear")
        gear_pair = records.get("gear_pair")
        revolve = records.get("revolve")
        profile_extrude = records.get("profile_extrude")
        sweep = records.get("sweep")
        loft = records.get("loft")
        sheet_metal = records.get("sheet_metal")
        weldment = records.get("weldment")
        freeform_surface = records.get("freeform_surface")
        reference_geometry = records.get("reference_geometry")
        draft = records.get("draft")
        shell = records.get("shell")
        configuration = records.get("configuration")
        equation = records.get("equation")
        assembly_mate = records.get("assembly_mate")
        assembly_task = bool(expected_features & {"assembly_mate", "gear_pair"})
        gear_pair_task = "gear_pair" in expected_features
        expected_holes = [item for item in context.design_json.get("features", []) if item.get("type") == "through_hole"]
        expected_hole_count = sum(int(item.get("params", {}).get("count", 1) or 1) for item in expected_holes)
        checks: dict[str, bool] = {
            "output_scope": all(
                bool(context.artifacts.get(expected_artifacts[output]) and Path(context.artifacts[expected_artifacts[output]]).is_file())
                for output in expected_outputs
                if output in expected_artifacts
            ) if not modifies_current else not expected_outputs,
            "no_unexpected_outputs": not context.unexpected_outputs,
            "no_test_template": all("cnc_mount_template" not in str(record.data).lower() and "plate_pipeline" not in str(record.data).lower() for record in context.step_records),
            "base_dimensions": modifies_current or "base_plate" not in expected_features or bool(base and base.data.get("created_features") and base.data["created_features"][0].get("length") == float(params.get("length", 0)) and base.data["created_features"][0].get("width") == float(params.get("width", 0)) and base.data["created_features"][0].get("thickness") == float(params.get("thickness", 0))),
            "source_part_clone": "source_part_clone" not in expected_features or bool(
                source_part_clone
                and source_part_clone.data.get("comparison", {}).get("parity_passed")
            ),
            "material": modifies_current or assembly_task or not params.get("material") or bool(
                (base and base.data.get("material") == params.get("material") and base.data.get("material_metadata"))
                or (gear and gear.data.get("material") == params.get("material") and gear.data.get("material_metadata"))
                or (gear_pair and gear_pair.data.get("material") == params.get("material") and gear_pair.data.get("material_metadata"))
                or (revolve and revolve.data.get("material") == params.get("material") and revolve.data.get("material_metadata"))
                or (profile_extrude and profile_extrude.data.get("material") == params.get("material") and profile_extrude.data.get("material_metadata"))
                or (sweep and sweep.data.get("material") == params.get("material") and sweep.data.get("material_metadata"))
                or (loft and loft.data.get("material") == params.get("material") and loft.data.get("material_metadata"))
                or (sheet_metal and sheet_metal.data.get("material") == params.get("material") and sheet_metal.data.get("material_metadata"))
                or (weldment and weldment.data.get("material") == params.get("material") and weldment.data.get("material_metadata"))
                or (freeform_surface and freeform_surface.data.get("material") == params.get("material") and freeform_surface.data.get("material_metadata"))
            ),
            "fillet": "fillet" not in expected_features or self._fillet_operation_matches(fillet),
            "through_hole": "through_hole" not in expected_features or bool(
                hole
                and hole.data.get("feature_created")
                and int(hole.data.get("holes_created", 0)) == expected_hole_count
                and self._hole_groups_match(expected_holes, hole.data.get("groups", []))
            ),
            "boss": "boss" not in expected_features or self._feature_operation_matches(context.design_json, boss, "boss"),
            "pocket": "pocket" not in expected_features or self._feature_operation_matches(context.design_json, pocket, "pocket"),
            "slot": "slot" not in expected_features or self._feature_operation_matches(context.design_json, slot, "slot"),
            "chamfer": "chamfer" not in expected_features or self._feature_operation_matches(context.design_json, chamfer, "chamfer"),
            "dome": "dome" not in expected_features or self._feature_operation_matches(context.design_json, dome, "dome"),
            "external_thread": "external_thread" not in expected_features or self._feature_operation_matches(context.design_json, external_thread, "external_thread"),
            "linear_pattern": "linear_pattern" not in expected_features or self._feature_operation_matches(context.design_json, linear_pattern, "linear_pattern"),
            "circular_pattern": "circular_pattern" not in expected_features or self._feature_operation_matches(context.design_json, circular_pattern, "circular_pattern"),
            "mirror": "mirror" not in expected_features or self._feature_operation_matches(context.design_json, mirror, "mirror"),
            "rib": "rib" not in expected_features or self._advanced_feature_operation_matches(context.design_json, rib, "rib"),
            "side_boss": "side_boss" not in expected_features or self._advanced_feature_operation_matches(context.design_json, side_boss, "side_boss"),
            "side_hole": "side_hole" not in expected_features or self._advanced_feature_operation_matches(context.design_json, side_hole, "side_hole"),
            "gear": "gear" not in expected_features or self._new_model_feature_operation_matches(context.design_json, gear, "gear"),
            "gear_pair": "gear_pair" not in expected_features or self._new_model_feature_operation_matches(context.design_json, gear_pair, "gear_pair"),
            "revolve": "revolve" not in expected_features or self._revolve_operation_matches(context.design_json, revolve),
            "profile_extrude": "profile_extrude" not in expected_features or self._profile_extrude_operation_matches(context.design_json, profile_extrude),
            "sweep": "sweep" not in expected_features or self._sweep_loft_operation_matches(context.design_json, sweep, "sweep"),
            "loft": "loft" not in expected_features or self._sweep_loft_operation_matches(context.design_json, loft, "loft"),
            "sheet_metal": "sheet_metal" not in expected_features or self._new_model_feature_operation_matches(context.design_json, sheet_metal, "sheet_metal"),
            "weldment": "weldment" not in expected_features or self._new_model_feature_operation_matches(context.design_json, weldment, "weldment"),
            "freeform_surface": "freeform_surface" not in expected_features or self._new_model_feature_operation_matches(context.design_json, freeform_surface, "freeform_surface"),
            "reference_geometry": "reference_geometry" not in expected_features or self._management_feature_operation_matches(context.design_json, reference_geometry, "reference_geometry"),
            "draft": "draft" not in expected_features or self._management_feature_operation_matches(context.design_json, draft, "draft"),
            "shell": "shell" not in expected_features or self._management_feature_operation_matches(context.design_json, shell, "shell"),
            "configuration": "configuration" not in expected_features or self._parametric_operation_matches(context.design_json, configuration, "configuration"),
            "equation": "equation" not in expected_features or self._parametric_operation_matches(context.design_json, equation, "equation"),
            "assembly_mate": not assembly_task or self._assembly_mate_operation_matches(context.design_json, assembly_mate),
            "no_extra_features": all(key not in records for key in ("solidworks_cnc_fillet", "solidworks_threaded_holes", "solidworks_drawing", "autocad_annotation")),
            "sldprt_exists": modifies_current or (assembly_task and not gear_pair_task) or bool(context.artifacts.get("solidworks_model") and Path(context.artifacts["solidworks_model"]).is_file()),
            "sldasm_exists": not assembly_task or modifies_current or bool(
                context.artifacts.get("solidworks_assembly") and Path(context.artifacts["solidworks_assembly"]).is_file()
            ),
        }
        return {"status": "success" if all(checks.values()) else "failed", "checks": checks, "expected_features": sorted(expected_features)}

    @staticmethod
    def _aggregate_profile_extrude_records(
        records: list[PipelineStepRecord],
    ) -> PipelineStepRecord:
        """Combine repeated profile batches for final whole-model validation."""
        return PipelineExecutor._aggregate_operation_records(
            records,
            skill_key="profile_extrude",
            action="apply_profile_extrude",
        )

    @staticmethod
    def _aggregate_operation_records(
        records: list[PipelineStepRecord],
        *,
        skill_key: str,
        action: str,
    ) -> PipelineStepRecord:
        """Combine repeated operations of one skill for final whole-model validation."""
        data: dict[str, Any] = {}
        operations: list[dict[str, Any]] = []
        files: list[str] = []
        for record in records:
            operations.extend(list(record.data.get("operations") or []))
            for key, value in record.data.items():
                if key != "operations" and value not in (None, "", [], {}):
                    data.setdefault(key, value)
            files.extend(record.output_files)
        data["operations"] = operations
        data["feature_created"] = bool(records) and all(
            record.status == "success" and bool(record.data.get("feature_created"))
            for record in records
        )
        return PipelineStepRecord(
            skill_key=skill_key,
            action=action,
            required=any(record.required for record in records),
            status="success" if data["feature_created"] else "failed",
            output_files=list(dict.fromkeys(files)),
            data=data,
        )

    @staticmethod
    def _hole_groups_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
        if len(expected) != len(actual):
            return False
        unmatched = list(actual)
        for feature in expected:
            params = feature.get("params", {})
            diameter = float(params.get("diameter", 0) or 0)
            count = int(params.get("count", 1) or 1)
            index = next(
                (
                    idx
                    for idx, group in enumerate(unmatched)
                    if group.get("success")
                    and int(group.get("count", 0)) == count
                    and abs(float(group.get("diameter_mm", 0)) - diameter) <= 1e-6
                ),
                None,
            )
            if index is None:
                return False
            unmatched.pop(index)
        return not unmatched

    @staticmethod
    def _fillet_operation_matches(record: PipelineStepRecord | None) -> bool:
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        postcondition = record.data.get("geometry_postcondition")
        if isinstance(postcondition, dict) and not postcondition.get("success"):
            return False
        return PipelineExecutor._fillet_payload_matches(record.data)

    @staticmethod
    def _fillet_payload_matches(data: dict[str, Any]) -> bool:
        selector = str(data.get("edge_selector") or "outer_vertical_edges").strip().lower()
        if selector == "ordered_batch":
            operations = list(data.get("operations") or [])
            requested = int(data.get("features_requested", 0) or 0)
            created = int(data.get("features_created", 0) or 0)
            return (
                requested > 0
                and created == requested
                and len(operations) == requested
                and all(
                    bool(item.get("success"))
                    and bool(item.get("feature_created"))
                    and PipelineExecutor._fillet_payload_matches(item)
                    for item in operations
                )
            )
        selected = int(
            data.get("selected_edges", data.get("selected_outer_vertical_edges", 0))
            or 0
        )
        if selector == "all_feature_edges":
            isolated = data.get("isolated_execution")
            if isinstance(isolated, dict) and not isolated.get("success"):
                return False
            return bool(data.get("target_feature_id")) and selected >= 1
        if selector == "explicit_edge_signatures":
            expected = int(data.get("expected_edge_signatures", 0) or 0)
            return expected > 0 and selected == expected
        return selected == 4

    @staticmethod
    def _feature_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [item for item in record.data.get("operations", []) if item.get("type") == feature_type and item.get("success")]
        if len(operations) != len(expected):
            return False
        for feature in expected:
            params = feature.get("params", {})
            operation = next((item for item in operations if item.get("name") == feature.get("name")), None)
            if operation is None:
                return False
            if feature_type == "chamfer":
                if abs(float(operation.get("size_mm", 0)) - float(params.get("size", 0) or 0)) > 1e-6:
                    return False
                selector = str(
                    params.get("targets")
                    or params.get("edge_selector")
                    or "all_outer_edges"
                ).strip().lower()
                if selector == "explicit_edge_signatures":
                    expected_edges = len(params.get("edge_signatures") or [])
                    if expected_edges <= 0:
                        return False
                    if int(operation.get("selected_edges", 0) or 0) != expected_edges:
                        return False
                    if int(operation.get("expected_edge_signatures", 0) or 0) != expected_edges:
                        return False
                    if float(operation.get("volume_delta_m3", 0.0) or 0.0) >= 0.0:
                        return False
                    if int(operation.get("body_count_after", -1)) != int(operation.get("body_count_before", -2)):
                        return False
                continue
            if feature_type == "dome":
                from ..active_model_feature_skill import ActiveModelFeatureSkill

                expected_request = ActiveModelFeatureSkill.normalize_dome_request(params)
                actual_request = dict(operation.get("request") or {})
                if not expected_request.get("success"):
                    return False
                keys = (
                    "mode",
                    "height_mm",
                    "reverse_direction",
                    "elliptical",
                    "face_selector",
                )
                if any(actual_request.get(key) != expected_request.get(key) for key in keys):
                    return False
                if str(operation.get("feature_type") or "").casefold() != "dome":
                    return False
                if int(operation.get("candidate_face_count", 0)) != 1:
                    return False
                native_definition = dict(operation.get("native_definition") or {})
                if not native_definition.get("matches_request"):
                    return False
                if abs(
                    float(native_definition.get("height_mm", 0.0))
                    - float(expected_request["height_mm"])
                ) > 1e-6:
                    return False
                if not operation.get("volume_changed"):
                    return False
                continue
            if feature_type == "external_thread":
                from ..active_model_feature_skill import ActiveModelFeatureSkill

                expected_request = ActiveModelFeatureSkill.normalize_external_thread_request(params)
                request = dict(operation.get("request") or {})
                keys = (
                    "designation",
                    "representation",
                    "end",
                    "axis",
                    "standard",
                    "standard_type",
                    "edge_selector",
                    "major_diameter_mm",
                    "pitch_mm",
                    "thread_length_mm",
                    "thread_callout",
                    "thread_method",
                    "right_handed",
                    "trim_start_face",
                    "trim_end_face",
                    "thread_start_angle_deg",
                    "reverse_direction",
                )
                if not expected_request.get("success") or any(request.get(key) != expected_request.get(key) for key in keys):
                    return False
                if int(operation.get("feature_error_code", -1)) != 0:
                    return False
                if not operation.get("feature_name") or not operation.get("edge_signature"):
                    return False
                if not bool((operation.get("feature_registry_evidence") or {}).get("success")):
                    return False
                representation = str(expected_request.get("representation") or "")
                expected_feature_type = "SweepThread" if representation == "modeled" else "CosmeticThread"
                if str(operation.get("feature_type") or "").casefold() != expected_feature_type.casefold():
                    return False
                if representation == "modeled":
                    geometry = dict(operation.get("geometry_evidence") or {})
                    if not geometry.get("success"):
                        return False
                    if int(geometry.get("body_count_before", 0)) <= 0:
                        return False
                    if int(geometry.get("body_count_after", 0)) != int(geometry.get("body_count_before", 0)):
                        return False
                    if float(geometry.get("volume_removed_m3", 0.0) or 0.0) <= 0.0:
                        return False
                continue
            if feature_type in {"linear_pattern", "circular_pattern", "mirror"}:
                request = operation.get("request", {})
                expected_request = PipelineExecutor._normalized_pattern_request(feature_type, params)
                if not expected_request.get("success"):
                    return False
                keys = {
                    "linear_pattern": ("seed_features", "count_1", "spacing_1_mm", "direction_1", "count_2", "spacing_2_mm", "direction_2"),
                    "circular_pattern": (
                        "seed_features",
                        "count",
                        "total_angle_deg",
                        "spacing_angle_deg",
                        "pattern_angle_deg",
                        "axis",
                        "axis_feature",
                        "axis_center_mm",
                        "equal_spacing",
                        "geometry_pattern",
                        "reverse",
                    ),
                    "mirror": ("seed_features", "mirror_plane", "scope"),
                }[feature_type]
                if any(request.get(key) != expected_request.get(key) for key in keys):
                    return False
                continue
            dimensions = operation.get("dimensions_mm", {})
            keys = {
                "boss": ("length", "width", "height"),
                "pocket": ("length", "width", "depth", "corner_radius"),
                "slot": ("length", "width"),
            }[feature_type]
            if any(abs(float(dimensions.get(key, 0)) - float(params.get(key, 0) or 0)) > 1e-6 for key in keys):
                return False
            if feature_type == "slot" and int(operation.get("count", 0)) != int(params.get("count", 0) or 0):
                return False
        return True

    @staticmethod
    def _normalized_pattern_request(feature_type: str, params: dict[str, Any]) -> dict[str, Any]:
        from ..active_model_pattern_skill import ActiveModelPatternSkill

        return ActiveModelPatternSkill.normalize_request(feature_type, params)

    @staticmethod
    def _advanced_feature_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        from ..advanced_feature_skill import AdvancedFeatureSkill

        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [item for item in record.data.get("operations", []) if item.get("type") == feature_type and item.get("success")]
        if len(operations) != len(expected):
            return False
        for feature in expected:
            operation = next((item for item in operations if item.get("name") == feature.get("name")), None)
            if operation is None:
                return False
            expected_request = AdvancedFeatureSkill.normalize_request(feature_type, feature.get("params", {}))
            actual_request = operation.get("request", {})
            if not expected_request.get("success"):
                return False
            if actual_request != expected_request:
                return False
        return True

    @staticmethod
    def _revolve_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
    ) -> bool:
        from ..revolve_skill import RevolveSkill

        expected = [item for item in design.get("features", []) if item.get("type") == "revolve"]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [item for item in record.data.get("operations", []) if item.get("type") == "revolve" and item.get("success")]
        if len(operations) != len(expected):
            return False
        for feature, operation in zip(expected, operations):
            request = RevolveSkill.normalize_request(feature.get("params", {}), design.get("task_type"))
            if not request.get("success") or not operation.get("geometry_validation", {}).get("success"):
                return False
            if operation.get("operation") != request.get("operation"):
                return False
            if abs(float(operation.get("angle_deg", 0)) - float(request.get("angle_deg", 0))) > 1e-6:
                return False
            if operation.get("profile_points_mm") != request.get("profile_points_mm"):
                return False
        named = RevolveSkill.profile_from_named_dimensions(
            design.get("parameters", {}),
            str(design.get("source_brief") or ""),
        )
        if named.get("applicable"):
            if not named.get("success") or len(operations) != 1:
                return False
            semantic = RevolveSkill.validate_profile_against_design_dimensions(
                operations[0].get("profile_points_mm", []),
                named["design_dimensions"],
            )
            if not semantic.get("success"):
                return False
            actual_spans = sorted(float(value) for value in operations[0].get("geometry_validation", {}).get("actual_spans_mm", []))
            expected_spans = sorted(
                [
                    float(named["design_dimensions"]["total_length_mm"]),
                    float(named["design_dimensions"]["flange_outer_diameter_mm"]),
                    float(named["design_dimensions"]["flange_outer_diameter_mm"]),
                ]
            )
            if len(actual_spans) != 3 or any(
                abs(actual - target) > max(0.25, target * 0.01)
                for actual, target in zip(actual_spans, expected_spans)
            ):
                return False
        return True

    @staticmethod
    def _profile_extrude_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
    ) -> bool:
        from ..profile_extrude_skill import ProfileExtrudeSkill

        expected = [item for item in design.get("features", []) if item.get("type") == "profile_extrude"]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [
            item for item in record.data.get("operations", [])
            if item.get("type") == "profile_extrude" and item.get("success")
        ]
        if len(operations) != len(expected):
            return False
        for feature, operation in zip(expected, operations):
            request = ProfileExtrudeSkill.normalize_request(
                feature.get("params", {}), design.get("task_type")
            )
            if not request.get("success") or operation.get("request") != request:
                return False
            if not operation.get("feature_name") or not operation.get("geometry_validation", {}).get("success"):
                return False
        return True

    @staticmethod
    def _management_feature_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        from ..feature_management_skill import FeatureManagementSkill

        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [item for item in record.data.get("operations", []) if item.get("type") == feature_type and item.get("success")]
        if len(operations) != len(expected):
            return False
        for feature, operation in zip(expected, operations):
            request = FeatureManagementSkill.normalize_request(feature_type, feature.get("params", {}))
            if not request.get("success") or operation.get("request") != request:
                return False
            if not operation.get("feature_name"):
                return False
        return True

    @staticmethod
    def _sweep_loft_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        from ..sweep_loft_skill import SweepLoftSkill

        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [
            item for item in record.data.get("operations", [])
            if item.get("type") == feature_type and item.get("success")
        ]
        if len(operations) != len(expected):
            return False
        for feature, operation in zip(expected, operations):
            request = SweepLoftSkill.normalize_request(
                feature_type, feature.get("params", {}), design.get("task_type")
            )
            if not request.get("success") or operation.get("request") != request:
                return False
            if not operation.get("feature_name") or not operation.get("geometry_validation", {}).get("success"):
                return False
        return True

    @staticmethod
    def _new_model_feature_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        if feature_type == "sheet_metal":
            from ..sheet_metal_skill import SheetMetalSkill

            normalizer = SheetMetalSkill.normalize_request
        elif feature_type == "gear":
            from ..gear_skill import GearSkill

            normalizer = GearSkill.normalize_request
        elif feature_type == "gear_pair":
            from ..gear_pair_skill import GearPairSkill

            normalizer = GearPairSkill.normalize_request
        elif feature_type == "weldment":
            from ..weldment_skill import WeldmentSkill

            normalizer = WeldmentSkill.normalize_request
        elif feature_type == "freeform_surface":
            from ..freeform_surface_skill import FreeformSurfaceSkill

            normalizer = FreeformSurfaceSkill.normalize_request
        else:
            return False
        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if len(expected) != 1 or not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [
            item for item in record.data.get("operations", [])
            if item.get("type") == feature_type and item.get("success")
        ]
        if len(operations) != 1:
            return False
        request = normalizer(expected[0].get("params", {}), design.get("task_type"))
        operation = operations[0]
        return bool(
            request.get("success")
            and operation.get("request") == request
            and operation.get("feature_name")
            and operation.get("geometry_validation", {}).get("success")
        )

    @staticmethod
    def _parametric_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
        feature_type: str,
    ) -> bool:
        from ..parametric_management_skill import ParametricManagementSkill

        expected = [item for item in design.get("features", []) if item.get("type") == feature_type]
        if not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        operations = [
            item for item in record.data.get("operations", [])
            if item.get("type") == feature_type and item.get("success")
        ]
        if len(operations) != len(expected):
            return False
        for feature, operation in zip(expected, operations):
            request = ParametricManagementSkill.normalize_request(feature_type, feature.get("params", {}))
            if not request.get("success") or operation.get("request") != request:
                return False
            if feature_type == "configuration":
                if operation.get("configuration_name") != request.get("name"):
                    return False
            elif request.get("lhs") not in str(operation.get("stored_expression", "")):
                return False
        return True

    @staticmethod
    def _assembly_mate_operation_matches(
        design: dict[str, Any],
        record: PipelineStepRecord | None,
    ) -> bool:
        from ..assembly_mate_skill import AssemblyMateSkill

        expected = [item for item in design.get("features", []) if item.get("type") == "assembly_mate"]
        if len(expected) != 1 or not record or record.status != "success" or not record.data.get("feature_created"):
            return False
        request = AssemblyMateSkill.normalize_request(expected[0].get("params", {}))
        if not request.get("success"):
            return False
        operations = [
            item for item in record.data.get("operations", [])
            if item.get("type") == "assembly_mate" and item.get("success")
        ]
        if len(operations) != len(request.get("mates", [])):
            return False
        expected_by_name = {item["name"]: item for item in request["mates"]}
        return all(
            operation.get("name") in expected_by_name
            and operation.get("request") == expected_by_name[operation["name"]]
            for operation in operations
        )
