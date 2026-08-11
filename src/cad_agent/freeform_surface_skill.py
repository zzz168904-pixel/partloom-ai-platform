from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .models import SkillResult
from .revolve_skill import RevolveSkill


class FreeformSurfaceSkill:
    """Create one native Fill Surface from an explicit closed 3D spline boundary."""

    FEATURE_TYPE = "freeform_surface"

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == self.FEATURE_TYPE]
        if len(features) != 1:
            return SkillResult(False, "A free-form surface task requires exactly one freeform_surface feature.")

        run_dir = self.output_root / f"freeform_surface_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "freeform_surface_report.json"
        try:
            request = self.normalize_request(features[0].get("params", {}), plan.get("task_type"))
            if not request.get("success"):
                return self._result(False, str(request.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "invalid_request": request,
                })

            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document

            sw, _active = connect_solidworks(visible=True)
            model = new_document(sw, "part")
            validation = RevolveSkill._validate_active_part(model, require_body=False)
            if not validation.get("success"):
                return self._result(False, str(validation.get("message")), report_path, validation)

            module = self._solidworks_module(sw)
            operation = self._create_surface(model, module, features[0], request)
            if not operation.get("success"):
                return self._result(False, str(operation.get("message")), report_path, {
                    "active_doc": validation.get("active_doc"),
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            model.ForceRebuild3(False)
            surface_body = self._surface_body_info(model)
            geometry = self._verify_geometry(model, request, surface_body, operation)
            operation["geometry_validation"] = geometry
            operation["bbox_after_m"] = surface_body.get("bbox", {})
            operation["body_count_after"] = surface_body.get("body_count", 0)
            if not geometry.get("success"):
                return self._result(False, str(geometry.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = RevolveSkill._set_material_metadata(model, material)
            requested_path = str(plan.get("execution_model_path") or "").strip()
            part_path = Path(requested_path) if requested_path else run_dir / "freeform_surface_part.SLDPRT"
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if not save_document(model, str(part_path)) or not part_path.is_file() or part_path.stat().st_size <= 0:
                return self._result(False, f"SolidWorks failed to save free-form surface Part: {part_path}", report_path, {
                    "operations": [operation],
                })
            reopen = self._verify_reopen(sw, model, part_path)
            if not reopen.get("success"):
                return self._result(False, f"Saved free-form surface Part could not be reopened: {part_path}", report_path, {
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "files": [str(part_path)],
                })
            model = reopen.pop("model")
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": "new_model",
                "feature_type": self.FEATURE_TYPE,
                "feature_created": True,
                "features_created": 1,
                "operations": [operation],
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": True,
                "side_effects": {
                    "modifies_active_doc": False,
                    "creates_new_doc": True,
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": [str(part_path)],
            }
            return self._result(True, "Native SolidWorks free-form fill surface created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"freeform_surface failed: {exc}", report_path, {
                "feature_type": self.FEATURE_TYPE,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        mode = str(params.get("mode") or "new_model").strip().lower()
        if mode != "new_model":
            return {"success": False, "message": "freeform_surface currently supports new_model mode only."}
        if task_type == "modify_3d":
            return {"success": False, "message": "modify_3d cannot create a new free-form surface Part."}
        surface_type = str(params.get("surface_type") or "fill").strip().lower()
        if surface_type not in {"fill", "fill_surface"}:
            return {"success": False, "message": "freeform_surface currently supports native fill surfaces only."}
        raw_curves = params.get("boundary_curves_mm") or params.get("boundary_curves")
        if not isinstance(raw_curves, list) or not 3 <= len(raw_curves) <= 12:
            return {"success": False, "message": "freeform_surface requires 3 to 12 ordered boundary_curves_mm."}

        curves: list[list[list[float]]] = []
        for curve_index, raw_curve in enumerate(raw_curves):
            points_raw = raw_curve.get("points_mm") if isinstance(raw_curve, dict) else raw_curve
            if not isinstance(points_raw, list) or len(points_raw) < 3:
                return {"success": False, "message": f"boundary curve {curve_index + 1} requires at least three 3D points."}
            points: list[list[float]] = []
            for point_index, raw_point in enumerate(points_raw):
                point = cls._point3(raw_point)
                if point is None or not all(math.isfinite(value) for value in point):
                    return {"success": False, "message": f"boundary curve {curve_index + 1} point {point_index + 1} is invalid."}
                if points and math.dist(points[-1], point) <= 1e-7:
                    return {"success": False, "message": f"boundary curve {curve_index + 1} contains a repeated adjacent point."}
                points.append(point)
            curves.append(points)

        tolerance = float(params.get("closure_tolerance_mm", 0.001) or 0.001)
        if tolerance <= 0 or tolerance > 0.1:
            return {"success": False, "message": "closure_tolerance_mm must be greater than 0 and at most 0.1."}
        for index, curve in enumerate(curves):
            next_curve = curves[(index + 1) % len(curves)]
            if math.dist(curve[-1], next_curve[0]) > tolerance:
                return {"success": False, "message": f"boundary curves {index + 1} and {(index + 1) % len(curves) + 1} are not contiguous."}

        all_points = [point for curve in curves for point in curve]
        spans = [max(point[axis] for point in all_points) - min(point[axis] for point in all_points) for axis in range(3)]
        if sorted(spans)[-2] <= 1e-6:
            return {"success": False, "message": "freeform_surface boundary must span at least two model axes."}
        resolution = int(params.get("resolution", 3) or 3)
        if resolution not in {1, 2, 3}:
            return {"success": False, "message": "freeform_surface resolution must be 1, 2, or 3."}
        return {
            "success": True,
            "mode": "new_model",
            "surface_type": "fill",
            "boundary_curves_mm": curves,
            "closure_tolerance_mm": tolerance,
            "resolution": resolution,
            "boundary_condition": "contact",
            "optimize_surface": True,
        }

    def _create_surface(
        self,
        model: Any,
        module: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        import pythoncom
        from win32com.client import VARIANT

        sketch_manager = module.ISketchManager(model.SketchManager._oleobj_)
        feature_manager = module.IFeatureManager(model.FeatureManager._oleobj_)
        sketch_manager.Insert3DSketch(True)
        segments: list[Any] = []
        for curve in request["boundary_curves_mm"]:
            flat = [self._m(value) for point in curve for value in point]
            segment = sketch_manager.CreateSpline(
                VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
            )
            if segment is None:
                sketch_manager.Insert3DSketch(True)
                return {"success": False, "message": "SolidWorks failed to create a 3D boundary spline."}
            segments.append(segment)
        sketch_manager.Insert3DSketch(True)

        sketch_feature = self._last_feature_of_type(model, "3DProfileFeature")
        if sketch_feature is None:
            return {"success": False, "message": "Could not resolve the closed 3D boundary sketch feature."}
        model.ClearSelection2(True)
        if not sketch_feature.Select2(False, 257):
            return {"success": False, "message": "Could not select the free-form boundary sketch."}
        selection_manager = module.ISelectionMgr(model.SelectionManager._oleobj_)
        boundary_object = selection_manager.GetSelectedObject6(1, 257)
        if boundary_object is None:
            return {"success": False, "message": "SolidWorks did not return the selected free-form boundary object."}

        created = feature_manager.InsertFillSurface2(
            request["resolution"],
            1,
            boundary_object,
            0,
            None,
            None,
        )
        if created is None:
            return {"success": False, "message": "SolidWorks InsertFillSurface2 returned no feature."}
        name = str(feature.get("name") or "FreeformFillSurface")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        self._hide_sketch(model, sketch_feature)
        ActiveModelThroughHoleExecutor._com_member(model, "GraphicsRedraw2")
        return {
            "success": True,
            "name": name,
            "type": self.FEATURE_TYPE,
            "mode": "new_model",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "boundary_sketch": str(sketch_feature.Name),
            "boundary_curve_count": len(segments),
        }

    @staticmethod
    def _surface_body_info(model: Any) -> dict[str, Any]:
        try:
            bodies = ActiveModelThroughHoleExecutor._com_member(model, "GetBodies2", 1, True)
            if bodies is None:
                return {"success": False, "message": "Part contains no surface bodies."}
            if not isinstance(bodies, (tuple, list)):
                bodies = (bodies,)
            boxes: list[tuple[float, ...]] = []
            valid: list[Any] = []
            for body in bodies:
                box = ActiveModelThroughHoleExecutor._com_member(body, "GetBodyBox")
                if isinstance(box, (tuple, list)) and len(box) >= 6:
                    boxes.append(tuple(float(value) for value in box[:6]))
                    valid.append(body)
            if not boxes:
                return {"success": False, "message": "Could not read the free-form surface bounding box."}
            bbox = {
                "xmin": min(box[0] for box in boxes),
                "ymin": min(box[1] for box in boxes),
                "zmin": min(box[2] for box in boxes),
                "xmax": max(box[3] for box in boxes),
                "ymax": max(box[4] for box in boxes),
                "zmax": max(box[5] for box in boxes),
            }
            return {"success": True, "bodies": valid, "body_count": len(valid), "bbox": bbox}
        except Exception as exc:
            return {"success": False, "message": f"Failed to inspect surface bodies: {exc}"}

    @staticmethod
    def _verify_geometry(
        model: Any,
        request: dict[str, Any],
        body: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        bbox = body.get("bbox", {})
        spans = [
            abs(float(bbox.get(max_key, 0.0)) - float(bbox.get(min_key, 0.0))) * 1000.0
            for min_key, max_key in (("xmin", "xmax"), ("ymin", "ymax"), ("zmin", "zmax"))
        ]
        tree = ActiveModelFeatureSkill._feature_tree(model)
        text = " ".join(f"{item.get('name', '')} {item.get('type', '')}".lower() for item in tree)
        has_fill = any(
            token in text
            for token in ("fillsurface", "fillrefsurface", "surfacefill", "曲面填充", "填充曲面")
        )
        requested_points = [point for curve in request["boundary_curves_mm"] for point in curve]
        requested_min = [min(point[axis] for point in requested_points) for axis in range(3)]
        requested_max = [max(point[axis] for point in requested_points) for axis in range(3)]
        requested_spans = [requested_max[axis] - requested_min[axis] for axis in range(3)]
        allowance = max(requested_spans) * 0.5
        actual_min = [float(bbox.get(key, 0.0)) * 1000.0 for key in ("xmin", "ymin", "zmin")]
        actual_max = [float(bbox.get(key, 0.0)) * 1000.0 for key in ("xmax", "ymax", "zmax")]
        envelope_controlled = all(
            actual_min[axis] >= requested_min[axis] - allowance
            and actual_max[axis] <= requested_max[axis] + allowance
            for axis in range(3)
        )
        success = bool(
            body.get("success")
            and int(body.get("body_count", 0)) == 1
            and sorted(spans)[-2] > 0.01
            and operation.get("boundary_curve_count") == len(request["boundary_curves_mm"])
            and has_fill
            and envelope_controlled
        )
        return {
            "success": success,
            "message": "Free-form fill surface and surface body verified." if success else "Free-form surface geometry verification failed.",
            "surface_body_count": int(body.get("body_count", 0)),
            "actual_spans_mm": spans,
            "fill_surface_tree": has_fill,
            "requested_min_mm": requested_min,
            "requested_max_mm": requested_max,
            "envelope_allowance_mm": allowance,
            "envelope_controlled": envelope_controlled,
        }

    @classmethod
    def _verify_reopen(cls, sw: Any, model: Any, path: Path) -> dict[str, Any]:
        try:
            current_path = str(ActiveModelThroughHoleExecutor._com_member(model, "GetPathName", default="") or "")
            if not current_path or Path(current_path).resolve() != path.resolve():
                return {"success": False, "message": "Active task document path changed before surface reopen validation."}
            title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
            if title:
                sw.CloseDoc(title)
            opened = None
            errors: list[str] = []
            for method_name, args in (("OpenDoc", (str(path), 1)), ("LoadFile2", (str(path), ""))):
                try:
                    result = getattr(sw, method_name)(*args)
                    opened = result[0] if isinstance(result, tuple) else result
                    if opened is None:
                        opened = getattr(sw, "ActiveDoc", None)
                    if opened is not None:
                        break
                except Exception as exc:
                    errors.append(f"{method_name}: {exc!r}")
            info = cls._surface_body_info(opened) if opened is not None else {"success": False}
            reopened_path = str(ActiveModelThroughHoleExecutor._com_member(opened, "GetPathName", default="") or "")
            success = bool(
                opened is not None
                and reopened_path
                and Path(reopened_path).resolve() == path.resolve()
                and info.get("success")
                and int(info.get("body_count", 0)) == 1
            )
            return {
                "success": success,
                "path": str(path),
                "active_path": reopened_path,
                "surface_body_count": info.get("body_count", 0),
                "bbox_m": info.get("bbox", {}),
                "attempt_errors": errors,
                "model": opened,
            }
        except Exception as exc:
            return {"success": False, "path": str(path), "error": repr(exc)}

    @staticmethod
    def _last_feature_of_type(model: Any, type_name: str) -> Any | None:
        result = None
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None:
            current_type = str(ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or "")
            if current_type == type_name:
                result = feature
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _hide_sketch(model: Any, feature: Any | None) -> None:
        if feature is None:
            return
        try:
            model.ClearSelection2(True)
            if feature.Select2(False, 0):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankSketch")
        except Exception:
            # Sketch visibility is cosmetic and must not invalidate valid surface geometry.
            pass
        finally:
            model.ClearSelection2(True)

    @staticmethod
    def _point3(value: Any) -> list[float] | None:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                return [float(value[0]), float(value[1]), float(value[2])]
            if isinstance(value, dict):
                return [float(value[key]) for key in ("x", "y", "z")]
        except (KeyError, TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _solidworks_module(sw: Any) -> Any:
        import win32com.client

        revision = ActiveModelThroughHoleExecutor._com_member(sw, "RevisionNumber", default="33.0")
        major = int(str(revision).split(".", 1)[0])
        return win32com.client.gencache.EnsureModule(
            "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, major, 0
        )

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
        payload["files"] = list(dict.fromkeys(
            [str(item) for item in payload.get("files", [])] + [str(report_path)]
        ))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(success, message, data=payload, path=str(report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))
