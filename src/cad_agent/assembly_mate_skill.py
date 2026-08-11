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


class AssemblyMateSkill:
    """Create or modify one assembly from explicit components and mates."""

    MATE_TYPES = {
        "coincident": 0,
        "concentric": 1,
        "parallel": 3,
        "distance": 5,
        "gear": 10,
    }
    STANDARD_PLANES = {
        "front": ("Front Plane", "\u524d\u89c6\u57fa\u51c6\u9762"),
        "front plane": ("Front Plane", "\u524d\u89c6\u57fa\u51c6\u9762"),
        "top": ("Top Plane", "\u4e0a\u89c6\u57fa\u51c6\u9762"),
        "top plane": ("Top Plane", "\u4e0a\u89c6\u57fa\u51c6\u9762"),
        "right": ("Right Plane", "\u53f3\u89c6\u57fa\u51c6\u9762"),
        "right plane": ("Right Plane", "\u53f3\u89c6\u57fa\u51c6\u9762"),
    }

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == "assembly_mate"]
        if len(features) != 1:
            return SkillResult(False, "assembly_mate requires exactly one explicit assembly request per task.")
        run_dir = self.output_root / f"assembly_mate_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "assembly_mate_report.json"
        try:
            self._ensure_imports()
            from sw_assembly import (
                add_mate5_checked,
                collect_mate_feature_summary,
                find_component_by_name,
                find_largest_cylinder_face,
                get_component_feature_entity,
                get_components,
                resolve_component,
                select_entities_for_mate,
            )
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document

            request = self.normalize_request(features[0].get("params", {}))
            if not request.get("success"):
                return self._result(False, str(request.get("message")), report_path, {
                    "feature_type": "assembly_mate",
                    "invalid_request": request,
                })

            sw, active = connect_solidworks(visible=True)
            if request["mode"] == "new_assembly":
                opened_models = self._preload_components(sw, request["components"])
                model = new_document(sw, "assembly")
                component_map = self._insert_components(model, sw, request["components"])
            else:
                opened_models = []
                model = active
                validation = self._validate_active_assembly(model)
                if not validation.get("success"):
                    return self._result(False, str(validation.get("message")), report_path, validation)
                component_map = self._map_active_components(model, request["components"], find_component_by_name)

            before_summary = collect_mate_feature_summary(model)
            operations: list[dict[str, Any]] = []
            for mate in request["mates"]:
                component_a = component_map[mate["entity_a"]["component"]]
                component_b = component_map[mate["entity_b"]["component"]]
                resolve_component(component_a)
                resolve_component(component_b)
                entity_a = self._resolve_entity(
                    component_a,
                    mate["entity_a"],
                    get_component_feature_entity,
                    find_largest_cylinder_face,
                )
                entity_b = self._resolve_entity(
                    component_b,
                    mate["entity_b"],
                    get_component_feature_entity,
                    find_largest_cylinder_face,
                )
                select_entities_for_mate(model, entity_a, entity_b, mark=1)
                created = add_mate5_checked(
                    model,
                    self.MATE_TYPES[mate["mate_type"]],
                    align=int(mate["align"]),
                    flip=bool(mate["flip"]),
                    distance=self._m(mate["distance_mm"]),
                    gear_num=float(mate["gear_teeth_a"]),
                    gear_den=float(mate["gear_teeth_b"]),
                    lock_rotation=bool(mate["lock_rotation"]),
                    name=mate["name"],
                )
                operations.append({
                    "success": created is not None,
                    "name": mate["name"],
                    "type": "assembly_mate",
                    "mate_type": mate["mate_type"],
                    "request": mate,
                })
                if created is None:
                    return self._result(False, f"SolidWorks failed to create mate: {mate['name']}", report_path, {
                        "operations": operations,
                    })

            model.ForceRebuild3(False)
            after_summary = collect_mate_feature_summary(model)
            created_names = {str(item.get("name")) for item in after_summary}
            missing_mates = [item["name"] for item in request["mates"] if item["name"] not in created_names]
            if missing_mates:
                return self._result(False, f"Mate features missing after rebuild: {missing_mates}", report_path, {
                    "operations": operations,
                    "mate_summary": after_summary,
                })

            files: list[str] = []
            reopen_validation: dict[str, Any] = {"success": True, "skipped": True}
            if request["mode"] == "new_assembly":
                requested_path = str(plan.get("execution_assembly_path") or "").strip()
                assembly_path = Path(requested_path) if requested_path else run_dir / "assembly_task.SLDASM"
                assembly_path.parent.mkdir(parents=True, exist_ok=True)
                if not save_document(model, str(assembly_path)):
                    return self._result(False, f"SolidWorks failed to save assembly: {assembly_path}", report_path, {
                        "operations": operations,
                    })
                if not assembly_path.is_file() or assembly_path.stat().st_size <= 0:
                    return self._result(False, f"Saved assembly is missing or empty: {assembly_path}", report_path, {
                        "operations": operations,
                    })
                files.append(str(assembly_path))
                reopen_validation = self._verify_reopen(
                    sw,
                    model,
                    assembly_path,
                    len(request["components"]),
                    [item["name"] for item in request["mates"]],
                    get_components,
                    collect_mate_feature_summary,
                    get_com_member,
                )
                reopened_model = reopen_validation.pop("model", None)
                if not reopen_validation.get("success"):
                    return self._result(False, "Saved assembly could not be reopened and verified.", report_path, {
                        "operations": operations,
                        "reopen_validation": reopen_validation,
                        "files": files,
                    })
                model = reopened_model

            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": request["mode"],
                "feature_type": "assembly_mate",
                "feature_created": True,
                "components_added": len(request["components"]) if request["mode"] == "new_assembly" else 0,
                "component_ids": list(component_map),
                "components": request["components"],
                "mates_created": len(operations),
                "operations": operations,
                "mate_summary_before": before_summary,
                "mate_summary_after": after_summary,
                "reopen_validation": reopen_validation,
                "side_effects": {
                    "modifies_active_doc": request["mode"] == "active_assembly",
                    "creates_new_doc": request["mode"] == "new_assembly",
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": files,
            }
            return self._result(True, "SolidWorks assembly mates created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"assembly_mate failed: {exc}", report_path, {
                "feature_type": "assembly_mate",
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any]) -> dict[str, Any]:
        raw_components = params.get("components") or []
        mode = str(params.get("mode") or ("new_assembly" if raw_components and any(
            isinstance(item, dict) and item.get("path") for item in raw_components
        ) else "active_assembly")).strip().lower()
        if mode not in {"new_assembly", "active_assembly"}:
            return {"success": False, "message": "assembly_mate mode must be new_assembly or active_assembly."}
        if not isinstance(raw_components, list) or len(raw_components) < 2:
            return {"success": False, "message": "assembly_mate requires at least two explicit components."}
        components: list[dict[str, Any]] = []
        component_ids: set[str] = set()
        for index, raw in enumerate(raw_components):
            normalized = cls._normalize_component(raw, index, mode)
            if not normalized.get("success"):
                return normalized
            if normalized["id"] in component_ids:
                return {"success": False, "message": f"Duplicate assembly component id: {normalized['id']}"}
            component_ids.add(normalized["id"])
            components.append(normalized)

        raw_mates = params.get("mates")
        if raw_mates is None and params.get("mate_type"):
            raw_mates = [params]
        if not isinstance(raw_mates, list) or not raw_mates:
            return {"success": False, "message": "assembly_mate requires at least one explicit mate."}
        mates: list[dict[str, Any]] = []
        mate_names: set[str] = set()
        for index, raw in enumerate(raw_mates):
            normalized = cls._normalize_mate(raw, index, component_ids)
            if not normalized.get("success"):
                return normalized
            if normalized["name"] in mate_names:
                return {"success": False, "message": f"Duplicate mate name: {normalized['name']}"}
            mate_names.add(normalized["name"])
            mates.append(normalized)
        return {"success": True, "mode": mode, "components": components, "mates": mates}

    @classmethod
    def _normalize_component(cls, raw: Any, index: int, mode: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": f"assembly component {index + 1} must be an object."}
        component_id = str(raw.get("id") or raw.get("name") or "").strip()
        if not component_id or any(char in component_id for char in "\r\n/\\"):
            return {"success": False, "message": f"assembly component {index + 1} requires a stable id."}
        path_value = str(raw.get("path") or raw.get("file_path") or "").strip()
        if mode == "new_assembly":
            path = Path(path_value).expanduser()
            if not path.is_absolute() or not path.is_file() or path.stat().st_size <= 0:
                return {"success": False, "message": f"assembly component file is missing or empty: {path_value}"}
            if path.suffix.lower() not in {".sldprt", ".sldasm"}:
                return {"success": False, "message": f"assembly component must be SLDPRT or SLDASM: {path}"}
            path_value = str(path.resolve())
        position = raw.get("position_mm") or raw.get("position") or [0.0, 0.0, 0.0]
        if not isinstance(position, (list, tuple)) or len(position) < 3:
            return {"success": False, "message": f"assembly component {component_id} position_mm must contain x, y, z."}
        try:
            position_mm = [float(position[0]), float(position[1]), float(position[2])]
        except (TypeError, ValueError):
            return {"success": False, "message": f"assembly component {component_id} has invalid position_mm."}
        rotation_z = cls._number(raw, "rotation_z_deg", "rotation_deg") or 0.0
        if not math.isfinite(rotation_z) or abs(rotation_z) > 3600.0:
            return {"success": False, "message": f"assembly component {component_id} has invalid rotation_z_deg."}
        return {
            "success": True,
            "id": component_id,
            "path": path_value,
            "component_name": str(raw.get("component_name") or component_id).strip(),
            "configuration": str(raw.get("configuration") or "").strip(),
            "position_mm": position_mm,
            "rotation_z_deg": float(rotation_z),
        }

    @classmethod
    def _normalize_mate(cls, raw: Any, index: int, component_ids: set[str]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": f"assembly mate {index + 1} must be an object."}
        mate_type = str(raw.get("mate_type") or raw.get("type") or "").strip().lower()
        if mate_type not in cls.MATE_TYPES:
            return {"success": False, "message": f"assembly mate {index + 1} has unsupported mate_type: {mate_type}"}
        entities = raw.get("entities") if isinstance(raw.get("entities"), list) else []
        entity_a_raw = raw.get("entity_a") or (entities[0] if len(entities) > 0 else None)
        entity_b_raw = raw.get("entity_b") or (entities[1] if len(entities) > 1 else None)
        entity_a = cls._normalize_entity(entity_a_raw, component_ids, f"mate {index + 1} entity_a")
        entity_b = cls._normalize_entity(entity_b_raw, component_ids, f"mate {index + 1} entity_b")
        if not entity_a.get("success"):
            return entity_a
        if not entity_b.get("success"):
            return entity_b
        if entity_a["component"] == entity_b["component"]:
            return {"success": False, "message": "A mate must reference two different components."}
        if mate_type in {"concentric", "gear"} and (
            entity_a["selector_type"] != "cylinder" or entity_b["selector_type"] != "cylinder"
        ):
            return {"success": False, "message": f"{mate_type} mate requires two cylinder selectors."}
        if mate_type == "coincident":
            selector_pair = {entity_a["selector_type"], entity_b["selector_type"]}
            if not (
                selector_pair == {"reference_plane"}
                or selector_pair == {"reference_plane", "reference_axis"}
            ):
                return {
                    "success": False,
                    "message": "coincident mate requires two reference planes or one reference plane and one reference axis.",
                }
        if mate_type == "parallel" and (
            entity_a["selector_type"] != "reference_plane" or entity_b["selector_type"] != "reference_plane"
        ):
            return {"success": False, "message": "parallel mate currently requires two explicit reference planes."}
        if mate_type == "distance" and not (
            entity_a["selector_type"] == entity_b["selector_type"]
            and entity_a["selector_type"] in {"reference_plane", "reference_axis"}
        ):
            return {"success": False, "message": "distance mate requires two reference planes or two reference axes."}
        distance = cls._number(raw, "distance_mm", "distance") or 0.0
        teeth_a = cls._number(raw, "gear_teeth_a", "teeth_a") or 0.0
        teeth_b = cls._number(raw, "gear_teeth_b", "teeth_b") or 0.0
        if mate_type == "distance" and distance < 0:
            return {"success": False, "message": "distance mate distance_mm cannot be negative."}
        if mate_type == "gear" and (teeth_a <= 0 or teeth_b <= 0):
            return {"success": False, "message": "gear mate requires positive gear_teeth_a and gear_teeth_b."}
        return {
            "success": True,
            "name": str(raw.get("name") or f"Mate{index + 1}_{mate_type}").strip(),
            "mate_type": mate_type,
            "entity_a": entity_a,
            "entity_b": entity_b,
            "align": int(raw.get("align", 0) or 0),
            "flip": bool(raw.get("flip", False)),
            "distance_mm": float(distance),
            "lock_rotation": bool(raw.get("lock_rotation", False)),
            "gear_teeth_a": float(teeth_a),
            "gear_teeth_b": float(teeth_b),
        }

    @classmethod
    def _normalize_entity(cls, raw: Any, component_ids: set[str], label: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": f"{label} must be an object."}
        component = str(raw.get("component") or raw.get("component_id") or "").strip()
        if component not in component_ids:
            return {"success": False, "message": f"{label} references an unknown component: {component}"}
        selector = raw.get("selector") if isinstance(raw.get("selector"), dict) else raw
        selector_type = str(selector.get("selector_type") or selector.get("type") or "reference_plane").strip().lower()
        if selector_type == "reference_plane":
            plane = str(selector.get("name") or selector.get("plane") or "").strip()
            aliases = cls.STANDARD_PLANES.get(plane.lower())
            if aliases is None:
                return {"success": False, "message": f"{label} reference plane must be front, top, or right."}
            return {
                "success": True,
                "component": component,
                "selector_type": selector_type,
                "plane": plane.lower(),
                "aliases": list(aliases),
            }
        if selector_type == "reference_axis":
            name = str(selector.get("name") or selector.get("axis") or "").strip()
            if not name:
                return {"success": False, "message": f"{label} reference_axis requires a feature name."}
            return {
                "success": True,
                "component": component,
                "selector_type": selector_type,
                "name": name,
                "aliases": [name],
            }
        if selector_type == "cylinder":
            radius = cls._number(selector, "radius_mm", "radius")
            tolerance = cls._number(selector, "tolerance_mm", "tolerance")
            tolerance = 0.05 if tolerance is None else tolerance
            if radius is None or radius <= 0 or tolerance <= 0:
                return {"success": False, "message": f"{label} cylinder requires positive radius_mm and tolerance_mm."}
            return {
                "success": True,
                "component": component,
                "selector_type": selector_type,
                "radius_mm": float(radius),
                "tolerance_mm": float(tolerance),
            }
        return {"success": False, "message": f"{label} selector_type must be reference_plane, reference_axis, or cylinder."}

    @staticmethod
    def _preload_components(sw: Any, components: list[dict[str, Any]]) -> list[Any]:
        opened: list[Any] = []
        for component in components:
            doc_type = 2 if Path(component["path"]).suffix.lower() == ".sldasm" else 1
            model = sw.OpenDoc(component["path"], doc_type)
            if model is None:
                raise RuntimeError(f"Could not preload assembly component: {component['path']}")
            opened.append(model)
        return opened

    @classmethod
    def _insert_components(cls, model: Any, sw: Any, components: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in components:
            x, y, z = (cls._m(value) for value in item["position_mm"])
            component = model.AddComponent5(
                item["path"], 0, "", False, item["configuration"], x, y, z
            )
            if component is None:
                raise RuntimeError(f"SolidWorks failed to add component: {item['path']}")
            rotation_z = math.radians(float(item.get("rotation_z_deg", 0.0)))
            if abs(rotation_z) > 1e-12 and not cls._apply_component_transform_z(component, x, y, z, rotation_z):
                raise RuntimeError(f"SolidWorks failed to rotate component about GearAxis: {item['id']}")
            result[item["id"]] = component
        return result

    @staticmethod
    def _apply_component_transform_z(component: Any, x: float, y: float, z: float, angle_rad: float) -> bool:
        transform = ActiveModelThroughHoleExecutor._com_member(component, "Transform2")
        if transform is None:
            return False
        c = math.cos(float(angle_rad))
        s = math.sin(float(angle_rad))
        transform.ArrayData = (
            c, -s, 0.0,
            s, c, 0.0,
            0.0, 0.0, 1.0,
            float(x), float(y), float(z),
            1.0,
            0.0, 0.0, 0.0,
        )
        try:
            if component.SetTransformAndSolve2(transform):
                return True
        except Exception:
            pass
        try:
            component.Transform2 = transform
            return True
        except Exception:
            return False

    @staticmethod
    def _map_active_components(model: Any, components: list[dict[str, Any]], finder: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in components:
            component = finder(model, item["component_name"], top_level_only=True, case_sensitive=False)
            if component is None:
                raise RuntimeError(f"Active assembly component not found: {item['component_name']}")
            result[item["id"]] = component
        return result

    @staticmethod
    def _resolve_entity(component: Any, selector: dict[str, Any], feature_entity: Any, cylinder_face: Any) -> Any:
        if selector["selector_type"] in {"reference_plane", "reference_axis"}:
            return feature_entity(component, selector["aliases"], resolve=True)
        radius = selector["radius_mm"] / 1000.0
        tolerance = selector["tolerance_mm"] / 1000.0
        return cylinder_face(component, radius - tolerance, radius + tolerance, resolve=True)

    @staticmethod
    def _validate_active_assembly(model: Any) -> dict[str, Any]:
        if model is None:
            return {"success": False, "message": "ActiveDoc does not exist."}
        title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
        doc_type = int(ActiveModelThroughHoleExecutor._com_member(model, "GetType", default=0) or 0)
        if doc_type != 2:
            return {"success": False, "message": f"ActiveDoc is not an Assembly: {title}", "active_doc": title}
        return {"success": True, "active_doc": title}

    @staticmethod
    def _verify_reopen(
        sw: Any,
        model: Any,
        path: Path,
        expected_components: int,
        expected_mates: list[str],
        get_components: Any,
        mate_summary: Any,
        get_member: Any,
    ) -> dict[str, Any]:
        title = str(get_member(model, "GetTitle") or "")
        if title:
            sw.CloseDoc(title)
        reopened = sw.OpenDoc(str(path), 2)
        if reopened is None:
            return {"success": False, "path": str(path), "message": "SolidWorks OpenDoc returned no Assembly."}
        component_count = len(get_components(reopened, top_level_only=True))
        summary = mate_summary(reopened)
        mate_names = {str(item.get("name")) for item in summary}
        success = component_count == expected_components and all(name in mate_names for name in expected_mates)
        return {
            "success": success,
            "path": str(path),
            "active_path": str(get_member(reopened, "GetPathName") or ""),
            "component_count": component_count,
            "expected_component_count": expected_components,
            "mate_names": sorted(mate_names),
            "expected_mates": expected_mates,
            "model": reopened,
        }

    @staticmethod
    def _number(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            if values.get(key) not in (None, ""):
                try:
                    return float(values[key])
                except (TypeError, ValueError):
                    return None
        return None

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
        payload["files"] = list(dict.fromkeys(
            [str(item) for item in payload.get("files", [])] + [str(report_path)]
        ))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success,
            message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
