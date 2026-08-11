from __future__ import annotations

from typing import Any

from .agents_config import AgentsOrchestratorConfig
from ..cad_ir import CADIRCompiler
from ..skill_planner import SkillPlanner


class SkillRouterAgent:
    """Routes normalized Design JSON to the existing Skill Registry keys."""

    FEATURE_SKILL_MAP: dict[str, tuple[str, str, str, bool, str]] = {
        "source_part_clone": ("source_part_clone", "clone_source_part", "An explicit native SLDPRT is cloned and source-parity checked without geometric guessing.", True, "model_3d"),
        "base_plate": ("base_plate", "create_base_plate", "Base geometry is a production-only SolidWorks operation.", True, "model_3d"),
        "gear": ("gear", "create_spur_gear", "Explicit involute spur gears use the deterministic gear executor.", True, "model_3d"),
        "gear_pair": ("gear_pair", "create_spur_gear_pair", "A spur-gear pair creates two Parts and one native Gear Mate assembly.", True, "model_3d"),
        "revolve": ("revolve", "apply_revolve", "Explicit half-profile revolves use the deterministic SolidWorks revolve executor.", True, "model_3d"),
        "profile_extrude": ("profile_extrude", "apply_profile_extrude", "Closed line/arc/circle loops use the deterministic profile extrusion executor.", True, "model_3d"),
        "sweep": ("sweep", "apply_sweep", "Circular-profile sweeps require an explicit path and profile diameter.", True, "model_3d"),
        "loft": ("loft", "apply_loft", "Lofts require ordered closed sections with explicit offsets.", True, "model_3d"),
        "sheet_metal": ("sheet_metal", "apply_sheet_metal", "Sheet metal requires an explicit base flange and bounded flange, bend, or hem definitions.", True, "model_3d"),
        "weldment": ("weldment", "apply_weldment", "Weldments require an installed profile configuration and explicit grouped path segments.", True, "model_3d"),
        "freeform_surface": ("freeform_surface", "apply_freeform_surface", "Free-form fill surfaces require a closed ordered 3D spline boundary.", True, "model_3d"),
        "reference_geometry": ("reference_geometry", "apply_reference_geometry", "Reference planes and axes use explicit geometric constraints.", True, "model_3d"),
        "draft": ("draft", "apply_draft", "Draft requires an explicit neutral plane and face set.", True, "model_3d"),
        "shell": ("shell", "apply_shell", "Shell requires explicit wall thickness and removal faces.", True, "model_3d"),
        "configuration": ("configuration", "apply_configuration", "Part configurations require explicit names and options.", True, "model_3d"),
        "equation": ("equation", "apply_equation", "Equations require explicit SolidWorks expressions and configuration scope.", True, "model_3d"),
        "assembly_mate": ("assembly_mate", "apply_mates", "Assembly mates require explicit component paths and geometric entity selectors.", True, "model_3d"),
        "through_hole": ("through_hole", "apply_through_hole", "Plain holes modify the active production Part.", True, "model_3d"),
        "boss": ("boss", "apply_boss", "Boss geometry modifies the active production Part.", True, "model_3d"),
        "rib": ("rib", "apply_rib", "Ribs reinforce explicit side faces of the active production Part.", True, "model_3d"),
        "side_boss": ("side_boss", "apply_side_boss", "Bearing or flange bosses require side-face geometry execution.", True, "model_3d"),
        "side_hole": ("side_hole", "apply_side_hole", "Bearing bores and flange hole circles require side-face cuts.", True, "model_3d"),
        "pocket": ("pocket", "apply_pocket", "Pocket geometry modifies the active production Part.", True, "model_3d"),
        "slot": ("slot", "apply_slot", "Slot geometry modifies the active production Part.", True, "model_3d"),
        "threaded_hole": ("threaded_hole", "apply_threaded_hole", "Tapped holes require the native active-model Hole Wizard executor.", True, "model_3d"),
        "bolt_circle_pattern": ("solidworks_threaded_holes", "apply_threaded_holes", "PCD threaded-hole pattern routes through Thread Skill.", True, "model_3d"),
        "fillet": ("fillet", "apply_fillet", "Fillets route to the production fillet operation only.", True, "model_3d"),
        "chamfer": ("chamfer", "apply_chamfer", "Chamfers route to the production active-model operation only.", True, "model_3d"),
        "dome": ("dome", "apply_dome", "Domes require one explicitly resolved planar target face.", True, "model_3d"),
        "linear_pattern": ("linear_pattern", "apply_linear_pattern", "Linear patterns modify explicit seed features in the active Part.", True, "model_3d"),
        "circular_pattern": ("circular_pattern", "apply_circular_pattern", "Circular patterns require an explicit seed and geometric axis reference.", True, "model_3d"),
        "mirror": ("mirror", "apply_mirror", "Feature mirrors require explicit seeds and a reference plane.", True, "model_3d"),
        "drawing": ("solidworks_drawing", "generate_drawing", "Drawing views require the SolidWorks Drawing step.", True, "drawing"),
        "autocad_annotation": ("autocad_annotation", "annotate_dwg", "DWG annotation belongs to AutoCAD Annotation.", True, "autocad_annotation"),
    }

    OUTPUT_SKILL_MAP: dict[str, tuple[str, str, str, bool, str]] = {
        "STEP": ("step_export", "export_step", "STEP file requested.", True, "export_files"),
        "DWG": ("dwg_export", "export_dwg", "DWG output requested.", True, "export_files"),
        "DXF": ("dwg_export", "export_dwg", "DXF output requested.", True, "export_files"),
        "PDF": ("pdf_export", "export_pdf", "PDF output requested.", True, "export_files"),
        "Annotated DWG": ("autocad_annotation", "annotate_dwg", "Annotated DWG requested.", True, "autocad_annotation"),
    }

    def __init__(self, config: AgentsOrchestratorConfig | None = None) -> None:
        self.config = config or AgentsOrchestratorConfig.detect()
        self.sdk_agent = self.config.create_sdk_agent(
            "SkillRouterAgent",
            "Route normalized CAD Design JSON to registered CAD skills. Never silently ignore unsupported features.",
        )

    def route(self, design_json: dict[str, Any]) -> dict[str, Any]:
        cad_ir_result = CADIRCompiler().compile(design_json)
        design_json = cad_ir_result.design
        steps_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        unsupported = list(design_json.get("unsupported_features", []))
        requested_stages = set(design_json.get("requested_stages", []) or ["model_3d"])
        if not cad_ir_result.success or design_json.get("needs_confirmation"):
            return {
                "skill_pipeline": [],
                "unsupported_features": unsupported,
                "requested_stages": list(requested_stages),
                "forbidden_stages": list(design_json.get("forbidden_stages", [])),
                "stop_after": design_json.get("stop_after"),
                "needs_confirmation": True,
                "cad_ir_validation": design_json.get("cad_ir_validation", {}),
            }

        for feature in design_json.get("features", []):
            feature_type = str(feature.get("type", ""))
            route = self.FEATURE_SKILL_MAP.get(feature_type)
            if not route:
                unsupported.append({"type": feature_type, "reason": "No registered skill route for this feature type."})
                continue
            if route[4] not in requested_stages:
                unsupported.append({"type": feature_type, "reason": f"Filtered by requested_stages; stage {route[4]} was not requested."})
                continue
            if route[0] not in self._allowed_skills(design_json):
                unsupported.append({"type": feature_type, "reason": f"Production scope policy does not allow skill {route[0]!r} for this task."})
                continue
            self._add_step(steps_by_key, route, feature_type, design_json, feature)

        if "base_plate" in self._allowed_skills(design_json) and ("base_plate", "create_base_plate") not in steps_by_key:
            self._add_step(
                steps_by_key,
                ("base_plate", "create_base_plate", "3D body parameters require a production base Part.", True, "model_3d"),
                "body_parameters",
                design_json,
                {"name": "BaseModel", "type": "base_plate", "params": design_json.get("parameters", {}), "required": True},
            )
        if "drawing" in requested_stages and ("solidworks_drawing", "generate_drawing") not in steps_by_key:
            self._add_step(
                steps_by_key,
                ("solidworks_drawing", "generate_drawing", "Drawing stage was requested.", True, "drawing"),
                "stage:drawing",
                design_json,
                {"name": "DrawingViews", "type": "drawing", "params": {"views": ["front", "top", "right", "isometric"]}, "required": True},
            )
        if "autocad_annotation" in requested_stages and ("autocad_annotation", "annotate_dwg") not in steps_by_key:
            self._add_step(
                steps_by_key,
                ("autocad_annotation", "annotate_dwg", "AutoCAD annotation stage was requested.", True, "autocad_annotation"),
                "stage:autocad_annotation",
                design_json,
                {"name": "AutoCADAnnotation", "type": "autocad_annotation", "params": {}, "required": True},
            )

        for output in design_json.get("outputs", []):
            route = self.OUTPUT_SKILL_MAP.get(str(output))
            if route and route[4] in requested_stages:
                self._add_step(steps_by_key, route, f"output:{output}", design_json, {"type": "output", "params": {"format": output}})

        if "save_sldprt" in self._allowed_skills(design_json):
            self._add_step(
                steps_by_key,
                ("save_sldprt", "save_sldprt", "Save only the requested SLDPRT result.", True, "model_3d"),
                "output:SLDPRT",
                design_json,
                {"type": "output", "params": {"format": "SLDPRT"}},
            )

        ordered = []
        allowed_skills = self._allowed_skills(design_json)
        for skill_key in SkillPlanner.ordered_skill_keys(design_json, allowed_skills):
            ordered.extend(step for key, step in steps_by_key.items() if key[0] == skill_key)
        return {
            "skill_pipeline": ordered,
            "unsupported_features": unsupported,
            "requested_stages": list(requested_stages),
            "forbidden_stages": list(design_json.get("forbidden_stages", [])),
            "stop_after": design_json.get("stop_after"),
        }

    @staticmethod
    def _allowed_skills(design_json: dict[str, Any]) -> list[str]:
        return list(design_json.get("execution_policy", {}).get("allowed_skills", []))

    @staticmethod
    def _add_step(
        steps_by_key: dict[tuple[str, str], dict[str, Any]],
        route: tuple[str, str, str, bool, str],
        trigger: str,
        design_json: dict[str, Any],
        feature: dict[str, Any],
    ) -> None:
        skill_key, action, reason, required, stage = route
        key = (skill_key, action)
        routed_feature = {
            "name": feature.get("name", trigger),
            "type": feature.get("type", trigger),
            "params": feature.get("params", {}),
            "required": feature.get("required", required),
        }
        if key in steps_by_key:
            steps_by_key[key]["triggers"].append(trigger)
            steps_by_key[key]["parameters"]["features"].append(routed_feature)
            return
        steps_by_key[key] = {
            "skill_key": skill_key,
            "action": action,
            "reason": reason,
            "triggers": [trigger],
            "required": required,
            "stage": stage,
            "parameters": {
                "unit": design_json.get("parameters", {}).get("unit", "mm"),
                "part_parameters": design_json.get("parameters", {}),
                "features": [routed_feature],
                "outputs": design_json.get("outputs", []),
            },
            "inputs": {"design_json": design_json},
        }
