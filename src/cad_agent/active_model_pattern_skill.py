from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .models import SkillResult
from .production_fillet_skill import ProductionFilletSkill


SUPPORTED_PATTERN_FEATURES = {"linear_pattern", "circular_pattern", "mirror"}


class ActiveModelPatternSkill:
    """Apply production pattern and mirror features to the active Part only."""

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in SUPPORTED_PATTERN_FEATURES:
            return SkillResult(False, f"Unsupported active-model pattern type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"active_{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"active_model_{feature_type}_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            if model is None:
                return self._result(False, "ActiveDoc does not exist.", report_path, {"feature_type": feature_type})
            title = str(get_com_member(model, "GetTitle") or "")
            if int(get_com_member(model, "GetType")) != 1:
                return self._result(False, f"ActiveDoc is not a Part: {title}", report_path, {"active_doc": title})

            operations: list[dict[str, Any]] = []
            for feature in features:
                body_info = ActiveModelThroughHoleExecutor._body_info(model)
                if not body_info.get("success"):
                    return self._result(False, str(body_info.get("message")), report_path, {"active_doc": title})
                canonical = self.normalize_request(feature_type, feature.get("params", {}))
                if not canonical.get("success"):
                    return self._result(
                        False,
                        str(canonical.get("message")),
                        report_path,
                        {"active_doc": title, "feature_type": feature_type, "operations": operations, "request": canonical},
                    )
                before = self._model_evidence(model)
                operation = self._execute(model, feature, canonical, body_info)
                operation["model_before"] = before
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or f"{feature_type} failed"),
                        report_path,
                        {"active_doc": title, "feature_type": feature_type, "operations": operations},
                    )
                model.ClearSelection2(True)
                model.ForceRebuild3(False)
                after = self._model_evidence(model)
                operation["model_after"] = after
                geometry_validation = self._verify_geometry(before, after)
                operation["geometry_validation"] = geometry_validation
                if not geometry_validation.get("success"):
                    operation["success"] = False
                    operation["message"] = str(geometry_validation.get("message"))
                    return self._result(
                        False,
                        str(geometry_validation.get("message")),
                        report_path,
                        {"active_doc": title, "feature_type": feature_type, "operations": operations},
                    )

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            data = {
                "active_doc": title,
                "mode": "active_model",
                "feature_type": feature_type,
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "feature_tree": self._feature_tree(model),
                "saved_by_this_skill": False,
                "side_effects": {
                    "modifies_active_doc": True,
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                },
            }
            return self._result(True, f"Active-model {feature_type} created.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"active_model {feature_type} failed: {exc}",
                report_path,
                {"feature_type": feature_type, "error": repr(exc), "traceback": traceback.format_exc()},
            )

    @classmethod
    def normalize_request(cls, feature_type: str, params: dict[str, Any]) -> dict[str, Any]:
        seeds = cls._seed_names(params)
        if not seeds:
            return {"success": False, "message": "seed_features must name the existing feature(s) to pattern or mirror."}
        if feature_type == "linear_pattern":
            count_1 = int(params.get("count_1") or params.get("count_x") or params.get("count") or 0)
            spacing_1 = float(params.get("spacing_1") or params.get("spacing_x") or params.get("spacing") or 0)
            direction_1 = str(params.get("direction_1") or params.get("direction") or "").lower().replace(" ", "")
            count_2 = int(params.get("count_2") or params.get("count_y") or 1)
            spacing_2 = float(params.get("spacing_2") or params.get("spacing_y") or 0)
            direction_2 = str(params.get("direction_2") or "").lower().replace(" ", "")
            if count_1 < 2 or spacing_1 <= 0 or not cls._valid_axis_token(direction_1):
                return {"success": False, "message": "linear_pattern requires count_1>=2, spacing_1>0, and direction_1=x/y/z."}
            if count_2 > 1 and (spacing_2 <= 0 or not cls._valid_axis_token(direction_2) or cls._axis(direction_1) == cls._axis(direction_2)):
                return {"success": False, "message": "A two-direction linear pattern requires a distinct direction_2, count_2>=2, and spacing_2>0."}
            return {
                "success": True,
                "seed_features": seeds,
                "count_1": count_1,
                "spacing_1_mm": spacing_1,
                "direction_1": direction_1,
                "count_2": max(count_2, 1),
                "spacing_2_mm": spacing_2 if count_2 > 1 else 0.0,
                "direction_2": direction_2 if count_2 > 1 else None,
                "geometry_pattern": bool(params.get("geometry_pattern", True)),
                "reverse_1": bool(params.get("reverse_1", False)),
                "reverse_2": bool(params.get("reverse_2", False)),
            }
        if feature_type == "circular_pattern":
            count = int(params.get("count") or params.get("instances") or 0)
            equal_spacing = bool(params.get("equal_spacing", True))
            total_angle = float(params.get("total_angle_deg") or params.get("angle_deg") or params.get("angle") or 360.0)
            spacing_angle_raw = params.get("spacing_angle_deg")
            if spacing_angle_raw is None:
                spacing_angle_raw = params.get("spacing_deg")
            if spacing_angle_raw is None and not equal_spacing:
                # Backward compatibility for the former ambiguous contract,
                # where total_angle_deg was passed to the API as pitch.
                spacing_angle_raw = params.get("total_angle_deg") or params.get("angle_deg") or params.get("angle")
            spacing_angle = float(spacing_angle_raw) if spacing_angle_raw is not None else None
            axis = str(params.get("axis") or params.get("direction_axis") or "").lower().replace(" ", "")
            valid_angle = (
                0 < total_angle <= 360.0
                if equal_spacing
                else spacing_angle is not None and 0 < spacing_angle <= 360.0
            )
            if count < 2 or not valid_angle or not cls._valid_axis_token(axis):
                return {
                    "success": False,
                    "message": (
                        "circular_pattern requires count>=2, axis=x/y/z, and either "
                        "0<total_angle_deg<=360 for equal spacing or "
                        "0<spacing_angle_deg<=360 for fixed pitch."
                    ),
                }
            center = params.get("axis_center_mm")
            if center is not None and (not isinstance(center, (list, tuple)) or len(center) < 2):
                return {"success": False, "message": "axis_center_mm must contain two coordinates for a z-axis pattern."}
            return {
                "success": True,
                "seed_features": seeds,
                "count": count,
                "total_angle_deg": total_angle,
                "spacing_angle_deg": spacing_angle,
                "pattern_angle_deg": total_angle if equal_spacing else spacing_angle,
                "axis": axis,
                "axis_feature": str(params.get("axis_feature") or ""),
                "axis_center_mm": [float(value) for value in center[:2]] if center is not None else None,
                "axis_tolerance_mm": float(params.get("axis_tolerance_mm") or 0.0),
                "equal_spacing": equal_spacing,
                "geometry_pattern": bool(params.get("geometry_pattern", True)),
                "reverse": bool(params.get("reverse", False)),
            }
        if feature_type == "mirror":
            plane = str(params.get("mirror_plane") or params.get("plane") or "").strip()
            if not cls._canonical_plane(plane):
                return {"success": False, "message": "mirror requires mirror_plane=Front Plane/Top Plane/Right Plane (or XY/XZ/YZ)."}
            if str(params.get("scope") or "features") != "features":
                return {"success": False, "message": "The production mirror executor currently supports feature scope only."}
            return {
                "success": True,
                "seed_features": seeds,
                "mirror_plane": cls._canonical_plane(plane),
                "geometry_pattern": bool(params.get("geometry_pattern", False)),
                "scope": "features",
            }
        return {"success": False, "message": f"Unsupported pattern type: {feature_type}"}

    def _execute(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        feature_type = str(feature.get("type"))
        if feature_type == "linear_pattern":
            return self._create_linear_pattern(model, feature, request, body_info)
        if feature_type == "circular_pattern":
            return self._create_circular_pattern(model, feature, request, body_info)
        return self._create_mirror(model, feature, request)

    def _create_linear_pattern(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        selected = self._select_features(model, request["seed_features"], mark=4)
        if not selected.get("success"):
            return selected
        direction_1 = self._select_linear_direction(model, body_info["bodies"], request["direction_1"], mark=1)
        if not direction_1.get("success"):
            return direction_1
        direction_2 = None
        if request["count_2"] > 1:
            direction_2 = self._select_linear_direction(model, body_info["bodies"], request["direction_2"], mark=2)
            if not direction_2.get("success"):
                return direction_2
        flip_1 = bool(request["reverse_1"]) ^ bool(direction_1["edge_sign"] != direction_1["requested_sign"])
        flip_2 = False
        if direction_2 is not None:
            flip_2 = bool(request["reverse_2"]) ^ bool(direction_2["edge_sign"] != direction_2["requested_sign"])
        created = model.FeatureManager.FeatureLinearPattern3(
            int(request["count_1"]),
            float(request["spacing_1_mm"]) / 1000.0,
            int(request["count_2"]),
            float(request["spacing_2_mm"]) / 1000.0,
            flip_1,
            flip_2,
            "",
            "",
            bool(request["geometry_pattern"]),
            False,
        )
        if created is None:
            return {"success": False, "message": "SolidWorks FeatureLinearPattern3 returned no feature.", "request": request}
        self._name_feature(created, feature.get("name") or "LinearPattern")
        evidence = self._created_feature_evidence(created)
        if "lpattern" not in evidence["type_name"].lower():
            return {"success": False, "message": f"Unexpected linear pattern feature type: {evidence['type_name']}", "evidence": evidence}
        return {
            "success": True,
            "name": feature.get("name") or "LinearPattern",
            "type": "linear_pattern",
            "request": request,
            "seed_features": selected["selected_features"],
            "direction_1_evidence": direction_1,
            "direction_2_evidence": direction_2,
            "feature_name": evidence["name"],
            "feature_type_name": evidence["type_name"],
            "feature_error_code": evidence["error_code"],
        }

    def _create_circular_pattern(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        selected = self._select_features(model, request["seed_features"], mark=4)
        if not selected.get("success"):
            return selected
        axis = self._select_circular_axis(model, body_info, request, mark=1)
        if not axis.get("success"):
            return axis
        created = model.FeatureManager.FeatureCircularPattern4(
            int(request["count"]),
            math.radians(float(request["pattern_angle_deg"])),
            bool(request["reverse"]),
            "",
            bool(request["geometry_pattern"]),
            bool(request["equal_spacing"]),
            False,
        )
        if created is None:
            return {"success": False, "message": "SolidWorks FeatureCircularPattern4 returned no feature.", "request": request, "axis": axis}
        self._name_feature(created, feature.get("name") or "CircularPattern")
        evidence = self._created_feature_evidence(created)
        if "cirpattern" not in evidence["type_name"].lower():
            return {"success": False, "message": f"Unexpected circular pattern feature type: {evidence['type_name']}", "evidence": evidence}
        return {
            "success": True,
            "name": feature.get("name") or "CircularPattern",
            "type": "circular_pattern",
            "request": request,
            "seed_features": selected["selected_features"],
            "axis_evidence": axis,
            "feature_name": evidence["name"],
            "feature_type_name": evidence["type_name"],
            "feature_error_code": evidence["error_code"],
        }

    def _create_mirror(self, model: Any, feature: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        selected = self._select_features(model, request["seed_features"], mark=1)
        if not selected.get("success"):
            return selected
        plane = self._select_reference_plane(model, request["mirror_plane"], mark=2)
        if not plane.get("success"):
            return plane
        created = model.FeatureManager.InsertMirrorFeature2(
            False,
            bool(request["geometry_pattern"]),
            False,
            False,
            0,
        )
        if created is None:
            return {"success": False, "message": "SolidWorks InsertMirrorFeature2 returned no feature.", "request": request, "plane": plane}
        self._name_feature(created, feature.get("name") or "MirrorPattern")
        evidence = self._created_feature_evidence(created)
        if "mirror" not in evidence["type_name"].lower():
            return {"success": False, "message": f"Unexpected mirror feature type: {evidence['type_name']}", "evidence": evidence}
        return {
            "success": True,
            "name": feature.get("name") or "MirrorPattern",
            "type": "mirror",
            "request": request,
            "seed_features": selected["selected_features"],
            "mirror_plane_evidence": plane,
            "feature_name": evidence["name"],
            "feature_type_name": evidence["type_name"],
            "feature_error_code": evidence["error_code"],
        }

    def _select_features(self, model: Any, names: list[str], mark: int) -> dict[str, Any]:
        model.ClearSelection2(True)
        selected: list[str] = []
        for name in names:
            feature = self._find_feature(model, name)
            if feature is None:
                return {"success": False, "message": f"Seed feature does not exist in ActiveDoc: {name}", "selected_features": selected}
            try:
                if not feature.Select2(bool(selected), mark):
                    return {"success": False, "message": f"Could not select seed feature with mark {mark}: {name}", "selected_features": selected}
            except Exception as exc:
                return {"success": False, "message": f"Could not select seed feature {name}: {exc}", "selected_features": selected}
            selected.append(str(self._com_member(feature, "Name", default=name) or name))
        return {"success": True, "selected_features": selected, "selection_mark": mark}

    def _select_linear_direction(self, model: Any, bodies: list[Any], token: str, mark: int) -> dict[str, Any]:
        axis = self._axis(token)
        requested_sign = self._axis_sign(token)
        index = {"x": 0, "y": 1, "z": 2}[axis]
        best: tuple[float, Any, tuple[float, float, float]] | None = None
        for body in bodies:
            edges = self._as_sequence(self._com_member(body, "GetEdges"))
            for edge in edges:
                points = ProductionFilletSkill._edge_points(edge)
                if points is None:
                    continue
                start, end = points
                vector = tuple(float(end[i]) - float(start[i]) for i in range(3))
                length = math.sqrt(sum(value * value for value in vector))
                if length <= 1e-8 or abs(vector[index]) / length < 0.985:
                    continue
                if best is None or length > best[0]:
                    best = (length, edge, vector)
        if best is None:
            return {"success": False, "message": f"No model edge is aligned with the requested {axis.upper()} pattern direction."}
        select_data = self._select_data(model, mark)
        try:
            if not best[1].Select4(True, select_data):
                return {"success": False, "message": f"Could not select {axis.upper()} direction edge with mark {mark}."}
        except Exception as exc:
            return {"success": False, "message": f"Could not select pattern direction edge: {exc}"}
        edge_sign = 1 if best[2][index] >= 0 else -1
        return {
            "success": True,
            "axis": axis,
            "requested_sign": requested_sign,
            "edge_sign": edge_sign,
            "edge_vector": list(best[2]),
            "edge_length_m": best[0],
            "selection_mark": mark,
        }

    def _select_circular_axis(
        self,
        model: Any,
        body_info: dict[str, Any],
        request: dict[str, Any],
        mark: int,
    ) -> dict[str, Any]:
        axis = self._axis(request["axis"])
        index = {"x": 0, "y": 1, "z": 2}[axis]
        axis_feature = self._find_feature(model, request["axis_feature"]) if request.get("axis_feature") else None
        if axis_feature is not None:
            feature_type = str(self._com_member(axis_feature, "GetTypeName2", default="") or "")
            if feature_type.casefold() in {"refaxis", "referenceaxis", "axis"}:
                try:
                    if not axis_feature.Select2(True, mark):
                        return {
                            "success": False,
                            "message": f"Could not select reference axis with mark {mark}: {request['axis_feature']}",
                        }
                except Exception as exc:
                    return {
                        "success": False,
                        "message": f"Could not select reference axis {request['axis_feature']}: {exc}",
                    }
                return {
                    "success": True,
                    "axis": axis,
                    "axis_feature": str(self._com_member(axis_feature, "Name", default=request["axis_feature"])),
                    "axis_feature_type": feature_type,
                    "reference_kind": "explicit_reference_axis",
                    "selection_mark": mark,
                }
        face_sources: list[Any] = []
        if axis_feature is not None:
            face_sources.extend(self._as_sequence(self._com_member(axis_feature, "GetFaces")))
        if not face_sources:
            for body in body_info["bodies"]:
                face_sources.extend(self._as_sequence(self._com_member(body, "GetFaces")))

        bbox = body_info["bbox"]
        center_m = request.get("axis_center_mm")
        if center_m is not None:
            target = [float(value) / 1000.0 for value in center_m]
        elif axis == "z":
            target = [(bbox["xmin"] + bbox["xmax"]) / 2.0, (bbox["ymin"] + bbox["ymax"]) / 2.0]
        elif axis == "x":
            target = [(bbox["ymin"] + bbox["ymax"]) / 2.0, (bbox["zmin"] + bbox["zmax"]) / 2.0]
        else:
            target = [(bbox["xmin"] + bbox["xmax"]) / 2.0, (bbox["zmin"] + bbox["zmax"]) / 2.0]

        candidates: list[tuple[float, Any, list[float]]] = []
        for face in face_sources:
            surface = self._com_member(face, "GetSurface")
            if surface is None or not bool(self._com_member(surface, "IsCylinder", default=False)):
                continue
            params = self._com_member(surface, "CylinderParams")
            if not isinstance(params, (list, tuple)) or len(params) < 7:
                continue
            values = [float(value) for value in params[:7]]
            vector = values[3:6]
            norm = math.sqrt(sum(value * value for value in vector))
            if norm <= 1e-8 or abs(vector[index]) / norm < 0.985:
                continue
            perpendicular = [values[i] for i in range(3) if i != index]
            distance = math.dist(perpendicular, target)
            candidates.append((distance, face, values))
        if not candidates:
            return {"success": False, "message": f"No cylindrical reference aligned with the requested {axis.upper()} axis was found."}
        candidates.sort(key=lambda item: item[0])
        distance, face, values = candidates[0]
        default_tolerance = max(0.0005, min(bbox["length"], bbox["width"], bbox["thickness"] * 5.0) * 0.02)
        tolerance = float(request.get("axis_tolerance_mm") or 0.0) / 1000.0 or default_tolerance
        if not request.get("axis_feature") and distance > tolerance:
            return {
                "success": False,
                "message": "No unambiguous central cylindrical axis was found; provide axis_feature or axis_center_mm.",
                "nearest_axis_distance_mm": distance * 1000.0,
                "axis_tolerance_mm": tolerance * 1000.0,
            }
        select_data = self._select_data(model, mark)
        try:
            if not face.Select4(True, select_data):
                return {"success": False, "message": "Could not select cylindrical axis reference."}
        except Exception as exc:
            return {"success": False, "message": f"Could not select cylindrical axis reference: {exc}"}
        return {
            "success": True,
            "axis": axis,
            "cylinder_params_m": values,
            "center_distance_mm": distance * 1000.0,
            "selection_mark": mark,
            "axis_feature": request.get("axis_feature") or None,
        }

    def _select_reference_plane(self, model: Any, canonical: str, mark: int) -> dict[str, Any]:
        planes = [feature for feature in self._all_features(model) if str(self._com_member(feature, "GetTypeName2", default="")) == "RefPlane"]
        index = {"front": 0, "top": 1, "right": 2}[canonical]
        if len(planes) <= index:
            return {"success": False, "message": f"Default {canonical} reference plane is unavailable."}
        plane = planes[index]
        try:
            if not plane.Select2(True, mark):
                return {"success": False, "message": f"Could not select {canonical} reference plane with mark {mark}."}
        except Exception as exc:
            return {"success": False, "message": f"Could not select mirror plane: {exc}"}
        return {
            "success": True,
            "canonical_plane": canonical,
            "feature_name": str(self._com_member(plane, "Name", default="") or ""),
            "selection_mark": mark,
        }

    def _select_data(self, model: Any, mark: int) -> Any:
        manager = self._com_member(model, "SelectionManager")
        data = self._com_member(manager, "CreateSelectData")
        if data is None:
            raise RuntimeError("SolidWorks SelectionManager.CreateSelectData is unavailable.")
        data.Mark = int(mark)
        return data

    def _find_feature(self, model: Any, name: str) -> Any | None:
        if not name:
            return None
        feature = self._com_member(model, "FeatureByName", name)
        if feature is not None:
            return feature
        requested = str(name).casefold()
        return next(
            (
                item
                for item in self._all_features(model)
                if str(self._com_member(item, "Name", default="") or "").casefold() == requested
            ),
            None,
        )

    @classmethod
    def _model_evidence(cls, model: Any) -> dict[str, Any]:
        body_info = ActiveModelThroughHoleExecutor._body_info(model)
        faces = 0
        edges = 0
        for body in body_info.get("bodies", []):
            faces += len(cls._as_sequence(cls._com_member(body, "GetFaces")))
            edges += len(cls._as_sequence(cls._com_member(body, "GetEdges")))
        return {
            "success": bool(body_info.get("success")),
            "solid_body_count": len(body_info.get("bodies", [])),
            "face_count": faces,
            "edge_count": edges,
            "volume_m3": ActiveModelThroughHoleExecutor._solid_volume(list(body_info.get("bodies", []))),
            "bbox_m": body_info.get("bbox", {}),
        }

    @staticmethod
    def _verify_geometry(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        if not after.get("success") or int(after.get("solid_body_count", 0)) <= 0:
            return {"success": False, "message": "Pattern or mirror produced no solid body."}
        body_count_before = int(before.get("solid_body_count", 0))
        body_count_after = int(after.get("solid_body_count", 0))
        if body_count_after != body_count_before:
            return {
                "success": False,
                "message": "Pattern or mirror changed the solid-body count.",
                "body_count_before": body_count_before,
                "body_count_after": body_count_after,
            }
        volume_before = before.get("volume_m3")
        volume_after = after.get("volume_m3")
        if volume_before is None or volume_after is None:
            return {"success": False, "message": "Could not measure solid volume across the pattern or mirror."}
        volume_before = float(volume_before)
        volume_after = float(volume_after)
        volume_delta = volume_after - volume_before
        tolerance = max(abs(volume_before), abs(volume_after), 1e-12) * 1e-9
        if abs(volume_delta) <= tolerance:
            return {
                "success": False,
                "message": "Pattern or mirror did not change measurable solid geometry.",
                "volume_before_m3": volume_before,
                "volume_after_m3": volume_after,
                "volume_delta_m3": volume_delta,
            }
        return {
            "success": True,
            "body_count_before": body_count_before,
            "body_count_after": body_count_after,
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
        }

    @classmethod
    def _created_feature_evidence(cls, feature: Any) -> dict[str, Any]:
        raw_error = cls._com_member(feature, "GetErrorCode2", default=0)
        try:
            error_code = int(raw_error or 0)
        except Exception:
            error_code = None
        return {
            "name": str(cls._com_member(feature, "Name", default="") or ""),
            "type_name": str(cls._com_member(feature, "GetTypeName2", default="") or ""),
            "error_code": error_code,
        }

    @classmethod
    def _feature_tree(cls, model: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": str(cls._com_member(feature, "Name", default="") or ""),
                "type": str(cls._com_member(feature, "GetTypeName2", default="") or ""),
            }
            for feature in cls._all_features(model)
        ]

    @classmethod
    def _all_features(cls, model: Any) -> list[Any]:
        result: list[Any] = []
        feature = cls._com_member(model, "FirstFeature")
        seen: set[int] = set()
        while feature is not None and id(feature) not in seen and len(result) < 1000:
            seen.add(id(feature))
            result.append(feature)
            feature = cls._com_member(feature, "GetNextFeature")
        return result

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
    def _seed_names(params: dict[str, Any]) -> list[str]:
        value = params.get("seed_features") or params.get("seed_feature") or params.get("target_features") or params.get("target_feature")
        if isinstance(value, str):
            return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @staticmethod
    def _valid_axis_token(value: str) -> bool:
        return value.lower().replace(" ", "") in {"x", "+x", "-x", "y", "+y", "-y", "z", "+z", "-z"}

    @staticmethod
    def _axis(value: str) -> str:
        return value.lower().replace(" ", "").replace("+", "").replace("-", "")

    @staticmethod
    def _axis_sign(value: str) -> int:
        return -1 if value.lower().replace(" ", "").startswith("-") else 1

    @staticmethod
    def _canonical_plane(value: str) -> str | None:
        lowered = value.lower().replace(" ", "")
        aliases = {
            "front": {"front", "frontplane", "前视", "前视基准面", "xy", "z"},
            "top": {"top", "topplane", "上视", "上视基准面", "xz", "y"},
            "right": {"right", "rightplane", "右视", "右视基准面", "yz", "x"},
        }
        return next((name for name, values in aliases.items() if lowered in values), None)

    @staticmethod
    def _name_feature(feature: Any, name: str) -> None:
        try:
            feature.Name = str(name)
        except Exception:
            pass

    @staticmethod
    def _as_sequence(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [item for item in value if item is not None]
        return [value]

    @staticmethod
    def _com_member(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
        return ActiveModelThroughHoleExecutor._com_member(obj, name, *args, default=default)

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["files"] = [str(report_path)]
        return SkillResult(
            success=success,
            message=message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
