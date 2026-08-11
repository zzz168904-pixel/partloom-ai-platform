from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .models import SkillResult


class ProductionFilletSkill:
    """Apply a requested fillet to the active production Part only.

    This is deliberately separate from the legacy CNC demonstration template.
    It never creates a new document, loads a template, exports files, or adds
    chamfers/CNC features that were not requested.
    """

    key = "fillet"
    name = "SolidWorks Production Fillet"

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == "fillet"]
        if not features:
            return SkillResult(False, "No fillet feature found in the production plan.")
        if len(features) > 1:
            return self._run_batch_plan(plan, features)
        feature = features[0]

        params = feature.get("params", {})
        radius_mm = float(params.get("radius", 0) or 0)
        if radius_mm <= 0:
            return SkillResult(False, "Fillet radius must be greater than 0 mm.")
        edge_selector = self._edge_selector(params)

        run_dir = self.output_root / f"production_fillet_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "production_fillet_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, mm

            sw, model = connect_solidworks(visible=True)
            if model is None:
                return self._result(False, "ActiveDoc does not exist.", report_path, {})
            if int(get_com_member(model, "GetType")) != 1:
                return self._result(False, "ActiveDoc is not a SolidWorks Part.", report_path, {})

            body_info = ActiveModelThroughHoleExecutor._body_info(model)
            if not body_info.get("success"):
                return self._result(False, str(body_info.get("message")), report_path, body_info)

            selected_target_feature = None
            reopened_for_selection = False
            isolated_execution: dict[str, Any] | None = None
            created_in_isolated_process = False
            if edge_selector == "all_feature_edges":
                selected_target_feature = self._target_feature_id(feature, params)
                if not selected_target_feature:
                    return self._result(
                        False,
                        "all_feature_edges requires target_reference.feature_id.",
                        report_path,
                        {"edge_selector": edge_selector},
                    )
                selected = 0
                selection_attempts = 1
                if self._select_feature_by_name(model, selected_target_feature):
                    selected = 1
                if selected != 1:
                    model_path = Path(str(plan.get("execution_model_path") or ""))
                    isolated_execution = self._run_isolated_feature_fillet(
                        sw,
                        model,
                        model_path,
                        selected_target_feature,
                        radius_mm,
                        str(feature.get("name") or "EdgeFillet"),
                    )
                    if not isolated_execution.get("success"):
                        return self._result(
                            False,
                            f"Target feature {selected_target_feature!r} was not selectable in either COM session.",
                            report_path,
                            {
                                "edge_selector": edge_selector,
                                "target_feature_id": selected_target_feature,
                                "selection_attempts": selection_attempts,
                                "reopened_for_selection": reopened_for_selection,
                                "isolated_execution": isolated_execution,
                            },
                        )
                    model = ActiveModelThroughHoleExecutor._com_member(
                        sw,
                        "ActiveDoc",
                        default=None,
                    )
                    if model is None:
                        model = self._reopen_task_model(sw, None, model_path)
                    if model is None:
                        return self._result(
                            False,
                            "The task model could not be reopened after isolated fillet execution.",
                            report_path,
                            {"isolated_execution": isolated_execution},
                        )
                    time.sleep(1.0)
                    selected = 1
                    created_in_isolated_process = True
            elif edge_selector == "axial_bearing_boundary_end_edges":
                selected = self._select_axial_bearing_boundary_end_edges(
                    model,
                    body_info["bodies"],
                    body_info["bbox"],
                    axis=str(params.get("axis") or "x"),
                )
                if selected != 4:
                    return self._result(
                        False,
                        f"Expected 4 axial bearing-boundary end edges; selected {selected}.",
                        report_path,
                        {
                            "edge_selector": edge_selector,
                            "selected_edges": selected,
                            "axis": str(params.get("axis") or "x"),
                            "bbox_m": body_info["bbox"],
                        },
                    )
            elif edge_selector == "explicit_edge_signatures":
                edge_signatures = list(params.get("edge_signatures") or [])
                edge_tolerance_mm = float(params.get("edge_tolerance_mm", 0.03) or 0.03)
                selected = self._select_explicit_edge_signatures(
                    model,
                    body_info["bodies"],
                    edge_signatures,
                    tolerance_mm=edge_tolerance_mm,
                )
                if selected != len(edge_signatures):
                    return self._result(
                        False,
                        f"Expected {len(edge_signatures)} explicit edge signatures; selected {selected}.",
                        report_path,
                        {
                            "edge_selector": edge_selector,
                            "selected_edges": selected,
                            "expected_edge_signatures": len(edge_signatures),
                            "edge_tolerance_mm": edge_tolerance_mm,
                        },
                    )
            else:
                selected = self._select_outer_vertical_edges(model, body_info["bodies"], body_info["bbox"])
                if selected != 4:
                    return self._result(
                        False,
                        f"Expected 4 outer vertical edges for the requested corner fillet; selected {selected}.",
                        report_path,
                        {"edge_selector": edge_selector, "selected_edges": selected, "bbox_m": body_info["bbox"]},
                    )

            created = None
            if not created_in_isolated_process:
                created = model.FeatureManager.FeatureFillet(195, mm(radius_mm), 0, 0, None, None, None)
                if created is None:
                    return self._result(False, "SolidWorks FeatureFillet returned no feature.", report_path, {"selected_edges": selected})
                try:
                    created.Name = str(feature.get("name") or "OuterFillet")
                except Exception:
                    pass
            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()

            save_path = plan.get("execution_model_path")
            save_after = bool(plan.get("execution_save_after_fillet", False))
            saved_path = Path(str(save_path)) if save_path else None
            if save_after:
                saved_path = ActiveModelThroughHoleExecutor._save_model(model, run_dir, saved_path)
                if not saved_path or not saved_path.is_file() or saved_path.stat().st_size <= 0:
                    return self._result(False, "Failed to save active Part after fillet.", report_path, {"selected_edges": selected})
            data = {
                "mode": "active_model",
                "radius_mm": radius_mm,
                "edge_selector": edge_selector,
                "target_feature_id": selected_target_feature,
                "selection_attempts": selection_attempts if edge_selector == "all_feature_edges" else 1,
                "reopened_for_selection": reopened_for_selection,
                "created_in_isolated_process": created_in_isolated_process,
                "isolated_execution": isolated_execution,
                "selected_edges": selected,
                "selected_outer_vertical_edges": selected,
                "expected_edge_signatures": (
                    len(list(params.get("edge_signatures") or []))
                    if edge_selector == "explicit_edge_signatures"
                    else None
                ),
                "feature_created": True,
                "saved_path": str(saved_path) if saved_path else None,
                "saved_by_this_skill": save_after,
                "side_effects": {
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                    "creates_chamfer": False,
                    "creates_cnc_features": False,
                },
            }
            return self._result(True, "Production fillet created on active Part.", report_path, data, saved_path)
        except Exception as exc:
            return self._result(False, f"production fillet failed: {exc}", report_path, {"error": repr(exc)})

    def _run_batch_plan(
        self,
        plan: dict[str, Any],
        features: list[dict[str, Any]],
    ) -> SkillResult:
        """Apply contiguous CAD-IR fillets in order and preserve each gate result."""
        run_dir = self.output_root / f"production_fillet_batch_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "production_fillet_batch_report.json"
        operations: list[dict[str, Any]] = []
        child_reports: list[str] = []
        for feature in features:
            child_plan = {
                **plan,
                "features": [feature],
            }
            result = self._run_single_plan_dispatch(child_plan)
            operation = dict(result.data or {})
            operation["requested_feature_id"] = str(
                feature.get("id") or feature.get("name") or ""
            )
            operation["success"] = bool(result.success)
            operations.append(operation)
            if result.path:
                child_reports.append(str(result.path))
            if not result.success:
                return self._result(
                    False,
                    f"Production fillet batch stopped at {operation['requested_feature_id']!r}: {result.message}",
                    report_path,
                    {
                        "mode": "active_model",
                        "feature_type": "fillet",
                        "feature_created": False,
                        "features_created": sum(
                            1 for item in operations if item.get("success")
                        ),
                        "features_requested": len(features),
                        "operations": operations,
                        "child_reports": child_reports,
                    },
                )

        selected_edges = sum(int(item.get("selected_edges", 0) or 0) for item in operations)
        expected_edges = sum(
            int(item.get("expected_edge_signatures", item.get("selected_edges", 0)) or 0)
            for item in operations
        )
        return self._result(
            True,
            f"Created and verified {len(operations)} ordered production fillet features.",
            report_path,
            {
                "mode": "active_model",
                "feature_type": "fillet",
                "feature_created": True,
                "features_created": len(operations),
                "features_requested": len(features),
                "edge_selector": "ordered_batch",
                "selected_edges": selected_edges,
                "expected_edge_signatures": expected_edges,
                "operations": operations,
                "child_reports": child_reports,
                "saved_by_this_skill": any(
                    bool(item.get("saved_by_this_skill")) for item in operations
                ),
                "side_effects": {
                    "modifies_active_doc": True,
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                    "creates_chamfer": False,
                    "creates_cnc_features": False,
                },
            },
        )

    def _run_single_plan_dispatch(self, plan: dict[str, Any]) -> SkillResult:
        """Dispatch hook kept separate so ordered batches are unit-testable."""
        return self.run_plan(plan)

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
    def _edge_selector(params: dict[str, Any]) -> str:
        value = str(
            params.get("edge_selector")
            or params.get("target")
            or params.get("targets")
            or "outer_vertical_edges"
        ).strip().lower()
        aliases = {
            "outer_edges": "outer_vertical_edges",
            "four_outer_corners": "outer_vertical_edges",
            "feature_edges": "all_feature_edges",
            "all_edges_of_feature": "all_feature_edges",
            "bearing_end_edges": "axial_bearing_boundary_end_edges",
            "axial_ring_end_edges": "axial_bearing_boundary_end_edges",
            "axial_outer_ring_end_edges": "axial_bearing_boundary_end_edges",
        }
        return aliases.get(value, value)

    @staticmethod
    def _target_feature_id(feature: dict[str, Any], params: dict[str, Any]) -> str:
        target_reference = feature.get("target_reference")
        if isinstance(target_reference, dict):
            value = target_reference.get("feature_id")
            if value not in (None, ""):
                return str(value).strip()
        return str(params.get("target_feature_id") or params.get("feature_id") or "").strip()

    @staticmethod
    def _select_feature_by_name(model: Any, name: str) -> bool:
        model.ClearSelection2(True)
        requested = str(name).strip().casefold()
        direct = ActiveModelThroughHoleExecutor._com_member(
            model,
            "FeatureByName",
            name,
            default=None,
        )
        if direct is not None and bool(
            ActiveModelThroughHoleExecutor._com_member(
                direct,
                "Select2",
                False,
                0,
                default=False,
            )
        ):
            return True

        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        seen: set[tuple[str, str]] = set()
        while feature is not None and len(seen) < 10000:
            feature_name = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "Name", default="") or ""
            ).strip()
            feature_type = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""
            ).strip()
            marker = (feature_name.casefold(), feature_type.casefold())
            if marker in seen:
                break
            seen.add(marker)
            if feature_name.casefold() == requested:
                return bool(
                    ActiveModelThroughHoleExecutor._com_member(
                        feature,
                        "Select2",
                        False,
                        0,
                        default=False,
                    )
                )
            next_feature = ActiveModelThroughHoleExecutor._com_member(
                feature,
                "IGetNextFeature",
                default=None,
            )
            if next_feature is None:
                next_feature = ActiveModelThroughHoleExecutor._com_member(
                    feature,
                    "GetNextFeature",
                    default=None,
                )
            feature = next_feature
        return False

    @staticmethod
    def _reopen_task_model(sw: Any, model: Any | None, path: Path) -> Any | None:
        if not path.is_file():
            return None
        if model is not None:
            ActiveModelThroughHoleExecutor._com_member(model, "Save", default=False)
            title = str(
                ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or ""
            ).strip()
            if title:
                ActiveModelThroughHoleExecutor._com_member(sw, "CloseDoc", title, default=None)
        opened = ActiveModelThroughHoleExecutor._com_member(
            sw,
            "OpenDoc6",
            str(path),
            1,
            1,
            "",
            0,
            0,
            default=None,
        )
        if isinstance(opened, tuple):
            opened = opened[0] if opened else None
        if opened is None:
            return None
        reopened_title = str(
            ActiveModelThroughHoleExecutor._com_member(opened, "GetTitle", default="") or ""
        ).strip()
        if not reopened_title:
            return opened
        activated = ActiveModelThroughHoleExecutor._com_member(
            sw,
            "ActivateDoc3",
            reopened_title,
            False,
            1,
            0,
            default=None,
        )
        if isinstance(activated, tuple):
            activated = activated[0] if activated else None
        active_document = ActiveModelThroughHoleExecutor._com_member(
            sw,
            "ActiveDoc",
            default=None,
        )
        if active_document is not None:
            return active_document
        return activated if activated is not None else opened

    @staticmethod
    def _run_isolated_feature_fillet(
        sw: Any,
        model: Any,
        path: Path,
        target_feature: str,
        radius_mm: float,
        output_name: str,
    ) -> dict[str, Any]:
        if not path.is_file():
            return {"success": False, "error": f"Task model does not exist: {path}"}
        ActiveModelThroughHoleExecutor._com_member(model, "Save", default=False)
        command = [
            sys.executable,
            "-m",
            "src.cad_agent.isolated_feature_fillet",
            "--model",
            str(path),
            "--feature",
            target_feature,
            "--radius-mm",
            str(radius_mm),
            "--output-name",
            output_name,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )
        except Exception as exc:
            return {"success": False, "error": repr(exc), "command": command}
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        payload: dict[str, Any]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            payload = {}
        payload.update({
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
        })
        payload["success"] = bool(payload.get("success") and completed.returncode == 0)
        return payload

    @staticmethod
    def _select_outer_vertical_edges(model: Any, bodies: list[Any], bbox: dict[str, float]) -> int:
        tolerance = max(1e-6, min(bbox["length"], bbox["width"]) * 0.02)
        model.ClearSelection2(True)
        count = 0
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                points = ProductionFilletSkill._edge_points(edge)
                if points is None:
                    continue
                start, end = points
                if abs(end[2] - start[2]) < bbox["thickness"] * 0.8:
                    continue
                x = (start[0] + end[0]) / 2.0
                y = (start[1] + end[1]) / 2.0
                on_x_boundary = min(abs(x - bbox["xmin"]), abs(x - bbox["xmax"])) <= tolerance
                on_y_boundary = min(abs(y - bbox["ymin"]), abs(y - bbox["ymax"])) <= tolerance
                if not (on_x_boundary and on_y_boundary):
                    continue
                try:
                    if edge.Select2(count > 0, 0):
                        count += 1
                except Exception:
                    continue
        return count

    @staticmethod
    def _select_axial_bearing_boundary_end_edges(
        model: Any,
        bodies: list[Any],
        bbox: dict[str, float],
        *,
        axis: str = "x",
    ) -> int:
        """Select the inner-bore and outer-diameter edges at both axial ends.

        The axial-boundary test excludes groove junctions. Selecting the
        smallest and largest end-circle radii reproduces the standard bearing
        edge break without rounding the intermediate race shoulders.
        """
        axis_name = str(axis or "x").strip().lower()
        axis_aliases = {"horizontal": "x", "vertical": "y"}
        axis_name = axis_aliases.get(axis_name, axis_name)
        axis_index = {"x": 0, "y": 1, "z": 2}.get(axis_name)
        if axis_index is None:
            return 0

        coordinate_names = ("x", "y", "z")
        axial_min = float(bbox[f"{axis_name}min"])
        axial_max = float(bbox[f"{axis_name}max"])
        axial_span = axial_max - axial_min
        radial_indices = tuple(index for index in range(3) if index != axis_index)
        centers = [
            (float(bbox[f"{name}min"]) + float(bbox[f"{name}max"])) / 2.0
            for name in coordinate_names
        ]
        radial_extent = max(
            (float(bbox[f"{coordinate_names[index]}max"]) - float(bbox[f"{coordinate_names[index]}min"])) / 2.0
            for index in radial_indices
        )
        if axial_span <= 0 or radial_extent <= 0:
            return 0

        axial_tolerance = max(1e-7, axial_span * 0.005)
        center_tolerance = max(1e-7, radial_extent * 0.005)
        model.ClearSelection2(True)
        candidates: list[tuple[Any, tuple[float, ...]]] = []
        seen: set[tuple[float, ...]] = set()
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                curve = ActiveModelThroughHoleExecutor._com_member(edge, "GetCurve", default=None)
                circle = ActiveModelThroughHoleExecutor._com_member(curve, "CircleParams", default=None)
                if not isinstance(circle, (tuple, list)) or len(circle) < 7:
                    continue
                try:
                    values = tuple(float(value) for value in circle[:7])
                except (TypeError, ValueError):
                    continue
                center = values[:3]
                normal = values[3:6]
                radius = abs(values[6])
                if abs(normal[axis_index]) < 0.985:
                    continue
                if any(abs(center[index] - centers[index]) > center_tolerance for index in radial_indices):
                    continue
                if min(abs(center[axis_index] - axial_min), abs(center[axis_index] - axial_max)) > axial_tolerance:
                    continue
                if radius <= 0 or radius > radial_extent + center_tolerance:
                    continue
                signature = tuple(round(value, 10) for value in values)
                if signature in seen:
                    continue
                seen.add(signature)
                candidates.append((edge, values))

        if not candidates:
            return 0
        radii = [abs(values[6]) for _, values in candidates]
        minimum_radius = min(radii)
        maximum_radius = max(radii)
        radius_tolerance = max(1e-7, radial_extent * 0.005)
        count = 0
        for edge, values in candidates:
            radius = abs(values[6])
            if min(abs(radius - minimum_radius), abs(radius - maximum_radius)) > radius_tolerance:
                continue
            if bool(
                ActiveModelThroughHoleExecutor._com_member(
                    edge,
                    "Select2",
                    count > 0,
                    0,
                    default=False,
                )
            ):
                count += 1
        return count

    @staticmethod
    def _select_explicit_edge_signatures(
        model: Any,
        bodies: list[Any],
        signatures: list[dict[str, Any]],
        *,
        tolerance_mm: float,
    ) -> int:
        """Select exact topology by unordered endpoint and optional curve data."""
        if not signatures or tolerance_mm <= 0:
            return 0
        tolerance_m = tolerance_mm / 1000.0
        candidates: list[
            tuple[
                Any,
                tuple[tuple[float, float, float], tuple[float, float, float]] | None,
                tuple[float, float, float, float, float, float, float] | None,
            ]
        ] = []
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                points = ProductionFilletSkill._edge_points(edge)
                circle = ProductionFilletSkill._edge_circle(edge)
                if points is not None or circle is not None:
                    candidates.append((edge, points, circle))

        model.ClearSelection2(True)
        used: set[int] = set()
        selected = 0
        for signature in signatures:
            requested_kind = str(signature.get("curve_type") or "").strip().lower()
            closed_circle = requested_kind == "circle" and not (
                signature.get("start_mm") is not None or signature.get("end_mm") is not None
            )
            if closed_circle:
                try:
                    expected_center = tuple(float(value) / 1000.0 for value in signature["center_mm"][:3])
                    expected_radius = float(signature["radius_mm"]) / 1000.0
                    expected_axis = tuple(float(value) for value in signature.get("axis", [])[:3])
                except (KeyError, TypeError, ValueError):
                    return selected
                if len(expected_center) != 3 or expected_radius <= 0:
                    return selected
                if expected_axis and len(expected_axis) != 3:
                    return selected
                match: tuple[int, Any] | None = None
                for index, (edge, points, circle) in enumerate(candidates):
                    if index in used or points is not None or circle is None:
                        continue
                    actual_center = circle[:3]
                    actual_axis = circle[3:6]
                    actual_radius = abs(circle[6])
                    if ProductionFilletSkill._point_distance(actual_center, expected_center) > tolerance_m:
                        continue
                    if abs(actual_radius - expected_radius) > tolerance_m:
                        continue
                    if expected_axis and not ProductionFilletSkill._parallel_axes(actual_axis, expected_axis):
                        continue
                    match = (index, edge)
                    break
                if match is None:
                    return selected
                index, edge = match
                if not bool(
                    ActiveModelThroughHoleExecutor._com_member(
                        edge,
                        "Select2",
                        selected > 0,
                        0,
                        default=False,
                    )
                ):
                    return selected
                used.add(index)
                selected += 1
                continue

            try:
                expected_start = tuple(float(value) / 1000.0 for value in signature["start_mm"][:3])
                expected_end = tuple(float(value) / 1000.0 for value in signature["end_mm"][:3])
            except (KeyError, TypeError, ValueError):
                return selected
            if len(expected_start) != 3 or len(expected_end) != 3:
                return selected

            match: tuple[int, Any] | None = None
            for index, (edge, points, circle) in enumerate(candidates):
                if index in used or points is None:
                    continue
                actual_start, actual_end = points
                direct = (
                    ProductionFilletSkill._point_distance(actual_start, expected_start) <= tolerance_m
                    and ProductionFilletSkill._point_distance(actual_end, expected_end) <= tolerance_m
                )
                reverse = (
                    ProductionFilletSkill._point_distance(actual_start, expected_end) <= tolerance_m
                    and ProductionFilletSkill._point_distance(actual_end, expected_start) <= tolerance_m
                )
                if not (direct or reverse):
                    continue
                is_circle = circle is not None
                if requested_kind in {"circle", "arc"} and not is_circle:
                    continue
                if requested_kind == "line" and is_circle:
                    continue
                requested_radius = signature.get("radius_mm")
                if requested_radius not in (None, ""):
                    if not is_circle:
                        continue
                    try:
                        radius_error = abs(abs(circle[6]) - float(requested_radius) / 1000.0)
                    except (TypeError, ValueError):
                        continue
                    if radius_error > tolerance_m:
                        continue
                match = (index, edge)
                break
            if match is None:
                return selected
            index, edge = match
            if not bool(
                ActiveModelThroughHoleExecutor._com_member(
                    edge,
                    "Select2",
                    selected > 0,
                    0,
                    default=False,
                )
            ):
                return selected
            used.add(index)
            selected += 1
        return selected

    @staticmethod
    def _edge_circle(edge: Any) -> tuple[float, float, float, float, float, float, float] | None:
        curve = ActiveModelThroughHoleExecutor._com_member(edge, "GetCurve", default=None)
        values = ActiveModelThroughHoleExecutor._com_member(curve, "CircleParams", default=None)
        if not isinstance(values, (tuple, list)) or len(values) < 7:
            return None
        try:
            return tuple(float(value) for value in values[:7])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parallel_axes(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
        first_norm = math.sqrt(sum(value * value for value in first[:3]))
        second_norm = math.sqrt(sum(value * value for value in second[:3]))
        if first_norm <= 0 or second_norm <= 0:
            return False
        dot = sum(first[index] * second[index] for index in range(3)) / (first_norm * second_norm)
        return abs(abs(dot) - 1.0) <= 1e-6

    @staticmethod
    def _point_distance(first: tuple[float, ...], second: tuple[float, ...]) -> float:
        return sum((first[index] - second[index]) ** 2 for index in range(3)) ** 0.5

    @staticmethod
    def _edge_points(edge: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        start = ActiveModelThroughHoleExecutor._com_member(edge, "GetStartVertex")
        end = ActiveModelThroughHoleExecutor._com_member(edge, "GetEndVertex")
        if start is None or end is None:
            return None
        start_point = ActiveModelThroughHoleExecutor._com_member(start, "GetPoint")
        end_point = ActiveModelThroughHoleExecutor._com_member(end, "GetPoint")
        if not start_point or not end_point or len(start_point) < 3 or len(end_point) < 3:
            return None
        return tuple(float(value) for value in start_point[:3]), tuple(float(value) for value in end_point[:3])

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any], path: Path | None = None) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files = [str(report_path)]
        if path:
            files.append(str(path))
        payload["files"] = files
        return SkillResult(success=success, message=message, data=payload, path=str(path or report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))
