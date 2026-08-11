from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .geometry_resolver import FaceGeometryResolver
from .models import SkillResult


SUPPORTED_FEATURES = {"shell", "draft", "reference_geometry"}


class FeatureManagementSkill:
    """Apply shell, draft, and reference geometry to a task-owned Part."""

    PLANE_ALIASES = {
        "front": "Front Plane",
        "front plane": "Front Plane",
        "前视基准面": "Front Plane",
        "top": "Top Plane",
        "top plane": "Top Plane",
        "上视基准面": "Top Plane",
        "right": "Right Plane",
        "right plane": "Right Plane",
        "右视基准面": "Right Plane",
    }

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in SUPPORTED_FEATURES:
            return SkillResult(False, f"Unsupported feature-management type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"feature_management_{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"{feature_type}_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            validation = self._validate_active_part(model)
            if not validation.get("success"):
                return self._result(False, str(validation["message"]), report_path, validation)

            handlers: dict[str, Callable[[Any, dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
                "shell": self._create_shell,
                "draft": self._create_draft,
                "reference_geometry": self._create_reference_geometry,
            }
            operations: list[dict[str, Any]] = []
            for feature in features:
                request = self.normalize_request(feature_type, feature.get("params", {}))
                if not request.get("success"):
                    operations.append({**request, "name": feature.get("name") or feature_type})
                    return self._result(False, str(request.get("message")), report_path, {"operations": operations})
                body_info = ActiveModelThroughHoleExecutor._body_info(model)
                if not body_info.get("success"):
                    return self._result(False, str(body_info.get("message")), report_path, {"operations": operations})
                operation = handlers[feature_type](model, feature, request, body_info)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(False, str(operation.get("message")), report_path, {"operations": operations})

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": "active_model",
                "feature_type": feature_type,
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": False,
                "side_effects": {
                    "modifies_active_doc": True,
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                },
            }
            return self._result(True, f"SolidWorks {feature_type} created and verified.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"{feature_type} failed: {exc}",
                report_path,
                {"feature_type": feature_type, "error": repr(exc), "traceback": traceback.format_exc()},
            )

    @classmethod
    def normalize_request(cls, feature_type: str, params: dict[str, Any]) -> dict[str, Any]:
        if feature_type == "shell":
            thickness = cls._number(params, "thickness_mm", "thickness", "wall_thickness_mm", "wall_thickness")
            if thickness is None or not 0.0 < thickness <= 100.0:
                return {"success": False, "message": "shell requires thickness_mm greater than 0 and at most 100."}
            remove_faces = params.get("remove_faces") or params.get("opening_faces") or params.get("remove_face")
            if isinstance(remove_faces, str):
                remove_faces = [remove_faces]
            if not isinstance(remove_faces, list) or not remove_faces:
                return {"success": False, "message": "shell requires an explicit remove_faces list."}
            normalized_faces = [str(item).strip().lower() for item in remove_faces]
            valid_faces = {"top", "bottom", "x_min", "x_max", "y_min", "y_max"}
            if any(item not in valid_faces for item in normalized_faces):
                return {"success": False, "message": f"shell remove_faces must use {sorted(valid_faces)}."}
            return {
                "success": True,
                "type": "shell",
                "thickness_mm": float(thickness),
                "outward": bool(params.get("outward", False)),
                "remove_faces": list(dict.fromkeys(normalized_faces)),
            }

        if feature_type == "draft":
            angle = cls._number(params, "angle_deg", "angle")
            if angle is None or not 0.0 < angle <= 30.0:
                return {"success": False, "message": "draft requires angle_deg greater than 0 and at most 30."}
            neutral = str(params.get("neutral_plane") or params.get("neutral_face") or "").strip().lower()
            if neutral not in {"top", "bottom", "explicit_planar_face_signature"}:
                return {
                    "success": False,
                    "message": (
                        "draft neutral_plane must be top, bottom, or "
                        "explicit_planar_face_signature."
                    ),
                }
            targets = str(params.get("target_faces") or params.get("faces") or "").strip().lower()
            if targets not in {"outer_vertical_faces", "explicit_planar_face_signatures"}:
                return {
                    "success": False,
                    "message": (
                        "draft target_faces must be outer_vertical_faces or "
                        "explicit_planar_face_signatures."
                    ),
                }
            request = {
                "success": True,
                "type": "draft",
                "angle_deg": float(angle),
                "angle_rad": math.radians(float(angle)),
                "neutral_plane": neutral,
                "target_faces": targets,
                "flip_direction": bool(params.get("flip_direction", False)),
                "propagation_type": int(params.get("propagation_type", 0) or 0),
            }
            if neutral == "explicit_planar_face_signature":
                signature = cls._normalize_planar_face_signature(
                    params.get("neutral_face_signature"),
                    "neutral_face_signature",
                )
                if not signature.get("success"):
                    return signature
                request["neutral_face_signature"] = signature["signature"]
            if targets == "explicit_planar_face_signatures":
                raw_signatures = params.get("target_face_signatures")
                if not isinstance(raw_signatures, list) or not raw_signatures:
                    return {
                        "success": False,
                        "message": (
                            "explicit_planar_face_signatures requires a non-empty "
                            "target_face_signatures list."
                        ),
                    }
                normalized_signatures: list[dict[str, Any]] = []
                for index, raw_signature in enumerate(raw_signatures):
                    signature = cls._normalize_planar_face_signature(
                        raw_signature,
                        f"target_face_signatures[{index}]",
                    )
                    if not signature.get("success"):
                        return signature
                    normalized_signatures.append(signature["signature"])
                request["target_face_signatures"] = normalized_signatures
            tolerance = cls._number(params, "face_tolerance_mm")
            if tolerance is None:
                tolerance = 0.03
            if not 0.0 < tolerance <= 1.0:
                return {
                    "success": False,
                    "message": "draft face_tolerance_mm must be greater than 0 and at most 1.",
                }
            request["face_tolerance_mm"] = float(tolerance)
            return request

        if feature_type != "reference_geometry":
            return {"success": False, "message": f"Unsupported feature-management type: {feature_type}"}
        reference_type = str(params.get("reference_type") or params.get("type") or "").strip().lower()
        reference_type = {"plane": "offset_plane", "offset": "offset_plane", "axis": "axis_two_planes"}.get(reference_type, reference_type)
        if reference_type == "offset_plane":
            base_plane = cls._plane_name(params.get("base_plane") or params.get("reference_plane"))
            offset = cls._number(params, "offset_mm", "distance_mm", "offset", "distance")
            if base_plane is None or offset is None or abs(offset) <= 1e-9:
                return {"success": False, "message": "offset_plane requires base_plane and a non-zero offset_mm."}
            return {
                "success": True,
                "type": "reference_geometry",
                "reference_type": "offset_plane",
                "base_plane": base_plane,
                "offset_mm": float(offset),
            }
        if reference_type == "axis_two_planes":
            first = cls._plane_name(params.get("plane_1") or params.get("first_plane"))
            second = cls._plane_name(params.get("plane_2") or params.get("second_plane"))
            if first is None or second is None or first == second:
                return {"success": False, "message": "axis_two_planes requires two different explicit base planes."}
            return {
                "success": True,
                "type": "reference_geometry",
                "reference_type": "axis_two_planes",
                "plane_1": first,
                "plane_2": second,
            }
        return {"success": False, "message": "reference_type must be offset_plane or axis_two_planes."}

    def _create_shell(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        bbox = body_info["bbox"]
        minimum_span_mm = min(float(bbox["length"]), float(bbox["width"]), float(bbox["thickness"])) * 1000.0
        if request["thickness_mm"] * 2.0 >= minimum_span_mm:
            return {"success": False, "message": "shell thickness must be less than half the minimum body span."}
        targets = []
        for selector in request["remove_faces"]:
            face = self._find_boundary_planar_face(body_info["bodies"], bbox, selector)
            if face is None:
                return {"success": False, "message": f"Could not find shell removal face: {selector}"}
            targets.append(face)
        model.ClearSelection2(True)
        for index, face in enumerate(targets):
            if not self._select_entity(face, append=index > 0, mark=1):
                return {"success": False, "message": f"Could not select shell removal face: {request['remove_faces'][index]}"}

        before_tree = ActiveModelFeatureSkill._feature_tree(model)
        face_count_before = self._face_count(body_info.get("bodies", []))
        model.InsertFeatureShell(self._m(request["thickness_mm"]), bool(request["outward"]))
        model.ForceRebuild3(False)
        created = self._new_feature(model, before_tree, ("shell",))
        if created is None:
            return {"success": False, "message": "SolidWorks did not create a Shell feature."}
        name = str(feature.get("name") or "Shell")
        ActiveModelFeatureSkill._name_feature(created, name)
        after = ActiveModelThroughHoleExecutor._body_info(model)
        if not after.get("success") or len(after.get("bodies", [])) != len(body_info.get("bodies", [])):
            return {"success": False, "message": "Shell did not preserve the expected solid-body count."}
        face_count_after = self._face_count(after.get("bodies", []))
        if face_count_after <= face_count_before:
            return {"success": False, "message": "Shell feature did not add the expected internal/opening topology."}
        return {
            "success": True,
            "name": name,
            "type": "shell",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "face_count_before": face_count_before,
            "face_count_after": face_count_after,
            "body_count_after": len(after.get("bodies", [])),
        }

    def _create_draft(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        bbox = body_info["bbox"]
        resolution_reports: dict[str, Any] = {}
        if request["neutral_plane"] == "explicit_planar_face_signature":
            neutral, report = self._resolve_planar_face_signature(
                model,
                body_info["bodies"],
                request["neutral_face_signature"],
                request["face_tolerance_mm"],
            )
            resolution_reports["neutral_face"] = report
        else:
            neutral = self._find_boundary_planar_face(
                body_info["bodies"],
                bbox,
                request["neutral_plane"],
            )
        if neutral is None:
            return {
                "success": False,
                "message": f"Could not resolve draft neutral plane: {request['neutral_plane']}",
                "geometry_resolution": resolution_reports,
            }

        if request["target_faces"] == "explicit_planar_face_signatures":
            side_faces = []
            target_reports: list[dict[str, Any]] = []
            fingerprints: set[str] = set()
            for signature in request["target_face_signatures"]:
                face, report = self._resolve_planar_face_signature(
                    model,
                    body_info["bodies"],
                    signature,
                    request["face_tolerance_mm"],
                )
                target_reports.append(report)
                if face is None:
                    return {
                        "success": False,
                        "message": "Could not resolve one explicit draft target face.",
                        "geometry_resolution": {
                            **resolution_reports,
                            "target_faces": target_reports,
                        },
                    }
                resolved = report.get("resolved_signature") or {}
                fingerprint = json.dumps(
                    {
                        "normal": resolved.get("normal"),
                        "plane_coordinate_m": resolved.get("plane_coordinate_m"),
                        "face_box_m": resolved.get("face_box_m"),
                    },
                    sort_keys=True,
                )
                if fingerprint in fingerprints:
                    return {
                        "success": False,
                        "message": "Explicit draft target signatures resolved to the same face.",
                        "geometry_resolution": {
                            **resolution_reports,
                            "target_faces": target_reports,
                        },
                    }
                fingerprints.add(fingerprint)
                side_faces.append(face)
            resolution_reports["target_faces"] = target_reports
        else:
            side_faces = self._outer_vertical_faces(body_info["bodies"], bbox)
        if not side_faces:
            return {"success": False, "message": "Could not find draft target faces."}
        model.ClearSelection2(True)
        if not self._select_entity(neutral, append=False, mark=1):
            return {"success": False, "message": "Could not select the draft neutral plane."}
        for face in side_faces:
            if not self._select_entity(face, append=True, mark=2):
                return {"success": False, "message": "Could not select an outer vertical draft face."}

        before_tree = ActiveModelFeatureSkill._feature_tree(model)
        volume_before = ActiveModelThroughHoleExecutor._solid_volume(body_info["bodies"])
        created = model.FeatureManager.InsertMultiFaceDraft(
            float(request["angle_rad"]),
            bool(request["flip_direction"]),
            False,
            int(request["propagation_type"]),
            False,
            False,
        )
        model.ForceRebuild3(False)
        if created is None:
            created = self._new_feature(model, before_tree, ("draft",))
        if created is None:
            return {"success": False, "message": "SolidWorks InsertMultiFaceDraft returned no Draft feature."}
        name = str(feature.get("name") or "Draft")
        ActiveModelFeatureSkill._name_feature(created, name)
        after = ActiveModelThroughHoleExecutor._body_info(model)
        if not after.get("success") or len(after.get("bodies", [])) != len(body_info.get("bodies", [])):
            return {"success": False, "message": "Draft did not preserve the expected solid-body count."}
        volume_after = ActiveModelThroughHoleExecutor._solid_volume(after.get("bodies", []))
        bbox_changed = not self._bbox_equal(bbox, after.get("bbox", {}))
        volume_changed = bool(
            volume_before is not None
            and volume_after is not None
            and abs(volume_after - volume_before) > max(1e-15, abs(volume_before) * 1e-9)
        )
        if not bbox_changed and not volume_changed:
            return {"success": False, "message": "Draft feature did not measurably change the solid geometry."}
        return {
            "success": True,
            "name": name,
            "type": "draft",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "selected_face_count": len(side_faces),
            "geometry_resolution": resolution_reports,
            "bbox_before_m": bbox,
            "bbox_after_m": after.get("bbox", {}),
            "bbox_changed": bbox_changed,
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_changed": volume_changed,
            "body_count_after": len(after.get("bodies", [])),
        }

    @staticmethod
    def _resolve_planar_face_signature(
        model: Any,
        bodies: list[Any],
        signature: dict[str, Any],
        tolerance_mm: float,
    ) -> tuple[Any | None, dict[str, Any]]:
        resolver = FaceGeometryResolver(
            minimum_score=0.85,
            minimum_margin=0.05,
            linear_tolerance_m=float(tolerance_mm) / 1000.0,
            normal_tolerance_deg=1.0,
            area_relative_tolerance=0.05,
        )
        result = resolver.resolve(
            model_doc=model,
            bodies=bodies,
            expected_signature=signature,
            allow_geometry_fallback=True,
        )
        return result.entity if result.success else None, result.as_dict()

    def _create_reference_geometry(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        _body_info: dict[str, Any],
    ) -> dict[str, Any]:
        before_tree = ActiveModelFeatureSkill._feature_tree(model)
        model.ClearSelection2(True)
        created = None
        if request["reference_type"] == "offset_plane":
            if not self._select_named_plane(model, request["base_plane"], append=False):
                return {"success": False, "message": f"Could not select base plane: {request['base_plane']}"}
            constraint = 8
            # SolidWorks places an unflipped distance plane on the positive
            # model-axis side of each standard plane. OptionFlip selects the
            # negative side while the API distance itself remains positive.
            if float(request["offset_mm"]) < 0.0:
                constraint |= 256
            created = model.FeatureManager.InsertRefPlane(
                constraint,
                self._m(abs(float(request["offset_mm"]))),
                0,
                0.0,
                0,
                0.0,
            )
        else:
            if not self._select_named_plane(model, request["plane_1"], append=False):
                return {"success": False, "message": f"Could not select first axis plane: {request['plane_1']}"}
            if not self._select_named_plane(model, request["plane_2"], append=True):
                return {"success": False, "message": f"Could not select second axis plane: {request['plane_2']}"}
            if not bool(model.InsertAxis2(True)):
                return {"success": False, "message": "SolidWorks InsertAxis2 returned false."}
        model.ForceRebuild3(False)
        if created is None:
            expected_types = ("axis", "refaxis") if request["reference_type"] == "axis_two_planes" else ("refplane", "plane")
            created = self._new_feature(model, before_tree, expected_types)
        if created is None:
            return {"success": False, "message": "SolidWorks did not create the requested reference geometry."}
        name = str(feature.get("name") or ("ReferenceAxis" if request["reference_type"] == "axis_two_planes" else "OffsetPlane"))
        ActiveModelFeatureSkill._name_feature(created, name)
        geometry_validation: dict[str, Any] | None = None
        if request["reference_type"] == "offset_plane":
            geometry_validation = self._validate_offset_plane_geometry(created, request)
            if not geometry_validation.get("success"):
                return {
                    "success": False,
                    "message": str(geometry_validation["message"]),
                    "name": name,
                    "type": "reference_geometry",
                    "request": request,
                    "feature_name": ActiveModelFeatureSkill._feature_name(created),
                    "geometry_validation": geometry_validation,
                }
        hidden = False
        try:
            model.ClearSelection2(True)
            if bool(ActiveModelThroughHoleExecutor._com_member(created, "Select2", False, 0, default=False)):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankRefGeom")
                hidden = True
        finally:
            model.ClearSelection2(True)
        return {
            "success": True,
            "name": name,
            "type": "reference_geometry",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "geometry_validation": geometry_validation,
            "reference_hidden": hidden,
        }

    @staticmethod
    def _validate_offset_plane_geometry(created: Any, request: dict[str, Any]) -> dict[str, Any]:
        member = ActiveModelThroughHoleExecutor._com_member
        plane = created
        corner_points = member(plane, "CornerPoints", default=None)
        if corner_points is None:
            plane = member(created, "GetSpecificFeature2", default=None)
            corner_points = member(plane, "CornerPoints", default=None)
        if not isinstance(corner_points, (tuple, list)) or len(corner_points) != 4:
            return {
                "success": False,
                "message": "Reference-plane geometry validation could not read four IRefPlane.CornerPoints.",
            }
        point_values: list[list[float]] = []
        try:
            for point in corner_points:
                values = member(point, "ArrayData", default=None)
                if not isinstance(values, (tuple, list)) or len(values) < 3:
                    raise ValueError("invalid reference-plane corner point")
                point_values.append([float(values[index]) for index in range(3)])
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": "Reference-plane geometry validation returned a non-numeric corner point.",
            }
        axis_by_plane = {"Right Plane": 0, "Top Plane": 1, "Front Plane": 2}
        axis = axis_by_plane.get(str(request.get("base_plane") or ""))
        if axis is None:
            return {
                "success": False,
                "message": f"Unsupported base plane for geometry validation: {request.get('base_plane')}",
            }
        expected_m = float(request["offset_mm"]) / 1000.0
        measured_coordinates = [point[axis] for point in point_values]
        measured_m = sum(measured_coordinates) / len(measured_coordinates)
        coordinate_spread_m = max(measured_coordinates) - min(measured_coordinates)
        tolerance_m = 1e-7
        success = (
            coordinate_spread_m <= tolerance_m
            and abs(measured_m - expected_m) <= tolerance_m
        )
        return {
            "success": success,
            "message": (
                "Reference plane has the requested measured offset."
                if success
                else (
                    "Reference plane node exists, but its measured world-space offset "
                    f"is {measured_m * 1000.0:.6f} mm instead of {expected_m * 1000.0:.6f} mm."
                )
            ),
            "base_plane": request["base_plane"],
            "normal_axis": axis,
            "expected_offset_mm": expected_m * 1000.0,
            "measured_offset_mm": measured_m * 1000.0,
            "corner_points_m": point_values,
            "normal_coordinate_spread_mm": coordinate_spread_m * 1000.0,
            "tolerance_mm": tolerance_m * 1000.0,
        }

    @staticmethod
    def _validate_active_part(model: Any) -> dict[str, Any]:
        if model is None:
            return {"success": False, "message": "ActiveDoc does not exist."}
        title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
        if int(ActiveModelThroughHoleExecutor._com_member(model, "GetType", default=0) or 0) != 1:
            return {"success": False, "message": f"ActiveDoc is not a Part: {title}", "active_doc": title}
        info = ActiveModelThroughHoleExecutor._body_info(model)
        if not info.get("success"):
            return {"success": False, "message": str(info.get("message")), "active_doc": title}
        return {"success": True, "active_doc": title}

    @classmethod
    def _find_boundary_planar_face(
        cls,
        bodies: list[Any],
        bbox: dict[str, float],
        selector: str,
    ) -> Any | None:
        mapping = {
            "x_min": (0, "xmin"),
            "x_max": (0, "xmax"),
            "y_min": (1, "ymin"),
            "y_max": (1, "ymax"),
            "bottom": (2, "zmin"),
            "top": (2, "zmax"),
        }
        axis, bound = mapping[selector]
        target = float(bbox[bound])
        tolerance = 1e-5
        best = None
        best_area = -1.0
        for face in cls._faces(bodies):
            box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
            if not box or len(box) < 6:
                continue
            box = [float(value) for value in box]
            if abs(box[axis + 3] - box[axis]) > tolerance:
                continue
            if abs((box[axis] + box[axis + 3]) / 2.0 - target) > tolerance:
                continue
            surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface")
            if surface is not None and ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True) is False:
                continue
            spans = [abs(box[index + 3] - box[index]) for index in range(3) if index != axis]
            area = spans[0] * spans[1]
            if area > best_area:
                best, best_area = face, area
        return best

    @classmethod
    def _outer_vertical_faces(cls, bodies: list[Any], bbox: dict[str, float]) -> list[Any]:
        result: list[Any] = []
        for selector in ("x_min", "x_max", "y_min", "y_max"):
            face = cls._find_boundary_planar_face(bodies, bbox, selector)
            if face is not None:
                result.append(face)
        return result

    @staticmethod
    def _faces(bodies: list[Any]) -> list[Any]:
        result: list[Any] = []
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces") or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            result.extend(face for face in faces if face is not None)
        return result

    @staticmethod
    def _select_entity(entity: Any, append: bool, mark: int) -> bool:
        try:
            return bool(entity.Select2(append, mark))
        except Exception:
            return False

    @classmethod
    def _select_named_plane(cls, model: Any, name: str, append: bool) -> bool:
        candidates = [name]
        reverse = {"Front Plane": "前视基准面", "Top Plane": "上视基准面", "Right Plane": "右视基准面"}
        if name in reverse:
            candidates.append(reverse[name])
        for candidate in candidates:
            feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", candidate)
            if feature is not None:
                try:
                    if feature.Select2(append, 0):
                        return True
                except Exception:
                    pass
            try:
                if model.Extension.SelectByID2(candidate, "PLANE", 0, 0, 0, append, 0, None, 0):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _new_feature(model: Any, before_tree: list[dict[str, str]], expected_types: tuple[str, ...]) -> Any | None:
        before_names = {item.get("name") for item in before_tree}
        candidates: list[dict[str, str]] = []
        for item in ActiveModelFeatureSkill._feature_tree(model):
            if item.get("name") in before_names:
                continue
            if any(token in str(item.get("type", "")).lower() for token in expected_types):
                candidates.append(item)
        for item in reversed(candidates):
            feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", item.get("name"))
            if feature is not None:
                return feature
        return None

    @staticmethod
    def _face_count(bodies: list[Any]) -> int:
        return len(FeatureManagementSkill._faces(bodies))

    @staticmethod
    def _bbox_equal(first: dict[str, float], second: dict[str, float], tolerance: float = 1e-7) -> bool:
        keys = ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")
        return bool(second) and all(abs(float(first.get(key, 0)) - float(second.get(key, 0))) <= tolerance for key in keys)

    @classmethod
    def _plane_name(cls, value: Any) -> str | None:
        if value is None:
            return None
        return cls.PLANE_ALIASES.get(str(value).strip().lower())

    @staticmethod
    def _number(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = values.get(key)
            if value is not None and value != "":
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @classmethod
    def _normalize_planar_face_signature(
        cls,
        raw: Any,
        label: str,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": f"draft {label} must be an object."}
        signature: dict[str, Any] = {"planar": True, "surface_type": "plane"}
        constraints = 0

        normal = raw.get("normal") or raw.get("face_normal")
        if normal is not None:
            vector = cls._finite_vector(normal, 3)
            if vector is None or sum(value * value for value in vector) <= 1e-12:
                return {"success": False, "message": f"draft {label}.normal must be a non-zero 3D vector."}
            signature["normal"] = vector
            constraints += 1

        if raw.get("normal_axis") is not None:
            try:
                axis = int(raw["normal_axis"])
            except (TypeError, ValueError):
                axis = -1
            if axis not in {0, 1, 2}:
                return {"success": False, "message": f"draft {label}.normal_axis must be 0, 1, or 2."}
            signature["normal_axis"] = axis
            constraints += 1

        coordinate = cls._number(raw, "plane_coordinate_mm")
        if coordinate is not None:
            signature["plane_coordinate_m"] = coordinate / 1000.0
            constraints += 1

        origin = raw.get("plane_origin_mm") or raw.get("face_origin_mm")
        if origin is not None:
            vector = cls._finite_vector(origin, 3)
            if vector is None:
                return {"success": False, "message": f"draft {label}.plane_origin_mm must contain 3 finite values."}
            signature["plane_origin_m"] = [value / 1000.0 for value in vector]
            constraints += 1

        box = raw.get("face_box_mm") or raw.get("bbox_mm")
        if box is not None:
            vector = cls._finite_vector(box, 6)
            if vector is None or any(vector[index] > vector[index + 3] for index in range(3)):
                return {"success": False, "message": f"draft {label}.face_box_mm must be an ordered 6-value box."}
            signature["face_box_m"] = [value / 1000.0 for value in vector]
            constraints += 1

        area = cls._number(raw, "area_mm2")
        if area is not None:
            if area <= 0:
                return {"success": False, "message": f"draft {label}.area_mm2 must be greater than 0."}
            signature["area_m2"] = area / 1_000_000.0
            constraints += 1

        for key in ("feature_name", "body_name"):
            value = str(raw.get(key) or "").strip()
            if value:
                signature[key] = value
                constraints += 1
        if raw.get("body_index") is not None:
            try:
                body_index = int(raw["body_index"])
            except (TypeError, ValueError):
                body_index = -1
            if body_index < 0:
                return {"success": False, "message": f"draft {label}.body_index must be non-negative."}
            signature["body_index"] = body_index
            constraints += 1

        if constraints < 2:
            return {
                "success": False,
                "message": (
                    f"draft {label} requires at least two deterministic constraints "
                    "such as normal_axis, plane_coordinate_mm, and face_box_mm."
                ),
            }
        return {"success": True, "signature": signature}

    @staticmethod
    def _finite_vector(value: Any, length: int) -> list[float] | None:
        if not isinstance(value, (tuple, list)) or len(value) != length:
            return None
        try:
            result = [float(item) for item in value]
        except (TypeError, ValueError):
            return None
        return result if all(math.isfinite(item) for item in result) else None

    @staticmethod
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

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
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        payload["files"] = list(dict.fromkeys([str(item) for item in payload.get("files", [])] + [str(report_path)]))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(success, message, data=payload, path=str(report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))
