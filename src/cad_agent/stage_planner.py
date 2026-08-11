from __future__ import annotations

import re
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_pattern_skill import ActiveModelPatternSkill
from .advanced_feature_skill import AdvancedFeatureSkill
from .feature_management_skill import FeatureManagementSkill
from .gear_skill import GearSkill
from .gear_pair_skill import GearPairSkill
from .profile_extrude_skill import ProfileExtrudeSkill
from .revolve_skill import RevolveSkill
from .sweep_loft_skill import SweepLoftSkill
from .sheet_metal_skill import SheetMetalSkill
from .weldment_skill import WeldmentSkill
from .freeform_surface_skill import FreeformSurfaceSkill
from .parametric_management_skill import ParametricManagementSkill
from .assembly_mate_skill import AssemblyMateSkill
from .source_part_clone_skill import SourcePartCloneSkill
from .thread_skill import ThreadSkill


STAGE_ORDER = ("model_3d", "drawing", "autocad_annotation", "export_files")

MODE_TO_STAGE = {
    "auto": None,
    "model_3d": "model_3d",
    "modify_3d": "model_3d",
    "create_drawing": "drawing",
    "annotate_drawing": "autocad_annotation",
    "export_files": "export_files",
    "full_pipeline": "full_pipeline",
}

STAGE_LABELS = {
    "model_3d": "仅3D建模",
    "drawing": "工程图",
    "autocad_annotation": "AutoCAD / DWG 标注",
    "export_files": "文件导出",
}

STAGE_OUTPUTS = {
    "model_3d": {"SLDPRT", "SLDASM"},
    "drawing": {"SLDDRW"},
    "autocad_annotation": {"Annotated DWG", "annotation_report_json"},
    "export_files": {"STEP", "PDF", "DWG", "DXF", "Annotated DWG"},
}

EXPORT_TOKENS = {
    "STEP": ("step", "stp", "导出step", "输出step"),
    "PDF": ("pdf", "导出pdf", "输出pdf"),
    "DWG": ("dwg", "导出dwg", "输出dwg"),
    "DXF": ("dxf", "导出dxf", "输出dxf"),
    "Annotated DWG": ("annotated dwg", "标注dwg"),
}

MODEL_FEATURE_TYPES = {
    "source_part_clone",
    "base_plate",
    "through_hole",
    "threaded_hole",
    "external_thread",
    "bolt_circle_pattern",
    "boss",
    "pocket",
    "slot",
    "fillet",
    "chamfer",
    "dome",
    "linear_pattern",
    "circular_pattern",
    "mirror",
    "side_boss",
    "side_hole",
    "rib",
    "gear",
    "gear_pair",
    "revolve",
    "profile_extrude",
    "sweep",
    "loft",
    "sheet_metal",
    "weldment",
    "freeform_surface",
    "shell",
    "draft",
    "reference_geometry",
    "configuration",
    "equation",
    "assembly_mate",
}

DRAWING_FEATURE_TYPES = {"drawing", "drawing_dimension", "section_view", "detail_view"}
ANNOTATION_FEATURE_TYPES = {"autocad_annotation"}


def infer_stage_plan(prompt: str, mode: str = "auto") -> dict[str, Any]:
    text = prompt.strip()
    intent_text = _without_negated_stage_requests(text)
    lower = intent_text.lower()
    forced = MODE_TO_STAGE.get(mode, None)
    full = forced == "full_pipeline" or _has_any(lower, ("完整生成", "全部完成", "完整流程", "全流程", "从建模到工程图", "from model to drawing"))

    if full:
        requested = ["model_3d", "drawing", "autocad_annotation", "export_files"]
        task_type = "full_pipeline"
    elif forced in STAGE_ORDER:
        requested = [forced]
        task_type = mode if mode != "auto" else forced
    else:
        requested, task_type = _infer_requested_stages(intent_text, lower)

    requested = _dedupe_preserve_order(requested)
    forbidden = [stage for stage in STAGE_ORDER if stage not in requested]
    stop_after = requested[-1] if requested else None
    requested_outputs = requested_outputs_for_prompt(intent_text, requested)
    needs_confirmation = _needs_confirmation(intent_text, requested)
    if needs_confirmation:
        forbidden = list(STAGE_ORDER)
        stop_after = None

    return {
        "task_type": task_type,
        "requested_stages": requested,
        "forbidden_stages": forbidden,
        "stop_after": stop_after,
        "requested_outputs": requested_outputs,
        "needs_confirmation": needs_confirmation,
        "confirmation_reason": "模型参数不完整，不能安全启动 SolidWorks。" if needs_confirmation else "",
        "stage_mode": mode,
    }


def apply_stage_plan(design: dict[str, Any], stage_plan: dict[str, Any]) -> dict[str, Any]:
    updated = dict(design)
    planner_needs_confirmation = bool(updated.get("needs_confirmation", False))
    planner_confirmation_reason = str(updated.get("confirmation_reason") or "")
    requested = list(stage_plan.get("requested_stages", []))
    updated.update(
        {
            "task_type": stage_plan.get("task_type", "model_3d"),
            "requested_stages": requested,
            "forbidden_stages": list(stage_plan.get("forbidden_stages", [])),
            "stop_after": stage_plan.get("stop_after"),
            "needs_confirmation": planner_needs_confirmation or bool(stage_plan.get("needs_confirmation", False)),
            "confirmation_reason": stage_plan.get("confirmation_reason", "") or planner_confirmation_reason,
        }
    )
    outputs = list(stage_plan.get("requested_outputs", []))
    has_new_assembly = any(
        feature.get("type") == "assembly_mate"
        and AssemblyMateSkill.normalize_request(feature.get("params", {})).get("mode") == "new_assembly"
        for feature in updated.get("features", [])
    )
    has_gear_pair = any(feature.get("type") == "gear_pair" for feature in updated.get("features", []))
    if has_gear_pair:
        outputs = _dedupe_preserve_order(outputs + ["SLDPRT", "SLDASM"])
    elif has_new_assembly:
        outputs = ["SLDASM" if output == "SLDPRT" else output for output in outputs]
    if not outputs:
        outputs = _filter_outputs_by_stage(updated.get("outputs", []), requested)
    requested_outputs = _dedupe_preserve_order(outputs)
    if stage_plan.get("task_type") == "modify_3d" and "SLDPRT" in requested_outputs and "save" not in str(updated.get("source_brief", "")).lower():
        requested_outputs.remove("SLDPRT")
    updated["outputs"] = _dedupe_preserve_order(requested_outputs + ["brain_plan_json", "design_plan_json", "pipeline_report_json"])
    updated["features"] = _filter_features_by_stage(updated.get("features", []), requested)
    unexecutable = _unexecutable_required_features(updated, requested)
    if unexecutable:
        updated["needs_confirmation"] = True
        updated["confirmation_reason"] = "Required production features are not implemented: " + ", ".join(
            str(item["type"]) for item in unexecutable
        )
        existing_unsupported = list(updated.get("unsupported_features", []))
        existing_unsupported.extend(unexecutable)
        updated["unsupported_features"] = _dedupe_feature_entries(existing_unsupported)
    allowed_skills = [] if unexecutable or updated["needs_confirmation"] else _allowed_skills(updated, requested, stage_plan.get("task_type", "model_3d"), requested_outputs)
    updated["execution_policy"] = {
        "minimum_execution": True,
        "requires_user_confirmation": True,
        "requested_stages": requested,
        "forbidden_stages": updated["forbidden_stages"],
        "stop_after": updated["stop_after"],
        "allowed_skills": allowed_skills,
        "expected_outputs": requested_outputs,
        "unexecutable_required_features": unexecutable,
    }
    return updated


def _unexecutable_required_features(design: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    if "model_3d" not in requested:
        return []
    missing: list[dict[str, Any]] = []
    for feature in design.get("features", []):
        if not feature.get("required", True):
            continue
        feature_type = str(feature.get("type", ""))
        params = feature.get("params", {})
        normalization_error = ""
        if feature_type == "source_part_clone" and SourcePartCloneSkill.normalize_request(params).get("success"):
            continue
        if feature_type == "base_plate":
            continue
        if feature_type == "fillet" and _supported_fillet(feature, params):
            continue
        if feature_type == "through_hole" and _supported_through_hole(params):
            continue
        if feature_type == "threaded_hole" and ThreadSkill.normalize_request(params).get("success"):
            continue
        if feature_type == "boss" and _supported_boss(params):
            continue
        if feature_type == "pocket" and _supported_pocket(params):
            continue
        if feature_type == "slot" and _supported_slot(params):
            continue
        if feature_type == "chamfer" and _supported_chamfer(params):
            continue
        if feature_type == "dome" and ActiveModelFeatureSkill.normalize_dome_request(params).get("success"):
            continue
        if feature_type == "external_thread" and ActiveModelFeatureSkill.normalize_external_thread_request(params).get("success"):
            continue
        if feature_type in {"side_boss", "side_hole", "rib"} and AdvancedFeatureSkill.normalize_request(feature_type, params).get("success"):
            continue
        if feature_type in {"linear_pattern", "circular_pattern", "mirror"} and ActiveModelPatternSkill.normalize_request(feature_type, params).get("success"):
            continue
        if feature_type == "revolve":
            revolve_request = RevolveSkill.normalize_request(params, design.get("task_type"))
            if revolve_request.get("success"):
                continue
            normalization_error = str(revolve_request.get("message") or "").strip()
        if feature_type == "profile_extrude" and ProfileExtrudeSkill.normalize_request(
            params, design.get("task_type")
        ).get("success"):
            continue
        if feature_type == "gear" and GearSkill.normalize_request(params, design.get("task_type")).get("success"):
            continue
        if feature_type == "gear_pair" and GearPairSkill.normalize_request(params, design.get("task_type")).get("success"):
            continue
        if feature_type in {"sweep", "loft"} and SweepLoftSkill.normalize_request(
            feature_type, params, design.get("task_type")
        ).get("success"):
            continue
        if feature_type == "sheet_metal" and SheetMetalSkill.normalize_request(
            params, design.get("task_type")
        ).get("success"):
            continue
        if feature_type == "weldment":
            weldment_request = WeldmentSkill.normalize_request(params, design.get("task_type"))
            if weldment_request.get("success"):
                continue
            normalization_error = str(weldment_request.get("message") or "").strip()
        if feature_type == "freeform_surface" and FreeformSurfaceSkill.normalize_request(
            params, design.get("task_type")
        ).get("success"):
            continue
        if feature_type in {"shell", "draft", "reference_geometry"} and FeatureManagementSkill.normalize_request(feature_type, params).get("success"):
            continue
        if feature_type in {"configuration", "equation"} and ParametricManagementSkill.normalize_request(
            feature_type, params
        ).get("success"):
            continue
        if feature_type == "assembly_mate" and AssemblyMateSkill.normalize_request(params).get("success"):
            continue
        missing.append(
            {
                "name": feature.get("name") or feature_type,
                "type": feature_type,
                "params": params,
                "reason": (
                    f"Production executor rejected the feature parameters: {normalization_error}"
                    if normalization_error
                    else "No compatible production executor is registered for this required feature."
                ),
            }
        )
    return missing


def _supported_through_hole(params: dict[str, Any]) -> bool:
    position = str(params.get("position") or "").lower()
    count = int(params.get("count", 1) or 1)
    if position == "center" and count == 1:
        return True
    placement = str(params.get("placement") or ("corner_offsets" if params.get("edge_offsets") else "")).lower()
    offsets = params.get("edge_offsets_mm") or params.get("edge_offsets")
    return (
        placement == "corner_offsets"
        and count == 4
        and isinstance(offsets, dict)
        and float(offsets.get("x", 0) or 0) > float(params.get("diameter", 0) or 0) / 2.0
        and float(offsets.get("y", 0) or 0) > float(params.get("diameter", 0) or 0) / 2.0
    )


def _supported_boss(params: dict[str, Any]) -> bool:
    return (
        min(float(params.get(key, 0) or 0) for key in ("length", "width", "height")) > 0
        and str(params.get("position") or "center_top") in {"center", "center_top"}
    )


def _supported_pocket(params: dict[str, Any]) -> bool:
    length = float(params.get("length", 0) or 0)
    width = float(params.get("width", 0) or 0)
    depth = float(params.get("depth", 0) or 0)
    radius = float(params.get("corner_radius", 0) or 0)
    return min(length, width, depth) > 0 and 0 <= radius < min(length, width) / 2.0


def _supported_slot(params: dict[str, Any]) -> bool:
    count = int(params.get("count", 0) or 0)
    length = float(params.get("length", 0) or 0)
    width = float(params.get("width", 0) or 0)
    offsets = params.get("centerline_offsets", [])
    orientation = str(params.get("orientation") or "y").lower()
    return count > 0 and len(offsets) == count and length > width > 0 and orientation in {"x", "y"}


def _supported_chamfer(params: dict[str, Any]) -> bool:
    if float(params.get("size", 0) or 0) <= 0:
        return False
    selector = str(params.get("targets") or "all_outer_edges").strip().lower()
    if selector in {"all_outer_edges", "outer_edges"}:
        return True
    if selector in {
        "bearing_end_edges",
        "axial_ring_end_edges",
        "axial_outer_ring_end_edges",
        "axial_bearing_boundary_end_edges",
        "axial_outer_end_edges",
    }:
        return str(params.get("axis") or "x").strip().lower() in {"x", "y", "z", "horizontal", "vertical"}
    if selector in {"axial_min_outer_end_edge", "axial_max_outer_end_edge"}:
        diameter = float(params.get("diameter", params.get("diameter_mm", 0)) or 0)
        return diameter > 0 and str(params.get("axis") or "x").strip().lower() in {
            "x", "y", "z", "horizontal", "vertical",
        }
    if selector == "explicit_edge_signatures":
        signatures = params.get("edge_signatures")
        tolerance = float(params.get("edge_tolerance_mm", 0) or 0)
        if not isinstance(signatures, list) or not signatures or tolerance <= 0:
            return False
        for signature in signatures:
            if not isinstance(signature, dict):
                return False
            curve_type = str(signature.get("curve_type") or "").strip().lower()
            if curve_type == "circle" and signature.get("start_mm") is None and signature.get("end_mm") is None:
                center = signature.get("center_mm")
                axis = signature.get("axis")
                try:
                    if not isinstance(center, (tuple, list)) or len(center) != 3:
                        return False
                    [float(value) for value in center]
                    if float(signature.get("radius_mm", 0) or 0) <= 0:
                        return False
                    if axis is not None:
                        if not isinstance(axis, (tuple, list)) or len(axis) != 3:
                            return False
                        axis_values = [float(value) for value in axis]
                        if not any(abs(value) > 0 for value in axis_values):
                            return False
                except (TypeError, ValueError):
                    return False
                continue
            for key in ("start_mm", "end_mm"):
                point = signature.get(key)
                if not isinstance(point, (tuple, list)) or len(point) != 3:
                    return False
                try:
                    [float(value) for value in point]
                except (TypeError, ValueError):
                    return False
        return True
    return False


def _supported_fillet(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    radius = float(params.get("radius", 0) or 0)
    selector = str(
        params.get("edge_selector")
        or params.get("target")
        or params.get("targets")
        or "outer_edges"
    ).strip().lower()
    if radius <= 0:
        return False
    if selector in {"outer_edges", "outer_vertical_edges", "four_outer_corners"}:
        return True
    if selector in {"feature_edges", "all_feature_edges", "all_edges_of_feature"}:
        target_reference = feature.get("target_reference")
        return bool(
            isinstance(target_reference, dict)
            and str(target_reference.get("feature_id") or "").strip()
        )
    if selector in {
        "bearing_end_edges",
        "axial_ring_end_edges",
        "axial_outer_ring_end_edges",
        "axial_bearing_boundary_end_edges",
    }:
        return str(params.get("axis") or "x").strip().lower() in {"x", "y", "z", "horizontal", "vertical"}
    if selector == "explicit_edge_signatures":
        signatures = params.get("edge_signatures")
        if not isinstance(signatures, list) or not signatures:
            return False
        for signature in signatures:
            if not isinstance(signature, dict):
                return False
            curve_type = str(signature.get("curve_type") or "").strip().lower()
            if curve_type == "circle" and signature.get("start_mm") is None and signature.get("end_mm") is None:
                center = signature.get("center_mm")
                axis = signature.get("axis")
                try:
                    if not isinstance(center, (tuple, list)) or len(center) != 3:
                        return False
                    [float(value) for value in center]
                    if float(signature.get("radius_mm", 0) or 0) <= 0:
                        return False
                    if axis is not None:
                        if not isinstance(axis, (tuple, list)) or len(axis) != 3:
                            return False
                        axis_values = [float(value) for value in axis]
                        if not any(abs(value) > 0 for value in axis_values):
                            return False
                except (TypeError, ValueError):
                    return False
                continue
            for key in ("start_mm", "end_mm"):
                point = signature.get(key)
                if not isinstance(point, (tuple, list)) or len(point) != 3:
                    return False
                try:
                    [float(value) for value in point]
                except (TypeError, ValueError):
                    return False
        return float(params.get("edge_tolerance_mm", 0.03) or 0.03) > 0
    return False


def _dedupe_feature_entries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("type", "")), str(item.get("name", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _allowed_skills(design: dict[str, Any], requested: list[str], task_type: str, outputs: list[str]) -> list[str]:
    feature_types = {str(feature.get("type", "")) for feature in design.get("features", [])}
    allowed: list[str] = []
    if "model_3d" in requested:
        has_new_shape_base = any(
            (
                feature.get("type") == "source_part_clone"
                and SourcePartCloneSkill.normalize_request(feature.get("params", {})).get("success")
            )
            or (
                feature.get("type") == "revolve"
                and RevolveSkill.normalize_request(feature.get("params", {}), task_type).get("mode") == "new_model"
            )
            or (
                feature.get("type") == "profile_extrude"
                and ProfileExtrudeSkill.normalize_request(
                    feature.get("params", {}), task_type
                ).get("mode") == "new_model"
            )
            or (
                feature.get("type") == "gear"
                and GearSkill.normalize_request(feature.get("params", {}), task_type).get("mode") == "new_model"
            )
            or (
                feature.get("type") in {"sweep", "loft"}
                and SweepLoftSkill.normalize_request(
                    str(feature.get("type")), feature.get("params", {}), task_type
                ).get("mode") == "new_model"
            )
            or (
                feature.get("type") == "sheet_metal"
                and SheetMetalSkill.normalize_request(feature.get("params", {}), task_type).get("mode") == "new_model"
            )
            or (
                feature.get("type") == "weldment"
                and WeldmentSkill.normalize_request(feature.get("params", {}), task_type).get("mode") == "new_model"
            )
            or (
                feature.get("type") == "freeform_surface"
                and FreeformSurfaceSkill.normalize_request(feature.get("params", {}), task_type).get("mode") == "new_model"
            )
            for feature in design.get("features", [])
        )
        has_assembly_task = bool(feature_types & {"assembly_mate", "gear_pair"})
        if task_type != "modify_3d" and not has_new_shape_base and not has_assembly_task:
            allowed.append("base_plate")
        if "source_part_clone" in feature_types:
            allowed.append("source_part_clone")
        if "revolve" in feature_types:
            allowed.append("revolve")
        if "profile_extrude" in feature_types:
            allowed.append("profile_extrude")
        if "dome" in feature_types:
            allowed.append("dome")
        if "gear" in feature_types:
            allowed.append("gear")
        if "gear_pair" in feature_types:
            allowed.append("gear_pair")
        if "sweep" in feature_types:
            allowed.append("sweep")
        if "loft" in feature_types:
            allowed.append("loft")
        if "sheet_metal" in feature_types:
            allowed.append("sheet_metal")
        if "weldment" in feature_types:
            allowed.append("weldment")
        if "freeform_surface" in feature_types:
            allowed.append("freeform_surface")
        # Preserve the stable production order: outer corners before through cuts.
        if "fillet" in feature_types:
            allowed.append("fillet")
        if "boss" in feature_types:
            allowed.append("boss")
        if "rib" in feature_types:
            allowed.append("rib")
        if "side_boss" in feature_types:
            allowed.append("side_boss")
        if "reference_geometry" in feature_types:
            allowed.append("reference_geometry")
        if "draft" in feature_types:
            allowed.append("draft")
        if "shell" in feature_types:
            allowed.append("shell")
        if "configuration" in feature_types:
            allowed.append("configuration")
        if "equation" in feature_types:
            allowed.append("equation")
        if "assembly_mate" in feature_types:
            allowed.append("assembly_mate")
        # Apply outer edge treatment while the topology is still simple.
        if "chamfer" in feature_types:
            allowed.append("chamfer")
        if "external_thread" in feature_types:
            allowed.append("external_thread")
        # Holes follow the boss so center holes cut through the complete part;
        # corner holes explicitly target the base-top face.
        if "through_hole" in feature_types:
            allowed.append("through_hole")
        if "threaded_hole" in feature_types:
            allowed.append("threaded_hole")
        if "side_hole" in feature_types:
            allowed.append("side_hole")
        if "pocket" in feature_types:
            allowed.append("pocket")
        if "slot" in feature_types:
            allowed.append("slot")
        if "linear_pattern" in feature_types:
            allowed.append("linear_pattern")
        if "circular_pattern" in feature_types:
            allowed.append("circular_pattern")
        if "mirror" in feature_types:
            allowed.append("mirror")
        if task_type != "modify_3d" and not has_assembly_task:
            allowed.append("save_sldprt")
    if "drawing" in requested:
        allowed.append("solidworks_drawing")
    if "autocad_annotation" in requested:
        allowed.append("autocad_annotation")
    if "export_files" in requested:
        if "STEP" in outputs:
            allowed.append("step_export")
        if "PDF" in outputs:
            allowed.append("pdf_export")
        native_flat_pattern_dxf = any(
            feature.get("type") == "sheet_metal"
            and SheetMetalSkill.normalize_request(feature.get("params", {}), task_type)
            .get("flat_pattern", {})
            .get("export_dxf", {})
            .get("enabled", False)
            for feature in design.get("features", [])
        )
        if "DWG" in outputs or ("DXF" in outputs and not native_flat_pattern_dxf):
            allowed.append("dwg_export")
    return _dedupe_preserve_order(allowed)


def requested_outputs_for_prompt(prompt: str, stages: list[str]) -> list[str]:
    lower = prompt.lower()
    outputs: list[str] = []
    if "model_3d" in stages:
        outputs.append("SLDPRT")
    if "drawing" in stages:
        outputs.append("SLDDRW")
    if "export_files" in stages:
        for output, tokens in EXPORT_TOKENS.items():
            if any(token in lower for token in tokens):
                outputs.append(output)
    if "autocad_annotation" in stages:
        outputs.append("Annotated DWG")
        outputs.append("annotation_report_json")
    return _dedupe_preserve_order(outputs)


def stage_summary(design: dict[str, Any]) -> dict[str, Any]:
    requested = list(design.get("requested_stages", []))
    outputs = list(design.get("outputs", []))
    plan_lines = []
    skipped = []
    if "model_3d" in requested:
        plan_lines.extend(_model_plan_lines(design))
    if "drawing" in requested:
        plan_lines.append("生成三视图/等轴测工程图")
        special_views = design.get("drawing_plan", {}).get("special_views", [])
        if any(item.get("type") == "section_view" for item in special_views):
            plan_lines.append("创建中心剖视图")
        if any(item.get("type") == "detail_view" for item in special_views):
            plan_lines.append("创建局部放大图")
    if "autocad_annotation" in requested:
        plan_lines.append("AutoCAD 打开 DWG 并自动标注")
    if "export_files" in requested:
        for output in outputs:
            if output in {"STEP", "PDF", "DWG", "DXF", "Annotated DWG"}:
                plan_lines.append(f"导出 {output}")
    stage_names = {"drawing": "工程图", "autocad_annotation": "AutoCAD", "export_files": "PDF/DWG/STEP 导出"}
    for stage in design.get("forbidden_stages", []):
        skipped.append(stage_names.get(stage, STAGE_LABELS.get(stage, stage)))
    planning_validation = dict(design.get("planning_validation") or {})
    planning_preview = dict(design.get("planning_preview") or {})
    return {
        "task_type": design.get("task_type"),
        "requested_stages": requested,
        "stop_after": design.get("stop_after"),
        "plan_lines": _dedupe_preserve_order(plan_lines),
        "skipped_lines": _dedupe_preserve_order(skipped),
        "outputs": outputs,
        "needs_confirmation": bool(design.get("needs_confirmation", False)),
        "confirmation_reason": design.get("confirmation_reason", ""),
        "unexecutable_required_features": design.get("execution_policy", {}).get("unexecutable_required_features", []),
        "planning_contract": design.get("planning_contract"),
        "planning_validation": planning_validation,
        "planning_preview": planning_preview,
        "part_type": planning_preview.get("part_type") or design.get("part_family"),
        "planner_status": planning_validation.get("status"),
        "confirmation_allowed": planning_validation.get("confirmation_allowed", not design.get("needs_confirmation", False)),
        "allow_execution": planning_validation.get("allow_pipeline", not design.get("needs_confirmation", False)),
    }


def _infer_requested_stages(text: str, lower: str) -> tuple[list[str], str]:
    has_drawing = _has_any(lower, ("工程图", "三视图", "slddrw", "drawing"))
    has_annotation = _has_any(lower, ("自动标注", "尺寸标注", "autocad标注", "annotat"))
    has_export = _has_export_intent(lower)
    has_model = _has_model_intent(text, lower)
    only_current_model_drawing = has_drawing and _has_any(lower, ("当前模型", "active model"))
    only_current_drawing_export = has_export and _has_any(lower, ("当前工程图", "当前图纸", "active drawing"))

    if only_current_drawing_export:
        return ["export_files"], "export_files"
    if only_current_model_drawing:
        return ["drawing"], "create_drawing"
    if has_annotation and not has_model and not has_drawing:
        return ["autocad_annotation"], "annotate_drawing"
    if has_drawing and not has_model:
        return ["drawing"], "create_drawing"
    if has_export and not has_model and not has_drawing:
        return ["export_files"], "export_files"

    requested = ["model_3d"] if has_model else []
    if has_drawing:
        requested.append("drawing")
    if has_annotation:
        requested.append("autocad_annotation")
    if has_export:
        requested.append("export_files")
    if requested == ["model_3d"]:
        return requested, "model_3d"
    if requested:
        return requested, "full_pipeline" if len(requested) == 4 else requested[-1]
    return ["model_3d"], "model_3d"


def _without_negated_stage_requests(text: str) -> str:
    """Remove explicit negative stage clauses before positive intent scans."""
    cleaned = text
    patterns = (
        r"(?:不|不要|无需|不需要|禁止)(?:自动)?(?:生成|创建)?\s*(?:三视图)?工程图",
        r"(?:不|不要|无需|不需要|禁止)(?:启动|打开|使用)?\s*AutoCAD",
        r"(?:不|不要|无需|不需要|禁止)(?:自动)?(?:导出|输出)[^。；\n]*",
        r"\b(?:do\s+not|don't|without)\s+(?:generate|create|export|open|launch|use)\b[^.;\n]*",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _filter_features_by_stage(features: list[dict[str, Any]], requested: list[str]) -> list[dict[str, Any]]:
    allowed: set[str] = set()
    if "model_3d" in requested:
        allowed |= MODEL_FEATURE_TYPES
    if "drawing" in requested:
        allowed |= DRAWING_FEATURE_TYPES
    if "autocad_annotation" in requested:
        allowed |= ANNOTATION_FEATURE_TYPES
    return [feature for feature in features if str(feature.get("type", "")) in allowed]


def _filter_outputs_by_stage(outputs: list[str], requested: list[str]) -> list[str]:
    allowed: set[str] = set()
    for stage in requested:
        allowed |= STAGE_OUTPUTS.get(stage, set())
    return [output for output in outputs if output in allowed]


def _model_plan_lines(design: dict[str, Any]) -> list[str]:
    labels = {
        "gear": "Create involute spur gear",
        "gear_pair": "Create and mate spur-gear pair",
        "base_plate": "创建底板",
        "through_hole": "创建通孔",
        "threaded_hole": "创建螺纹孔",
        "external_thread": "创建 ISO 外螺纹",
        "bolt_circle_pattern": "创建孔阵列",
        "boss": "创建凸台",
        "pocket": "创建型腔",
        "slot": "创建槽",
        "fillet": "创建圆角",
        "chamfer": "创建倒角",
        "dome": "创建圆顶",
        "linear_pattern": "创建线性阵列",
        "circular_pattern": "创建圆周阵列",
        "mirror": "创建镜像特征",
        "side_boss": "创建侧向轴承座",
        "side_hole": "创建侧向轴孔/法兰孔",
        "rib": "创建加强筋",
        "revolve": "创建旋转特征",
        "profile_extrude": "创建任意闭合轮廓拉伸",
        "sweep": "创建扫描特征",
        "loft": "创建放样特征",
        "sheet_metal": "创建钣金基体、受约束折弯、折边、转折或放样折弯",
        "weldment": "创建焊件结构构件",
        "freeform_surface": "创建自由填充曲面",
        "reference_geometry": "创建基准几何",
        "draft": "创建拔模特征",
        "shell": "创建抽壳特征",
        "configuration": "创建零件配置",
        "equation": "创建参数方程",
        "assembly_mate": "创建装配体与配合",
    }
    lines = [labels.get(str(feature.get("type", "")), str(feature.get("name") or feature.get("type"))) for feature in design.get("features", [])]
    if "SLDPRT" in design.get("outputs", []):
        lines.append("保存 SLDPRT")
    return lines


def _needs_confirmation(text: str, requested: list[str]) -> bool:
    if requested != ["model_3d"]:
        return False
    if _has_any(text.lower(), ("当前模型", "修改当前", "active model")):
        return False
    if not _has_model_intent(text, text.lower()):
        return False
    return not bool(re.search(r"\d+(?:\.\d+)?\s*(?:x|X|×|\*|\?|mm|毫米|长|宽|厚|l|w|h)", text, re.IGNORECASE))


def _has_model_intent(text: str, lower: str) -> bool:
    return _has_any(lower, ("创建", "设计", "建模", "模型", "零件", "底板", "安装板", "板", "solidworks", "model", "plate", "hole")) or bool(
        re.search(r"\d+\s*(?:x|X|×|\*|\?)\s*\d+", text)
    )


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _has_export_intent(text: str) -> bool:
    """Recognize file export without confusing output-shaft terminology."""
    if _has_any(text, ("导出", "另存为", "export", "step", "stp", "pdf", "dwg", "dxf")):
        return True
    return bool(re.search(r"输出\s*(?:文件|格式|结果|为|成|到)", text, flags=re.IGNORECASE))


def _dedupe_preserve_order(items: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for item in items:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
