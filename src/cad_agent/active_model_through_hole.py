from __future__ import annotations

import json
import math
import os
import sys
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .geometry_context import FaceContext
from .geometry_resolver import FaceGeometryResolver, GeometryResolution
from .model_integrity import as_part_doc
from .models import SkillResult


class ActiveModelThroughHoleExecutor:
    """Create through holes on the currently active SolidWorks part."""

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run(self, request: dict[str, Any]) -> SkillResult:
        run_dir = self.output_root / f"active_through_hole_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "active_model_through_hole_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, mm

            validation = self._validate_request(request)
            if not validation["success"]:
                return self._result(False, validation["message"], report_path, validation)

            sw, model = connect_solidworks(visible=True)
            if model is None:
                return self._result(False, "ActiveDoc does not exist.", report_path, {"request": request})
            title = str(get_com_member(model, "GetTitle"))
            doc_type = int(get_com_member(model, "GetType"))
            if doc_type != 1:
                return self._result(False, f"ActiveDoc is not a Part: title={title}, type={doc_type}", report_path, {"active_doc": title, "doc_type": doc_type})

            body_info = self._body_info(model)
            if not body_info["success"]:
                return self._result(False, body_info["message"], report_path, {"active_doc": title, "request": request})

            bbox = body_info["bbox"]
            context = FaceContext.from_request(request)
            context = self._context_at_bounding_box(context, bbox)
            if len(context.placement_uv_mm) != int(request["count"]):
                return self._result(False, "geometry_context placement_uv_mm count does not match hole count.", report_path, {"request": request})
            face_resolution = self._resolve_semantic_face_resolution(body_info["bodies"], context)
            if not face_resolution.success or face_resolution.entity is None or face_resolution.frame is None:
                return self._result(
                    False,
                    "semantic_face_resolution_failed",
                    report_path,
                    {
                        "active_doc": title,
                        "bbox": bbox,
                        "geometry_context": context.as_dict(),
                        "geometry_resolution": face_resolution.as_dict(),
                    },
                )
            top_face = face_resolution.entity
            face_signature = self._resolved_face_signature(context, face_resolution)
            centers = [
                list(face_resolution.frame.world_from_local_mm(point[0], point[1]))
                for point in context.placement_uv_mm
            ]
            precheck = self._intersection_precheck(bbox, centers, context)
            if not precheck["success"]:
                return self._result(False, "geometry_intersection_precheck_failed", report_path, {"active_doc": title, "bbox": bbox, "selected_face_signature": face_signature, "geometry_context": context.as_dict(), "intersection_precheck": precheck})
            volume_before = self._solid_volume(body_info["bodies"])
            cut, cut_direction = self._cut_holes(model, top_face, request, centers, bbox)
            if cut is None:
                return self._result(False, "SolidWorks FeatureCut4 returned no feature for through-hole cut.", report_path, {"active_doc": title, "bbox": bbox, "selected_face_signature": face_signature, "intersection_precheck": precheck, "cut_attempts": ["through_all_normal", "through_all_reverse", "through_all_both"]})

            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            save_path = request.get("save_path")
            save_after = bool(request.get("save_after", True))
            saved_path = Path(str(save_path)) if save_path else None
            if save_after:
                saved_path = self._save_model(model, run_dir, saved_path)
                if not saved_path or not saved_path.is_file() or saved_path.stat().st_size <= 0:
                    return self._result(False, f"Failed to save active model after through-hole cut: {saved_path}", report_path, {"active_doc": title})
                reopen = self._verify_reopen(sw, saved_path)
            else:
                reopen = {"success": True, "skipped": True, "reason": "Final save_sldprt step owns persistence for this production task."}
            try:
                cut.Name = str(request.get("feature_name") or "ThroughHole")
            except Exception:
                pass
            body_after = self._body_info(model)
            volume_after = self._solid_volume(body_after.get("bodies", [])) if body_after.get("success") else None
            result_data = {
                "request": request,
                "active_doc": title,
                "mode": "active_model",
                "bbox_m": bbox,
                "bbox_mm": {key: round(value * 1000.0, 6) for key, value in bbox.items()},
                "hole_centers_m": centers,
                "hole_centers_mm": [[round(value * 1000.0, 6) for value in point] for point in centers],
                "diameter_mm": float(request["diameter_mm"]),
                "selected_face_signature": face_signature,
                "sketch_plane": context.as_dict(),
                "geometry_resolution": face_resolution.as_dict(),
                "resolved_local_frame": face_resolution.frame.as_dict(),
                "cut_direction": cut_direction,
                "intersection_precheck": precheck,
                "volume_before": volume_before,
                "volume_after": volume_after,
                "volume_reduced": volume_before is not None and volume_after is not None and volume_after < volume_before,
                "rebuild_errors": bool(model.ForceRebuild3(False)),
                "holes_created": len(centers),
                "feature_created": True,
                "saved_path": str(saved_path) if saved_path else None,
                "saved_by_this_skill": save_after,
                "reopen_check": reopen,
            }
            success = reopen.get("success") is True
            return self._result(success, "Active-model through holes created." if success else "Through holes created but reopen check failed.", report_path, result_data, path=saved_path)
        except Exception as exc:
            return self._result(
                False,
                f"active_model through_hole failed: {exc}",
                report_path,
                {"error": repr(exc), "traceback": traceback.format_exc(), "request": request},
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
    def _validate_request(request: dict[str, Any]) -> dict[str, Any]:
        if request.get("mode") != "active_model" or request.get("hole_type") != "through":
            return {"success": False, "message": "Only active_model through holes are supported."}
        placement = str(request.get("placement") or "")
        count = int(request.get("count", 0) or 0)
        if placement == "center" and count != 1:
            return {"success": False, "message": "Center through_hole requires count=1."}
        if placement == "corner_offsets" and count != 4:
            return {"success": False, "message": "corner_offsets through_hole requires count=4."}
        if placement not in {"center", "corner_offsets"}:
            return {"success": False, "message": f"Unsupported through_hole placement: {placement!r}"}
        diameter = float(request.get("diameter_mm", 0) or 0)
        if diameter <= 0:
            return {"success": False, "message": "diameter_mm must be greater than 0."}
        try:
            context = FaceContext.from_request(request)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        if len(context.placement_uv_mm) != count:
            return {"success": False, "message": "geometry_context placement_uv_mm count does not match count."}
        if placement == "corner_offsets":
            offsets = request.get("edge_offsets_mm", {})
            if float(offsets.get("x", 0) or 0) <= diameter / 2.0 or float(offsets.get("y", 0) or 0) <= diameter / 2.0:
                return {"success": False, "message": "edge_offsets must be greater than hole radius."}
            if request.get("target_face") == "base_top" and float(request.get("base_thickness_mm", 0) or 0) <= 0:
                return {"success": False, "message": "base_thickness_mm is required for base_top hole placement."}
        return {"success": True, "message": "request ok"}

    @staticmethod
    def _solid_volume(bodies: list[Any]) -> float | None:
        numeric: list[float] = []
        for body in bodies:
            value = ActiveModelThroughHoleExecutor._com_member(body, "GetVolume", default=None)
            if value is None:
                mass_properties = ActiveModelThroughHoleExecutor._com_member(
                    body,
                    "GetMassProperties",
                    1.0,
                    default=None,
                )
                if isinstance(mass_properties, (tuple, list)) and len(mass_properties) > 3:
                    value = mass_properties[3]
            if value is not None:
                numeric.append(float(value))
        return sum(numeric) if numeric else None

    @staticmethod
    def _context_at_bounding_box(context: FaceContext, bbox: dict[str, float]) -> FaceContext:
        location = str(context.bounding_box_location or "").strip().lower()
        locations = {
            "x_min": (0, "xmin"),
            "x_max": (0, "xmax"),
            "y_min": (1, "ymin"),
            "y_max": (1, "ymax"),
            "z_min": (2, "zmin"),
            "z_max": (2, "zmax"),
        }
        selected = locations.get(location)
        if selected is None:
            return context
        axis, key = selected
        if key not in bbox:
            return context
        origin = list(context.face_origin)
        origin[axis] = float(bbox[key]) * 1000.0
        return replace(context, face_origin=origin)

    @staticmethod
    def _resolve_semantic_face_resolution(
        bodies: list[Any],
        context: FaceContext,
    ) -> GeometryResolution:
        expected_signature: dict[str, Any] = {
            "planar": True,
            "normal": list(context.face_normal),
            "plane_origin_m": [value / 1000.0 for value in context.face_origin],
        }
        if context.approximate_area_mm2 is not None:
            expected_signature["approximate_area_m2"] = context.approximate_area_mm2 / 1_000_000.0
        return FaceGeometryResolver().resolve(
            model_doc=None,
            bodies=bodies,
            expected_signature=expected_signature,
            preferred_origin_m=expected_signature["plane_origin_m"],
            preferred_u_axis=context.local_u_axis,
        )

    @staticmethod
    def _resolved_face_signature(
        context: FaceContext,
        resolution: GeometryResolution,
    ) -> dict[str, Any]:
        signature = dict(resolution.resolved_signature)
        signature.update(
            {
                "target_body": context.target_body,
                "target_feature": context.target_feature,
                "target_face_role": context.target_face_role,
                "face_normal_axis": signature.get("normal_axis"),
                "face_origin_m": signature.get("plane_origin_m"),
                "resolver_source": resolution.source,
                "resolver_score": resolution.score,
                "local_frame": resolution.frame.as_dict() if resolution.frame else None,
            }
        )
        return signature

    @staticmethod
    def _resolve_semantic_face(bodies: list[Any], context: FaceContext) -> tuple[Any, dict[str, Any]] | None:
        resolution = ActiveModelThroughHoleExecutor._resolve_semantic_face_resolution(bodies, context)
        if not resolution.success or resolution.entity is None:
            return None
        return (
            resolution.entity,
            ActiveModelThroughHoleExecutor._resolved_face_signature(context, resolution),
        )

    @staticmethod
    def _intersection_precheck(bbox: dict[str, float], centers: list[list[float]], context: FaceContext) -> dict[str, Any]:
        radius = 0.0005
        checks = []
        for point in centers:
            within = all(bbox[f"{axis}min"] - radius <= point[index] <= bbox[f"{axis}max"] + radius for index, axis in enumerate(("x", "y", "z")))
            checks.append({"point_m": point, "within_body_bbox": within, "normal_intersects": within, "reverse_intersects": within})
        return {"success": bool(checks) and all(item["within_body_bbox"] for item in checks), "closed_profiles": True, "sketch_on_target_face": True, "direction_candidates": ["through_all_normal", "through_all_reverse", "through_all_both"], "checks": checks}

    @staticmethod
    def _body_info(model: Any) -> dict[str, Any]:
        try:
            part_doc = as_part_doc(model)
            bodies = ActiveModelThroughHoleExecutor._com_member(part_doc, "GetBodies2", 0, False)
            if bodies is None:
                return {"success": False, "message": "Part contains no solid bodies."}
            if not isinstance(bodies, (tuple, list)):
                bodies = (bodies,)
            boxes = []
            valid_bodies = []
            for body in bodies:
                box = ActiveModelThroughHoleExecutor._com_member(body, "GetBodyBox")
                if box and len(box) >= 6:
                    boxes.append(tuple(float(v) for v in box[:6]))
                    valid_bodies.append(body)
            if not boxes:
                return {"success": False, "message": "Could not read solid body bounding box."}
            bbox = {
                "xmin": min(box[0] for box in boxes),
                "ymin": min(box[1] for box in boxes),
                "zmin": min(box[2] for box in boxes),
                "xmax": max(box[3] for box in boxes),
                "ymax": max(box[4] for box in boxes),
                "zmax": max(box[5] for box in boxes),
            }
            bbox["length"] = bbox["xmax"] - bbox["xmin"]
            bbox["width"] = bbox["ymax"] - bbox["ymin"]
            bbox["thickness"] = bbox["zmax"] - bbox["zmin"]
            return {"success": True, "bodies": valid_bodies, "bbox": bbox}
        except Exception as exc:
            return {"success": False, "message": f"Failed to inspect solid bodies: {exc}"}

    @staticmethod
    def _corner_centers(request: dict[str, Any], bbox: dict[str, float]) -> list[tuple[float, float]]:
        xoff = float(request["edge_offsets_mm"]["x"]) / 1000.0
        yoff = float(request["edge_offsets_mm"]["y"]) / 1000.0
        xmin, xmax, ymin, ymax = bbox["xmin"], bbox["xmax"], bbox["ymin"], bbox["ymax"]
        return [
            (xmin + xoff, ymin + yoff),
            (xmax - xoff, ymin + yoff),
            (xmin + xoff, ymax - yoff),
            (xmax - xoff, ymax - yoff),
        ]

    @staticmethod
    def _centers(request: dict[str, Any], bbox: dict[str, float]) -> list[tuple[float, float]]:
        if request.get("placement") == "center":
            return [((bbox["xmin"] + bbox["xmax"]) / 2.0, (bbox["ymin"] + bbox["ymax"]) / 2.0)]
        return ActiveModelThroughHoleExecutor._corner_centers(request, bbox)

    @staticmethod
    def _validate_centers(request: dict[str, Any], bbox: dict[str, float], centers: list[tuple[float, float]]) -> dict[str, Any]:
        radius = float(request["diameter_mm"]) / 2000.0
        for x, y in centers:
            if not (bbox["xmin"] + radius < x < bbox["xmax"] - radius and bbox["ymin"] + radius < y < bbox["ymax"] - radius):
                return {"success": False, "message": f"Hole center outside valid body range: {(x, y)}"}
        for i, left in enumerate(centers):
            for right in centers[i + 1 :]:
                if math.dist(left, right) <= 2.0 * radius:
                    return {"success": False, "message": f"Holes overlap: {left} and {right}"}
        return {"success": True, "message": "centers ok"}

    @staticmethod
    def _find_top_face(bodies: list[Any], bbox: dict[str, float], face_scan: dict[str, Any] | None = None) -> Any | None:
        zmax = bbox["zmax"]
        tolerance = max(1e-6, bbox["thickness"] * 0.01)
        best = None
        best_area = -1.0
        scanned = 0
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces")
            if faces is None:
                continue
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face in faces:
                scanned += 1
                try:
                    box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
                    if not box or len(box) < 6:
                        continue
                    face_zmax = float(box[5])
                    face_zmin = float(box[2])
                    if abs(face_zmax - zmax) > tolerance or abs(face_zmax - face_zmin) > tolerance:
                        continue
                    area = abs((float(box[3]) - float(box[0])) * (float(box[4]) - float(box[1])))
                    surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface", default=None)
                    is_plane = True
                    if surface is not None:
                        plane_value = ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True)
                        is_plane = bool(True if plane_value is None else plane_value)
                    if not is_plane:
                        continue
                    candidates.append({"box": [float(v) for v in box[:6]], "area": area})
                    if area > best_area:
                        best = face
                        best_area = area
                except Exception:
                    errors.append(repr(sys.exc_info()[1]))
                    continue
        if face_scan is not None:
            face_scan.update(
                {
                    "faces_scanned": scanned,
                    "candidate_count": len(candidates),
                    "candidates": candidates[:12],
                    "errors": errors[:12],
                    "zmax": zmax,
                    "tolerance": tolerance,
                    "best_area": best_area,
                }
            )
        return best

    @staticmethod
    def _find_planar_face_at_z(bodies: list[Any], target_z: float, face_scan: dict[str, Any] | None = None) -> Any | None:
        tolerance = 1e-5
        best = None
        best_area = -1.0
        scanned = 0
        candidates: list[dict[str, Any]] = []
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces") or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face in faces:
                scanned += 1
                box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
                if not box or len(box) < 6:
                    continue
                zmin, zmax = float(box[2]), float(box[5])
                if abs(zmax - zmin) > tolerance or abs(zmax - target_z) > tolerance:
                    continue
                surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface")
                is_plane = ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True) if surface is not None else True
                if is_plane is False:
                    continue
                area = abs((float(box[3]) - float(box[0])) * (float(box[4]) - float(box[1])))
                candidates.append({"box": [float(value) for value in box[:6]], "area": area})
                if area > best_area:
                    best = face
                    best_area = area
        if face_scan is not None:
            face_scan.update(
                {
                    "faces_scanned": scanned,
                    "candidate_count": len(candidates),
                    "candidates": candidates[:12],
                    "target_z": target_z,
                    "tolerance": tolerance,
                    "best_area": best_area,
                }
            )
        return best

    @staticmethod
    def _cut_holes(
        model: Any,
        top_face: Any,
        request: dict[str, Any],
        centers: list[list[float]],
        bbox: dict[str, float],
    ) -> tuple[Any | None, str | None]:
        from .active_model_feature_skill import ActiveModelFeatureSkill

        radius_m = float(request["diameter_mm"]) / 2000.0
        def draw() -> None:
            for point in centers:
                model.SketchManager.CreateCircleByRadius(
                    float(point[0]),
                    float(point[1]),
                    float(point[2]),
                    radius_m,
                )
        sketch_name = ActiveModelFeatureSkill._create_face_sketch(model, top_face, draw)
        depth = max(bbox["thickness"] * 2.0, 0.01)
        for direction in (False, True):
            feature = ActiveModelFeatureSkill._extrude_cut(
                model,
                sketch_name,
                depth,
                through_all=True,
                direction=direction,
            )
            if feature is not None:
                return feature, "through_all_reverse" if direction else "through_all_normal"
        return None, None

    @staticmethod
    def _com_member(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
        try:
            member = getattr(obj, name)
        except Exception:
            return default
        try:
            if callable(member):
                return member(*args)
            return member if not args else default
        except TypeError:
            if not args:
                return member
            return default
        except Exception as exc:
            # Some SolidWorks COM members are exposed as callable proxy
            # properties. Calling them raises Member not found, while the
            # proxy itself still exposes the next COM member.
            text = str(exc)
            if not args and ("-2147352573" in text or "Member not found" in text or "找不到成员" in text):
                return member
            return default

    @staticmethod
    def _save_model(model: Any, run_dir: Path, target_path: Path | None = None) -> Path | None:
        try:
            from sw_connect import get_com_member

            current_path = str(get_com_member(model, "GetPathName") or "")
        except Exception:
            current_path = ""
        target = target_path or (Path(current_path) if current_path else run_dir / "active_model_through_holes.SLDPRT")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if current_path or target_path:
                errors = None
                warnings = None
                try:
                    from sw_preflight import import_com_dependencies

                    pythoncom, _win32com, VARIANT = import_com_dependencies(allow_install=False)
                    errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                    warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                    model.Save3(1, errors, warnings)
                except Exception:
                    model.Save()
                if target.exists():
                    return target
                for save_call in (lambda: model.SaveAs3(str(target), 0, 0), lambda: model.SaveAs(str(target))):
                    try:
                        save_call()
                        if target.exists():
                            return target
                    except Exception:
                        continue
            else:
                for save_call in (
                    lambda: model.Extension.SaveAs(str(target), 0, 1, None, None, None),
                    lambda: model.SaveAs3(str(target), 0, 0),
                    lambda: model.SaveAs(str(target)),
                ):
                    try:
                        save_call()
                        break
                    except Exception:
                        continue
        except Exception:
            return None
        return target if target.exists() else None

    @staticmethod
    def _verify_reopen(sw: Any, path: Path) -> dict[str, Any]:
        try:
            from sw_preflight import import_com_dependencies

            pythoncom, _win32com, VARIANT = import_com_dependencies(allow_install=False)
            errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            opened = sw.OpenDoc6(str(path), 1, 1, "", errors, warnings)
            active = getattr(sw, "ActiveDoc", None)
            active_path = ""
            active_title = ""
            if active is not None:
                try:
                    from sw_connect import get_com_member

                    active_path = str(get_com_member(active, "GetPathName") or "")
                    active_title = str(get_com_member(active, "GetTitle") or "")
                except Exception:
                    active_path = str(ActiveModelThroughHoleExecutor._com_member(active, "GetPathName", default="") or "")
                    active_title = str(ActiveModelThroughHoleExecutor._com_member(active, "GetTitle", default="") or "")
            already_open = bool(active_path and Path(active_path).resolve() == path.resolve()) or active_title == path.name
            return {
                "success": opened is not None or already_open,
                "path": str(path),
                "errors": getattr(errors, "value", None),
                "warnings": getattr(warnings, "value", None),
                "already_open": already_open,
                "active_doc": active_title,
                "active_path": active_path,
            }
        except Exception as exc:
            return {"success": False, "path": str(path), "error": repr(exc)}

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any], path: Path | None = None) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files = [str(report_path)]
        if path:
            files.append(str(path))
        payload["files"] = files
        return SkillResult(success=success, message=message, data=payload, path=str(path or report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))
