from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai_brain import AIBrain
from .agents_orchestrator.agents_config import AgentsOrchestratorConfig
from .autocad_annotation_skill import AutoCADAnnotationSkill
from .autocad_skill import AutoCADSkill
from .assembly_mate_skill import AssemblyMateSkill
from .brain_models import BrainPlan
from .cad_ir import CADIRCompiler
from .planner_validator import PlannerValidator
from .fillet_chamfer_skill import FilletChamferCNCSkill
from .file2cad import CADFilePipelineRunner
from .feature_management_skill import FeatureManagementSkill
from .gear_skill import GearSkill
from .gear_pair_skill import GearPairSkill
from .models import AgentRoute, SkillPaths, SkillResult
from .parametric_management_skill import ParametricManagementSkill
from .pdf2cad import PDF2CADPipelineRunner
from .production_fillet_skill import ProductionFilletSkill
from .profile_extrude_skill import ProfileExtrudeSkill
from .revolve_skill import RevolveSkill
from .sweep_loft_skill import SweepLoftSkill
from .sheet_metal_skill import SheetMetalSkill
from .weldment_skill import WeldmentSkill
from .freeform_surface_skill import FreeformSurfaceSkill
from .solidworks_automation_skill import SolidWorksAutomationSkill
from .source_part_clone_skill import SourcePartCloneSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_pattern_skill import ActiveModelPatternSkill
from .advanced_feature_skill import AdvancedFeatureSkill
from .thread_skill import ThreadSkill
from .vibecad_skill import VibeCADSkill


class CADAgentSkillManager:
    """Plugin-style CAD Agent dispatcher.

    The dispatcher keeps each skill's responsibility narrow:
    VibeCAD understands intent and emits JSON, Automation executes geometry,
    Thread handles tapped holes, Fillet handles edge machining, and AutoCAD
    handles 2D DWG/DXF work.
    """

    def __init__(self, output_root: Path | None = None, project_root: Path | None = None) -> None:
        project = project_root or Path(__file__).resolve().parents[2]
        root = Path.home() / ".codex" / "skills" / "solidworks-automation"
        subskills = root / "subskills"
        self.paths = SkillPaths(
            project_root=project,
            output_root=output_root or (project / "logs" / "skill_outputs"),
            solidworks_skill_dir=root,
            vibecad_skill_dir=subskills / "solidworks-vibecad",
            threaded_holes_skill_dir=subskills / "solidworks-threaded-holes",
            fillet_chamfer_skill_dir=subskills / "solidworks-fillet-chamfer-cnc",
            autocad_skill_dir=subskills / "autocad-automation",
        )
        self.vibecad = VibeCADSkill(self.paths.output_root)
        self.solidworks_automation = SolidWorksAutomationSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.source_part_clone_skill = SourcePartCloneSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.thread_skill = ThreadSkill(self.paths.output_root, self.paths.threaded_holes_skill_dir)
        # Legacy demonstration template. It remains callable only from explicit
        # tests and is intentionally absent from production routing contracts.
        self.fillet_chamfer_skill = FilletChamferCNCSkill(self.paths.output_root, self.paths.fillet_chamfer_skill_dir)
        self.production_fillet_skill = ProductionFilletSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.active_model_through_hole = ActiveModelThroughHoleExecutor(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.active_model_feature_skill = ActiveModelFeatureSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.active_model_pattern_skill = ActiveModelPatternSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.advanced_feature_skill = AdvancedFeatureSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.gear_skill = GearSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.gear_pair_skill = GearPairSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.revolve_skill = RevolveSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.profile_extrude_skill = ProfileExtrudeSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.sweep_loft_skill = SweepLoftSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.sheet_metal_skill = SheetMetalSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.weldment_skill = WeldmentSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.freeform_surface_skill = FreeformSurfaceSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.feature_management_skill = FeatureManagementSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.parametric_management_skill = ParametricManagementSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.assembly_mate_skill = AssemblyMateSkill(self.paths.output_root, self.paths.solidworks_skill_dir)
        self.autocad_skill = AutoCADSkill(self.paths.output_root, self.paths.autocad_skill_dir)
        self.autocad_annotation_skill = AutoCADAnnotationSkill(self.paths.output_root, self.paths.autocad_skill_dir)
        self.brain = AIBrain(self.paths.output_root)
        self.pdf2cad_runner = PDF2CADPipelineRunner(self.paths.output_root, self.paths.autocad_skill_dir)
        self.file2cad_runner = CADFilePipelineRunner(self.paths.output_root, self.paths.autocad_skill_dir)

    def route(self, command: str, existing_actions: list[str] | None = None) -> AgentRoute | None:
        if existing_actions:
            return None
        text = command.lower()
        if self._looks_like_vibecad_request(command):
            return AgentRoute(
                skill_key="solidworks_vibecad",
                skill_name="SolidWorks VibeCAD",
                actions=["vibecad_agent_plan"],
                confidence=0.91,
                reason="natural language CAD intent should be planned before automation",
            )
        if any(token in text for token in ("dwg", "dxf", "autocad", "cad编辑", "图层")):
            return AgentRoute("autocad", "AutoCAD Automation", ["autocad_skill_status"], 0.76, "2D CAD/DWG request")
        return None

    def run_vibecad_plan(self, brief: str) -> SkillResult:
        return self.vibecad.run(brief)

    def plan_brain(self, prompt: str) -> BrainPlan:
        return self.brain.plan(prompt)

    def plan_brain_and_save(self, prompt: str) -> tuple[BrainPlan, Path]:
        return self.brain.plan_and_save(prompt)

    def run_agent_pipeline(self, prompt: str, execute_real_skills: bool = True) -> dict[str, Any]:
        from .pipeline import AgentPipelineRunner

        runner = AgentPipelineRunner(
            self.paths.output_root,
            skill_manager=self,
            brain=self.brain,
            execute_real_skills=execute_real_skills,
        )
        return runner.run(prompt).as_dict()

    def run_pdf2cad_pipeline(
        self,
        pdf_path: str | Path,
        mode: str = "2d",
        design_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.pdf2cad_runner.run(pdf_path, mode=mode, design_json=design_json).as_dict()

    def run_file2cad_pipeline(
        self,
        source_path: str | Path,
        requested_outputs: list[str] | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        planned = self.file2cad_runner.plan(source_path, requested_outputs=requested_outputs, prompt=prompt)
        return self.file2cad_runner.run(source_path, design_json=planned["design_json"])

    def run_solidworks_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.solidworks_automation.run_plan(normalized)

    def run_source_part_clone_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.source_part_clone_skill.run_plan(normalized)

    def run_base_plate_plan(self, plan: dict, run_dir: Path) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.solidworks_automation.run_base_plate_plan(normalized, run_dir)

    def run_production_fillet_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.production_fillet_skill.run_plan(normalized)

    def run_active_model_through_hole_plan(self, plan: dict) -> SkillResult:
        plan, failure = self._guard_cad_ir_plan(plan)
        if failure:
            return failure
        features = [item for item in plan.get("features", []) if item.get("type") == "through_hole"]
        if not features:
            return SkillResult(False, "No through_hole feature found in the production plan.")
        run_dir = self.paths.output_root / f"production_through_holes_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "production_through_holes_report.json"
        groups: list[dict[str, Any]] = []
        files: list[str] = []
        for feature in features:
            params = feature.get("params", {})
            placement = str(params.get("placement") or params.get("position") or "center").lower()
            request: dict[str, Any] = {
                "mode": "active_model",
                "hole_type": "through",
                "diameter_mm": float(params.get("diameter", 0) or 0),
                "feature_name": feature.get("name") or "ThroughHole",
                "save_path": plan.get("execution_model_path"),
                "save_after": False,
            }
            geometry_context = params.get("geometry_context") or params.get("target_geometry")
            if placement == "corner_offsets":
                length = float(plan.get("parameters", {}).get("length", 0) or 0)
                width = float(plan.get("parameters", {}).get("width", 0) or 0)
                offsets = params.get("edge_offsets_mm") or params.get("edge_offsets") or {}
                xoff, yoff = float(offsets.get("x", 0) or 0), float(offsets.get("y", 0) or 0)
                geometry_context = geometry_context or {
                    "target_body": "primary_solid",
                    "target_feature": "base_plate",
                    "target_face_role": "outer_horizontal_face",
                    "face_normal": [0, 0, 1],
                    "face_origin": [0, 0, float(plan.get("parameters", {}).get("thickness", 0) or 0)],
                    "local_u_axis": [1, 0, 0],
                    "local_v_axis": [0, 1, 0],
                    "placement_uv_mm": [[-length / 2 + xoff, -width / 2 + yoff], [length / 2 - xoff, -width / 2 + yoff], [-length / 2 + xoff, width / 2 - yoff], [length / 2 - xoff, width / 2 - yoff]],
                    "bounding_box_location": "z_max",
                }
                request.update(
                    {
                        "count": int(params.get("count", 4) or 4),
                        "placement": "corner_offsets",
                        "edge_offsets_mm": offsets,
                    }
                )
            else:
                geometry_context = geometry_context or {
                    "target_body": "primary_solid",
                    "target_feature": "base_plate",
                    "target_face_role": "outer_horizontal_face",
                    "face_normal": [0, 0, 1],
                    "face_origin": [0, 0, float(plan.get("parameters", {}).get("thickness", 0) or 0)],
                    "local_u_axis": [1, 0, 0],
                    "local_v_axis": [0, 1, 0],
                    "placement_uv_mm": [[0, 0]],
                    "bounding_box_location": "z_max",
                }
                request.update({"count": int(params.get("count", 1) or 1), "placement": "center"})
            request["geometry_context"] = geometry_context
            result = self.active_model_through_hole.run(request)
            files.extend(str(value) for value in result.data.get("files", []))
            groups.append(
                {
                    "name": feature.get("name") or "ThroughHole",
                    "success": result.success,
                    "message": result.message,
                    "diameter_mm": request["diameter_mm"],
                    "count": request["count"],
                    "placement": request["placement"],
                    "geometry_context": geometry_context,
                    "hole_centers_mm": result.data.get("hole_centers_mm", []),
                    "report_path": result.path,
                }
            )
            if not result.success:
                payload = {"success": False, "feature_created": False, "holes_created": sum(int(item.get("count", 0)) for item in groups if item.get("success")), "groups": groups, "files": files}
                report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                payload["files"].append(str(report_path))
                return SkillResult(False, result.message, data=payload, path=str(report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))

        payload = {
            "success": True,
            "feature_created": True,
            "holes_created": sum(int(item["count"]) for item in groups),
            "hole_groups_created": len(groups),
            "groups": groups,
            "files": files + [str(report_path)],
        }
        if len(groups) == 1:
            payload["diameter_mm"] = groups[0]["diameter_mm"]
            payload["hole_centers_mm"] = groups[0]["hole_centers_mm"]
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(True, f"Created {payload['holes_created']} through holes in {len(groups)} group(s).", data=payload, path=str(report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))

    def run_active_model_feature_plan(self, plan: dict, feature_type: str) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.active_model_feature_skill.run_plan(normalized, feature_type)

    def run_active_model_boss_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "boss")

    def run_active_model_pocket_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "pocket")

    def run_active_model_slot_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "slot")

    def run_active_model_chamfer_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "chamfer")

    def run_active_model_dome_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "dome")

    def run_active_model_external_thread_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_feature_plan(plan, "external_thread")

    def run_active_model_pattern_plan(self, plan: dict, feature_type: str) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.active_model_pattern_skill.run_plan(normalized, feature_type)

    def run_active_model_linear_pattern_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_pattern_plan(plan, "linear_pattern")

    def run_active_model_circular_pattern_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_pattern_plan(plan, "circular_pattern")

    def run_active_model_mirror_plan(self, plan: dict) -> SkillResult:
        return self.run_active_model_pattern_plan(plan, "mirror")

    def run_advanced_feature_plan(self, plan: dict, feature_type: str) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.advanced_feature_skill.run_plan(normalized, feature_type)

    def run_side_boss_plan(self, plan: dict) -> SkillResult:
        return self.run_advanced_feature_plan(plan, "side_boss")

    def run_side_hole_plan(self, plan: dict) -> SkillResult:
        return self.run_advanced_feature_plan(plan, "side_hole")

    def run_rib_plan(self, plan: dict) -> SkillResult:
        return self.run_advanced_feature_plan(plan, "rib")

    def run_revolve_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.revolve_skill.run_plan(normalized)

    def run_profile_extrude_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.profile_extrude_skill.run_plan(normalized)

    def run_gear_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.gear_skill.run_plan(normalized)

    def run_gear_pair_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.gear_pair_skill.run_plan(normalized)

    def run_sweep_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.sweep_loft_skill.run_plan(normalized, "sweep")

    def run_loft_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.sweep_loft_skill.run_plan(normalized, "loft")

    def run_sheet_metal_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.sheet_metal_skill.run_plan(normalized)

    def run_weldment_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.weldment_skill.run_plan(normalized)

    def run_freeform_surface_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.freeform_surface_skill.run_plan(normalized)

    def run_feature_management_plan(self, plan: dict, feature_type: str) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.feature_management_skill.run_plan(normalized, feature_type)

    def run_shell_plan(self, plan: dict) -> SkillResult:
        return self.run_feature_management_plan(plan, "shell")

    def run_draft_plan(self, plan: dict) -> SkillResult:
        return self.run_feature_management_plan(plan, "draft")

    def run_reference_geometry_plan(self, plan: dict) -> SkillResult:
        return self.run_feature_management_plan(plan, "reference_geometry")

    def run_parametric_management_plan(self, plan: dict, feature_type: str) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.parametric_management_skill.run_plan(normalized, feature_type)

    def run_configuration_plan(self, plan: dict) -> SkillResult:
        return self.run_parametric_management_plan(plan, "configuration")

    def run_equation_plan(self, plan: dict) -> SkillResult:
        return self.run_parametric_management_plan(plan, "equation")

    def run_assembly_mate_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.assembly_mate_skill.run_plan(normalized)

    def save_active_part(
        self,
        run_dir: Path,
        target_path: str | None = None,
        *,
        validated_plan: dict[str, Any] | None = None,
    ) -> SkillResult:
        if validated_plan is None:
            return SkillResult(
                False,
                "cad_ir_validation_failed",
                data={
                    "success": False,
                    "error": "cad_ir_validation_failed",
                    "gate": "skill_manager_boundary",
                    "errors": [{
                        "code": "missing_validated_plan",
                        "message": "save_active_part requires a CAD-IR validated plan.",
                        "feature_id": None,
                        "field": "validated_plan",
                    }],
                    "files": [],
                },
            )
        _normalized, failure = self._guard_cad_ir_plan(validated_plan)
        return failure or self.solidworks_automation.save_active_part(run_dir, target_path=target_path)

    def run_thread_plan(self, plan: dict) -> SkillResult:
        normalized, failure = self._guard_cad_ir_plan(plan)
        return failure or self.thread_skill.run_plan(normalized)

    def run_fillet_chamfer_plan(self, plan: dict, *, allow_test_template: bool = False) -> SkillResult:
        """Legacy test-template entry. Never register this for production tasks."""
        if not allow_test_template:
            return SkillResult(
                False,
                "legacy_test_entry_blocked",
                data={"success": False, "error": "legacy_test_entry_blocked", "files": []},
            )
        return self.fillet_chamfer_skill.run_plan(plan)

    def _guard_cad_ir_plan(self, plan: dict[str, Any]) -> tuple[dict[str, Any], SkillResult | None]:
        """Reject direct production calls that bypass Planner/Pipeline gates."""

        planner_gate = PlannerValidator.execution_gate(plan)
        if not planner_gate["success"]:
            payload = {
                "success": False,
                "error": "planner_validation_failed",
                "gate": "skill_manager_planner_boundary",
                "planner_gate": planner_gate,
                "files": [],
            }
            return dict(plan), SkillResult(
                False,
                "planner_validation_failed",
                data=payload,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        result = CADIRCompiler().compile(plan)
        if result.success:
            normalized = result.design
            requested_ids = {
                str(value).strip()
                for value in list(plan.get("execution_feature_ids") or [])
                if str(value).strip()
            }
            if requested_ids:
                available = {
                    str(feature.get("id") or feature.get("name") or "").strip()
                    for feature in list(normalized.get("features") or [])
                }
                missing = sorted(requested_ids - available)
                if missing:
                    payload = {
                        "success": False,
                        "error": "execution_feature_batch_not_found",
                        "missing_feature_ids": missing,
                        "files": [],
                    }
                    return normalized, SkillResult(
                        False,
                        "execution_feature_batch_not_found",
                        data=payload,
                        output=json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                normalized["features"] = [
                    feature
                    for feature in list(normalized.get("features") or [])
                    if str(feature.get("id") or feature.get("name") or "").strip() in requested_ids
                ]
                normalized["execution_feature_ids"] = sorted(requested_ids)
            return normalized, None
        payload = {
            "success": False,
            "error": "cad_ir_validation_failed",
            "gate": "skill_manager_boundary",
            "cad_ir": result.ir,
            "errors": result.errors,
            "warnings": result.warnings,
            "files": [],
        }
        return result.design, SkillResult(
            False,
            "cad_ir_validation_failed",
            data=payload,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )

    @staticmethod
    def production_skill_contracts() -> dict[str, dict[str, Any]]:
        return {
            "source_part_clone": {
                "capabilities": ["native_sldprt_clone", "editable_feature_tree_preservation", "source_parity_validation"],
                "side_effects": ["opens_source_read_only", "creates_independent_native_part"],
                "output_types": ["SLDPRT"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "base_plate": {
                "capabilities": ["base_plate"],
                "side_effects": [],
                "output_types": ["SLDPRT"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "gear": {
                "capabilities": ["spur_gear", "involute_profile", "bore", "keyway"],
                "side_effects": ["creates_new_doc"],
                "output_types": [],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "gear_pair": {
                "capabilities": ["spur_gear_pair", "center_distance", "gear_mate", "transmission_ratio", "interference_check"],
                "side_effects": ["creates_two_parts_and_one_assembly"],
                "output_types": ["SLDPRT", "SLDASM"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "revolve": {
                "capabilities": ["revolve", "revolve_base", "revolve_boss", "revolve_cut"],
                "side_effects": ["creates_new_doc_or_modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "profile_extrude": {
                "capabilities": [
                    "closed_profile_extrude",
                    "line_arc_circle_profiles",
                    "profile_base",
                    "profile_boss",
                    "profile_cut",
                    "offset_start_profile_boss",
                    "flipped_side_profile_cut",
                ],
                "side_effects": ["creates_new_doc_or_modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "sweep": {
                "capabilities": ["circular_profile_sweep", "swept_base", "swept_boss", "swept_cut"],
                "side_effects": ["creates_new_doc_or_modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "loft": {
                "capabilities": [
                    "closed_profile_loft",
                    "line_arc_profile_loft",
                    "profile_to_point_loft",
                    "explicit_fixed_section_plane",
                    "lofted_base",
                    "lofted_boss",
                    "lofted_cut",
                ],
                "side_effects": ["creates_new_doc_or_modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "sheet_metal": {
                "capabilities": ["sheet_metal", "base_flange", "edge_flange", "angled_edge_flange", "sketched_bend", "open_hem", "native_jog", "lofted_bend", "formed_helical_lofted_bend", "flat_pattern", "forming_tool_dimple", "flat_pattern_dxf"],
                "side_effects": ["creates_new_doc"],
                "output_types": [],
                "conditional_output_types": ["DXF"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "weldment": {
                "capabilities": ["weldment", "structural_member", "iso_profile", "solid_round_d4", "curved_wire_mesh", "wire_mesh_flat_bar_frame_20x3", "wire_mesh_notched_flat_bar_supports"],
                "side_effects": ["creates_new_doc"],
                "output_types": [],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "freeform_surface": {
                "capabilities": ["freeform_surface", "fill_surface", "closed_3d_spline_boundary"],
                "side_effects": ["creates_new_doc"],
                "output_types": [],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "reference_geometry": {
                "capabilities": ["offset_plane", "axis_two_planes"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "draft": {
                "capabilities": [
                    "neutral_plane_draft",
                    "outer_vertical_faces",
                    "explicit_planar_face_signatures",
                ],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "shell": {
                "capabilities": ["single_thickness_shell", "explicit_opening_faces"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "configuration": {
                "capabilities": ["create_configuration", "activate_configuration", "configuration_properties"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "equation": {
                "capabilities": ["global_variable", "dimension_equation", "configuration_scoped_equation"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "assembly_mate": {
                "capabilities": ["new_assembly", "active_assembly", "coincident", "concentric", "parallel", "distance", "gear"],
                "side_effects": ["creates_new_doc_or_modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "fillet": {
                "capabilities": ["fillet"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "through_hole": {
                "capabilities": ["through_hole"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "threaded_hole": {
                "capabilities": ["threaded_hole", "native_hole_wizard_tapped_hole"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "boss": {
                "capabilities": ["boss"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "pocket": {
                "capabilities": ["pocket", "pocket_corner_fillet"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "slot": {
                "capabilities": ["slot"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "chamfer": {
                "capabilities": ["chamfer"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "dome": {
                "capabilities": [
                    "native_dome",
                    "planar_face_signature_selection",
                ],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "external_thread": {
                "capabilities": [
                    "external_thread",
                    "iso_metric_external_cosmetic_thread",
                    "iso_metric_external_modeled_thread",
                ],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "linear_pattern": {
                "capabilities": ["linear_pattern"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "circular_pattern": {
                "capabilities": ["circular_pattern"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "mirror": {
                "capabilities": ["mirror"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "rib": {
                "capabilities": ["rib", "side_wall_stiffener"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "side_boss": {
                "capabilities": ["side_boss", "bearing_boss", "flange_boss"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "side_hole": {
                "capabilities": ["side_hole", "bearing_bore", "flange_hole_pattern"],
                "side_effects": ["modifies_active_doc"],
                "output_types": [],
                "modifies_active_doc": True,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "save_sldprt": {
                "capabilities": ["save_sldprt"],
                "side_effects": [],
                "output_types": ["SLDPRT"],
                "modifies_active_doc": False,
                "creates_new_doc": False,
                "exports_files": False,
                "uses_test_template": False,
            },
            "solidworks_drawing": {
                "capabilities": ["drawing", "section_view", "detail_view"],
                "side_effects": ["creates_new_doc"],
                "output_types": ["SLDDRW"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": False,
                "uses_test_template": False,
            },
            "step_export": {
                "capabilities": ["export_step"],
                "side_effects": ["exports_files"],
                "output_types": ["STEP"],
                "modifies_active_doc": False,
                "creates_new_doc": False,
                "exports_files": True,
                "uses_test_template": False,
            },
            "solidworks_cnc_fillet": {
                "capabilities": ["cnc", "fillet", "chamfer"],
                "side_effects": ["creates_new_doc", "exports_files", "uses_test_template"],
                "output_types": ["SLDPRT", "STEP"],
                "modifies_active_doc": False,
                "creates_new_doc": True,
                "exports_files": True,
                "uses_test_template": True,
            },
        }

    def skill_contract(self, skill_key: str) -> dict[str, Any] | None:
        return self.production_skill_contracts().get(skill_key)

    def run_autocad_plan(self, plan: dict) -> SkillResult:
        return self.autocad_skill.run_plan(plan)

    def run_autocad_annotation_plan(self, plan: dict) -> SkillResult:
        return self.autocad_annotation_skill.run_plan(plan)

    def run_autocad_preflight(self, launch: bool = False) -> SkillResult:
        return self.autocad_skill.run_preflight(launch=launch)

    def status_lines(self) -> list[str]:
        return [
            f"AI Brain: ready, provider={self.brain.design_planner.provider_name}",
            "Agent Pipeline Runner: ready",
            AgentsOrchestratorConfig.detect().status_line(),
            f"SolidWorks VibeCAD: {self._path_status(self.paths.vibecad_skill_dir)}, agent planner ready",
            f"SolidWorks Automation: {self._path_status(self.paths.solidworks_skill_dir)}",
            f"Thread Skill: {self._path_status(self.paths.threaded_holes_skill_dir)}",
            f"Fillet/Chamfer/CNC Skill: {self._path_status(self.paths.fillet_chamfer_skill_dir)}",
            f"AutoCAD Automation: {self._path_status(self.paths.autocad_skill_dir)}",
            f"AutoCAD Annotation Skill: {self._path_status(self.paths.autocad_skill_dir)}",
            "PDF2CAD Skill: ready, MinerU sidecar/CLI or local PDF text parser",
            "File2CAD: ready, DWG/DXF/STEP/IGES/STL/SolidWorks routing",
            f"Engineering Knowledge: ready, version={self.brain.design_planner.engineering_knowledge.version}",
            "SolidWorks Pattern/Mirror: active-model production executors ready",
            "SolidWorks Gearbox Features: side_boss/side_hole/rib production executors ready",
            "SolidWorks Revolve: explicit half-profile production executor ready",
            "SolidWorks Source Clone: native SLDPRT parity-preserving executor ready",
            "SolidWorks Sweep/Loft: explicit-path/profile production executors ready",
            "SolidWorks Sheet Metal: base-flange/30-150-degree edge-flange/FlatPattern executor ready",
            "SolidWorks Weldment: ISO structural-member production executor ready",
            "SolidWorks Freeform Surface: closed 3D spline Fill Surface executor ready",
            "SolidWorks Parameters: equation/configuration production executors ready",
            "SolidWorks Assembly: explicit component/mate production executor ready",
            "SolidWorks Mold/Reference: shell/draft/reference geometry production executors ready",
            f"Python COM: {self._module_status('pythoncom')}, win32com: {self._module_status('win32com')}",
        ]

    @staticmethod
    def _looks_like_vibecad_request(command: str) -> bool:
        lowered = command.lower()
        if "vibecad" in lowered or "text-to-cad" in lowered:
            return True
        has_dimension = any(mark in command for mark in ("x", "X", "×", "*", "长", "宽", "厚", "Φ", "φ", "Ø"))
        has_cad_noun = any(
            token in lowered
            for token in (
                "画",
                "建模",
                "生成",
                "零件",
                "钢板",
                "板",
                "孔",
                "支架",
                "安装座",
                "solidworks",
                "model",
                "plate",
                "bracket",
                "hole",
            )
        )
        return has_dimension and has_cad_noun

    @staticmethod
    def _path_status(path: Path) -> str:
        return "installed" if path.exists() else f"missing: {path}"

    @staticmethod
    def _module_status(name: str) -> str:
        return "available" if importlib.util.find_spec(name) else "missing"
