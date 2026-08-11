from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .model_integrity import SolidWorksModelInspector
from .models import SkillResult
from .profile_geometry import normalize_profile_extrude_request
from .revolve_skill import RevolveSkill


class ProfileExtrudeSkill:
    """Create deterministic line/arc/circle closed-profile extrusions."""

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"
        self.inspector = SolidWorksModelInspector()

    @staticmethod
    def normalize_request(params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        return normalize_profile_extrude_request(params, task_type=task_type)

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == "profile_extrude"]
        if not features:
            return SkillResult(False, "No profile_extrude feature found in the production plan.")

        run_dir = self.output_root / f"profile_extrude_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "profile_extrude_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document
            from sw_part import extrude_boss, extrude_cut, extrude_midplane, sketch, sketch_arc, sketch_circle, sketch_line

            sw, active = connect_solidworks(visible=True)
            model = active
            operations: list[dict[str, Any]] = []
            files: list[str] = []

            for index, feature in enumerate(features):
                request = self.normalize_request(feature.get("params", {}), plan.get("task_type"))
                if not request.get("success"):
                    operations.append({**request, "name": feature.get("name") or f"ProfileExtrude{index + 1}"})
                    return self._result(
                        False,
                        str(request.get("message") or "Invalid profile extrusion request."),
                        report_path,
                        {"feature_type": "profile_extrude", "operations": operations},
                    )

                if request["mode"] == "new_model":
                    if index > 0:
                        return self._result(
                            False,
                            "Only the first profile extrusion may create the task Part.",
                            report_path,
                            {"feature_type": "profile_extrude", "operations": operations},
                        )
                    model = new_document(sw, "part")
                validation = RevolveSkill._validate_active_part(
                    model,
                    require_body=request["mode"] == "active_model",
                )
                if not validation.get("success"):
                    return self._result(False, str(validation.get("message")), report_path, validation)

                before = self._snapshot(model) if request["mode"] == "active_model" else {}
                operation = self._execute(
                    model,
                    feature,
                    request,
                    sketch,
                    sketch_line,
                    sketch_arc,
                    sketch_circle,
                    extrude_boss,
                    extrude_cut,
                    extrude_midplane,
                )
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or "SolidWorks profile extrusion failed."),
                        report_path,
                        {
                            "active_doc": validation.get("active_doc"),
                            "feature_type": "profile_extrude",
                            "operations": operations,
                        },
                    )

                model.ForceRebuild3(False)
                after = self._snapshot(model)
                geometry_validation = self._verify_geometry(request, before, after)
                operation["geometry_validation"] = geometry_validation
                operation["bbox_after_m"] = after.get("bbox", {})
                operation["body_count_after"] = after.get("body_count", 0)
                operation["volume_after_m3"] = after.get("volume_m3")
                operation["rebuild_error_count"] = after.get("rebuild_error_count", 0)
                operation["feature_error_count"] = after.get("feature_error_count", 0)
                if not geometry_validation.get("success"):
                    return self._result(
                        False,
                        str(geometry_validation.get("message")),
                        report_path,
                        {
                            "active_doc": validation.get("active_doc"),
                            "feature_type": "profile_extrude",
                            "operations": operations,
                        },
                    )

            if model is None:
                return self._result(False, "SolidWorks did not return a Part document.", report_path, {})

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = RevolveSkill._set_material_metadata(model, material)
            reopen_validation: dict[str, Any] = {
                "success": True,
                "skipped": True,
                "reason": "active-model operation",
            }

            if operations and operations[0].get("mode") == "new_model":
                requested_path = str(plan.get("execution_model_path") or "").strip()
                part_path = Path(requested_path) if requested_path else run_dir / "profile_extruded_part.SLDPRT"
                part_path.parent.mkdir(parents=True, exist_ok=True)
                if not save_document(model, str(part_path)):
                    return self._result(
                        False,
                        f"SolidWorks failed to save profile-extruded Part: {part_path}",
                        report_path,
                        {"operations": operations},
                    )
                if not part_path.is_file() or part_path.stat().st_size <= 0:
                    return self._result(
                        False,
                        f"Saved profile-extruded Part is missing or empty: {part_path}",
                        report_path,
                        {"operations": operations},
                    )
                files.append(str(part_path))
                reopen_validation = RevolveSkill._verify_reopen(sw, model, part_path)
                if not reopen_validation.get("success"):
                    return self._result(
                        False,
                        f"Saved profile-extruded Part could not be reopened: {part_path}",
                        report_path,
                        {
                            "operations": operations,
                            "reopen_validation": reopen_validation,
                            "files": files,
                        },
                    )
                model = reopen_validation.pop("model")
                reopened_state = self._snapshot(model)
                reopen_validation.update({
                    "volume_m3": reopened_state.get("volume_m3"),
                    "rebuild_error_count": reopened_state.get("rebuild_error_count"),
                    "feature_error_count": reopened_state.get("feature_error_count"),
                })
                if reopened_state.get("rebuild_error_count") or reopened_state.get("feature_error_count"):
                    return self._result(
                        False,
                        "Reopened profile-extruded Part contains rebuild or feature errors.",
                        report_path,
                        {
                            "operations": operations,
                            "reopen_validation": reopen_validation,
                            "files": files,
                        },
                    )

            title = str(get_com_member(model, "GetTitle") or "")
            final_state = self._snapshot(model)
            data = {
                "active_doc": title,
                "mode": operations[0].get("mode") if operations else "",
                "feature_type": "profile_extrude",
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen_validation,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "body_count": final_state.get("body_count", 0),
                "solid_body_count": final_state.get("solid_body_count", 0),
                "volume_m3": final_state.get("volume_m3"),
                "rebuild_error_count": final_state.get("rebuild_error_count", 0),
                "feature_error_count": final_state.get("feature_error_count", 0),
                "saved_by_this_skill": bool(files),
                "side_effects": {
                    "modifies_active_doc": any(item.get("mode") == "active_model" for item in operations),
                    "creates_new_doc": any(item.get("mode") == "new_model" for item in operations),
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": files,
            }
            return self._result(True, "SolidWorks closed-profile extrusion created and verified.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"profile_extrude failed: {exc}",
                report_path,
                {
                    "feature_type": "profile_extrude",
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def _execute(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        sketch_factory: Any,
        sketch_line: Any,
        sketch_arc: Any,
        sketch_circle: Any,
        extrude_boss: Any,
        extrude_cut: Any,
        extrude_midplane: Any,
    ) -> dict[str, Any]:
        sketch_name = ""
        with sketch_factory(model, request["sketch_plane"]) as sketch_name:
            sketch_manager = model.SketchManager
            previous_add_to_db = bool(getattr(sketch_manager, "AddToDB", False))
            previous_display = bool(getattr(sketch_manager, "DisplayWhenAdded", True))
            try:
                # Exact CAD-IR coordinates must not be changed by sketch
                # inferencing, grid snapping, or automatic relations.
                sketch_manager.AddToDB = True
                sketch_manager.DisplayWhenAdded = False
                for loop in request["profiles"]:
                    for segment in loop["segments"]:
                        created = self._create_segment(
                            model,
                            segment,
                            sketch_line,
                            sketch_arc,
                            sketch_circle,
                        )
                        if created is None:
                            return {
                                "success": False,
                                "message": f"SolidWorks failed to create a {segment['type']} profile segment.",
                            }
            finally:
                sketch_manager.AddToDB = previous_add_to_db
                sketch_manager.DisplayWhenAdded = previous_display

        operation = request["body_operation"]
        end_condition = request["end_condition"]
        direction = not bool(request["reverse_direction"])
        if operation in {"base", "boss"}:
            if end_condition == "mid_plane":
                created_feature = extrude_midplane(model, sketch_name, self._m(request["depth_mm"]))
            elif operation == "boss" and (
                request["start_offset_mm"] > 0.0 or end_condition == "through_next"
            ):
                created_feature = self._extrude_boss_with_start_offset(
                    model,
                    sketch_name,
                    depth_m=self._m(request["depth_mm"]),
                    direction=direction,
                    merge=bool(request["merge_result"]),
                    start_offset_m=self._m(request["start_offset_mm"]),
                    flip_start_offset=bool(request["flip_start_offset"]),
                    end_condition=(2 if end_condition == "through_next" else 0),
                )
            else:
                created_feature = extrude_boss(
                    model,
                    sketch_name,
                    self._m(request["depth_mm"]),
                    direction=direction,
                    merge=bool(request["merge_result"]),
                )
        else:
            if request["start_offset_mm"] > 0.0:
                created_feature = self._extrude_cut_with_start_offset(
                    model,
                    sketch_name,
                    depth_m=self._m(request["depth_mm"]),
                    reverse_direction=bool(request["reverse_direction"]),
                    flip=bool(request["flip_side_to_cut"]),
                    start_offset_m=self._m(request["start_offset_mm"]),
                    flip_start_offset=bool(request["flip_start_offset"]),
                )
            elif end_condition == "mid_plane":
                created_feature = self._extrude_cut_midplane(
                    model,
                    sketch_name,
                    total_depth_m=self._m(request["depth_mm"]),
                    flip=bool(request["flip_side_to_cut"]),
                )
            elif end_condition == "through_all_both":
                created_feature = self._extrude_cut_through_all_both(
                    model,
                    sketch_name,
                    direction=direction,
                    flip=bool(request["flip_side_to_cut"]),
                )
            elif request["reverse_direction"]:
                created_feature = self._extrude_cut_reversed(
                    model,
                    sketch_name,
                    depth_m=(
                        0.0
                        if end_condition == "through_all"
                        else self._m(request["depth_mm"])
                    ),
                    flip=bool(request["flip_side_to_cut"]),
                )
            else:
                created_feature = extrude_cut(
                    model,
                    sketch_name,
                    0.0 if end_condition == "through_all" else self._m(request["depth_mm"]),
                    direction=direction,
                    flip=bool(request["flip_side_to_cut"]),
                )
        if created_feature is None:
            return {"success": False, "message": "SolidWorks extrusion API returned no feature."}

        name = str(feature.get("name") or "ProfileExtrude")
        ActiveModelFeatureSkill._name_feature(created_feature, name)
        return {
            "success": True,
            "message": "Closed profile extrusion created.",
            "name": name,
            "type": "profile_extrude",
            "feature_name": ActiveModelFeatureSkill._feature_name(created_feature),
            "mode": request["mode"],
            "body_operation": operation,
            "sketch_plane": request["sketch_plane"],
            "end_condition": end_condition,
            "depth_mm": request["depth_mm"],
            "reverse_direction": request["reverse_direction"],
            "merge_result": request["merge_result"],
            "flip_side_to_cut": request["flip_side_to_cut"],
            "start_offset_mm": request["start_offset_mm"],
            "flip_start_offset": request["flip_start_offset"],
            "profiles": request["profiles"],
            "profile_metrics": request["profile_metrics"],
            "request": request,
        }

    @staticmethod
    def _extrude_cut_through_all_both(
        model: Any,
        sketch_name: Any,
        *,
        direction: bool,
        flip: bool,
    ) -> Any:
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
        return model.FeatureManager.FeatureCut4(
            False,  # Sd=False creates a double-ended cut.
            flip,
            not direction,
            1,  # swEndCondThroughAll, direction 1
            1,  # swEndCondThroughAll, direction 2
            0.01,
            0.01,
            False,
            False,
            False,
            False,
            0.0,
            0.0,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            0,
            0.0,
            False,
            False,
        )

    @staticmethod
    def _extrude_cut_midplane(
        model: Any,
        sketch_name: Any,
        *,
        total_depth_m: float,
        flip: bool,
    ) -> Any:
        """Create a symmetric cut whose requested depth is the total span."""
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
        return model.FeatureManager.FeatureCut4(
            True,
            flip,
            False,
            6,  # swEndCondMidPlane
            0,
            float(total_depth_m),
            0.0,
            False,
            False,
            False,
            False,
            0.0,
            0.0,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            0,
            0.0,
            False,
            False,
        )

    @staticmethod
    def _extrude_cut_reversed(
        model: Any,
        sketch_name: Any,
        *,
        depth_m: float,
        flip: bool,
    ) -> Any:
        """Create a single-ended cut with SolidWorks' actual Dir flag set."""
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
        end_condition = 1 if depth_m == 0.0 else 0
        effective_depth = 0.01 if depth_m == 0.0 else float(depth_m)
        return model.FeatureManager.FeatureCut4(
            True,  # Sd=True: one cut direction.
            flip,
            True,  # Dir=True: reverse the first direction.
            end_condition,
            0,
            effective_depth,
            0.0,
            False,
            False,
            False,
            False,
            0.0,
            0.0,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            0,
            0.0,
            False,
            False,
        )

    @staticmethod
    def _extrude_boss_with_start_offset(
        model: Any,
        sketch_name: Any,
        *,
        depth_m: float,
        direction: bool,
        merge: bool,
        start_offset_m: float,
        flip_start_offset: bool,
        end_condition: int = 0,
    ) -> Any:
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
        effective_depth_m = 0.01 if end_condition == 2 else depth_m
        return model.FeatureManager.FeatureExtrusion3(
            True, False, direction,
            end_condition, 0,
            effective_depth_m, 0.0,
            False, False, False, False,
            0.0, 0.0,
            False, False, False, False,
            merge, False, True,
            3, start_offset_m, flip_start_offset,
        )

    @staticmethod
    def _extrude_cut_with_start_offset(
        model: Any,
        sketch_name: Any,
        *,
        depth_m: float,
        reverse_direction: bool,
        flip: bool,
        start_offset_m: float,
        flip_start_offset: bool,
    ) -> Any:
        """Create a blind cut from an explicit offset of the sketch plane."""
        from sw_part import _ensure_sketch_selected

        _ensure_sketch_selected(model, sketch_name)
        return model.FeatureManager.FeatureCut4(
            True,
            flip,
            reverse_direction,
            0,
            0,
            depth_m,
            0.0,
            False,
            False,
            False,
            False,
            0.0,
            0.0,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            3,
            start_offset_m,
            flip_start_offset,
            False,
        )

    def _create_segment(
        self,
        model: Any,
        segment: dict[str, Any],
        sketch_line: Any,
        sketch_arc: Any,
        sketch_circle: Any,
    ) -> Any:
        if segment["type"] == "line":
            start = segment["start_mm"]
            end = segment["end_mm"]
            return sketch_line(model, self._m(start[0]), self._m(start[1]), self._m(end[0]), self._m(end[1]))
        if segment["type"] == "arc":
            center = segment["center_mm"]
            start = segment["start_mm"]
            end = segment["end_mm"]
            return sketch_arc(
                model,
                self._m(center[0]),
                self._m(center[1]),
                self._m(start[0]),
                self._m(start[1]),
                self._m(end[0]),
                self._m(end[1]),
                int(segment["direction"]),
            )
        center = segment["center_mm"]
        return sketch_circle(
            model,
            self._m(center[0]),
            self._m(center[1]),
            self._m(segment["radius_mm"]),
        )

    def _snapshot(self, model: Any) -> dict[str, Any]:
        body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        exact_bbox = self._exact_body_bbox(model)
        if exact_bbox:
            body["bbox"] = exact_bbox
        state = self.inspector.inspect(model, rebuild=True)
        body.update({
            "body_count": state.body_count,
            "solid_body_count": state.solid_body_count,
            "surface_body_count": state.surface_body_count,
            "feature_count": state.feature_count,
            "volume_m3": state.volume_m3,
            "rebuild_error_count": state.rebuild_error_count,
            "feature_error_count": state.feature_error_count,
        })
        return body

    def _exact_body_bbox(self, model: Any) -> dict[str, float] | None:
        return self.inspector.exact_body_bbox(model)

    @staticmethod
    def _verify_geometry(
        request: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        if not after.get("success") or int(after.get("solid_body_count", 0)) <= 0:
            return {"success": False, "message": "Profile extrusion produced no solid body."}
        if after.get("rebuild_error_count") or after.get("feature_error_count"):
            return {
                "success": False,
                "message": "Profile extrusion produced rebuild or feature errors.",
                "rebuild_error_count": after.get("rebuild_error_count"),
                "feature_error_count": after.get("feature_error_count"),
            }

        actual_volume = after.get("volume_m3")
        if actual_volume is None or float(actual_volume) <= 0.0:
            return {"success": False, "message": "Profile extrusion volume could not be verified."}

        operation = request["body_operation"]
        if operation == "base":
            expected_volume = (
                float(request["profile_metrics"]["net_area_mm2"])
                * float(request["depth_mm"])
                * 1e-9
            )
            volume_error = abs(float(actual_volume) - expected_volume) / max(expected_volume, 1e-15)
            bbox = after.get("bbox", {})
            actual_spans = sorted([
                abs(float(bbox.get("xmax", 0.0)) - float(bbox.get("xmin", 0.0))) * 1000.0,
                abs(float(bbox.get("ymax", 0.0)) - float(bbox.get("ymin", 0.0))) * 1000.0,
                abs(float(bbox.get("zmax", 0.0)) - float(bbox.get("zmin", 0.0))) * 1000.0,
            ])
            profile_bbox = request["profile_metrics"]["bbox_mm"]
            expected_spans = sorted([
                float(profile_bbox["xmax"]) - float(profile_bbox["xmin"]),
                float(profile_bbox["ymax"]) - float(profile_bbox["ymin"]),
                float(request["depth_mm"]),
            ])
            span_match = all(
                abs(actual - expected) <= max(0.15, abs(expected) * 0.01)
                for actual, expected in zip(actual_spans, expected_spans)
            )
            success = volume_error <= 0.02 and span_match
            return {
                "success": success,
                "message": "Base profile extrusion geometry verified." if success else "Base profile extrusion does not match the requested area/depth envelope.",
                "expected_volume_m3": expected_volume,
                "actual_volume_m3": actual_volume,
                "relative_volume_error": volume_error,
                "expected_spans_mm": expected_spans,
                "actual_spans_mm": actual_spans,
            }

        before_volume = before.get("volume_m3")
        if before_volume is None:
            return {"success": False, "message": "Active-model volume before profile extrusion is unavailable."}
        before_body_count = int(before.get("solid_body_count", before.get("body_count", 0)) or 0)
        after_body_count = int(after.get("solid_body_count", after.get("body_count", 0)) or 0)
        if (
            operation == "boss"
            and bool(request.get("merge_result", True))
            and before_body_count > 0
            and after_body_count > before_body_count
        ):
            return {
                "success": False,
                "message": "Merged profile boss produced additional solid bodies.",
                "body_count_before": before_body_count,
                "body_count_after": after_body_count,
            }
        tolerance = max(abs(float(before_volume)), abs(float(actual_volume)), 1e-12) * 1e-9
        delta = float(actual_volume) - float(before_volume)
        expected_direction = delta > tolerance if operation == "boss" else delta < -tolerance
        return {
            "success": expected_direction,
            "message": f"Active-model {operation} volume change verified." if expected_direction else f"Active-model {operation} did not change volume in the expected direction.",
            "volume_before_m3": before_volume,
            "volume_after_m3": actual_volume,
            "volume_delta_m3": delta,
        }

    def _ensure_imports(self) -> None:
        if not self.script_dir.exists():
            raise RuntimeError(f"SolidWorks automation scripts not found: {self.script_dir}")
        script_text = str(self.script_dir)
        if script_text not in sys.path:
            sys.path.insert(0, script_text)
        os.environ["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["CAD_AGENT_NO_INTERACTIVE"] = "1"

    @staticmethod
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        existing_files = [str(item) for item in payload.get("files", [])]
        payload["files"] = list(dict.fromkeys(existing_files + [str(report_path)]))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success=success,
            message=message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
