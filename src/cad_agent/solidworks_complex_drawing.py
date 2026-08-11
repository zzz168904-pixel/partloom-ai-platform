from __future__ import annotations

import traceback
from typing import Any


SW_DRAWING_SECTION_VIEW = 2
SW_DRAWING_DETAIL_VIEW = 3
SW_DETAIL_VIEW_STANDARD = 0
SW_DETAIL_CIRCLE = 1


class SolidWorksComplexDrawingViewEngine:
    """Create requested section and detail views in an existing drawing."""

    def __init__(self, connector: Any) -> None:
        self.connector = connector

    def apply(self, drawing: Any, drawing_plan: dict[str, Any]) -> dict[str, Any]:
        requests = [dict(item) for item in drawing_plan.get("special_views", [])]
        if not requests:
            return {
                "success": True,
                "requested": 0,
                "created": 0,
                "views": [],
                "layout": {"status": "not_required"},
                "errors": [],
            }

        source_views = self._standard_source_views(drawing)
        source_layout = self._layout_source_views(drawing, source_views)
        if not source_layout.get("success"):
            return {
                "success": False,
                "requested": len(requests),
                "created": 0,
                "views": [],
                "layout": source_layout,
                "errors": [str(source_layout.get("error") or "Source drawing view layout failed.")],
            }
        source_outlines = {
            name: self.connector._get_view_outline(view)
            for name, view in source_views.items()
        }
        created_views: dict[str, Any] = {}
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        for request in requests:
            view_type = str(request.get("type") or "")
            source_name = str(request.get("source_view") or "front").lower()
            if source_name == "auto":
                source_name = self._largest_orthographic_view(source_outlines)
            source_view = source_views.get(source_name)
            if source_view is None:
                result = {
                    "type": view_type,
                    "success": False,
                    "required": bool(request.get("required", True)),
                    "error": f"Source drawing view is unavailable: {source_name}",
                }
            elif view_type == "section_view":
                result = self._create_section_view(drawing, source_view, request, source_outlines[source_name])
            elif view_type == "detail_view":
                result = self._create_detail_view(drawing, source_view, request, source_outlines[source_name])
            else:
                result = {
                    "type": view_type,
                    "success": False,
                    "required": bool(request.get("required", True)),
                    "error": f"Unsupported complex drawing view: {view_type}",
                }
            results.append(result)
            if result.get("success") and result.get("view") is not None:
                created_views[view_type] = result["view"]
            elif result.get("required", True):
                errors.append(str(result.get("error") or f"Failed to create {view_type}"))

        layout = self._layout_views(drawing, source_views, created_views, source_layout)
        if created_views and not layout.get("success"):
            errors.append(str(layout.get("error") or "Complex drawing view layout failed."))

        serializable_results = []
        for result in results:
            item = {key: value for key, value in result.items() if key != "view"}
            serializable_results.append(item)
        return {
            "success": not errors,
            "requested": len(requests),
            "created": len(created_views),
            "views": serializable_results,
            "layout": layout,
            "errors": errors,
        }

    def _create_section_view(
        self,
        drawing: Any,
        source_view: Any,
        request: dict[str, Any],
        source_outline: tuple[float, float, float, float],
    ) -> dict[str, Any]:
        label = str(request.get("label") or "A")
        required = bool(request.get("required", True))
        cutting_plane = dict(request.get("cutting_plane") or {})
        orientation = str(cutting_plane.get("orientation") or "vertical").lower()
        try:
            outline = source_outline
            left, bottom, right, top = outline
            width = max(right - left, 0.001)
            height = max(top - bottom, 0.001)
            if orientation == "horizontal":
                y = (bottom + top) / 2.0
                line_points = (left + width * 0.08, y, right - width * 0.08, y)
            else:
                x = (left + right) / 2.0
                line_points = (x, bottom + height * 0.08, x, top - height * 0.08)

            self._activate_view(drawing, source_view)
            self._clear_selection(drawing)
            sketch_manager = self._member(drawing, "SketchManager")
            if sketch_manager is None:
                raise RuntimeError("Drawing SketchManager is unavailable.")
            line = sketch_manager.CreateLine(
                float(line_points[0]),
                float(line_points[1]),
                0.0,
                float(line_points[2]),
                float(line_points[3]),
                0.0,
            )
            if line is None:
                raise RuntimeError("CreateLine returned no section line.")
            if self._selection_count(drawing) <= 0 and not self._select_sketch_segment(drawing, source_view, line):
                raise RuntimeError("The section line could not be selected.")

            target = self._initial_target_position(drawing, "section_view")
            options = int(request.get("options", 0) or 0)
            section_depth = float(request.get("section_depth_m", 0.0) or 0.0)
            view = None
            api_method = ""
            api_errors: list[str] = []
            for method_name, args in (
                (
                    "CreateSectionViewAt5",
                    (target[0], target[1], 0.0, label, options, None, section_depth),
                ),
                (
                    "CreateSectionViewAt4",
                    (target[0], target[1], 0.0, label, options, None),
                ),
            ):
                try:
                    method = getattr(drawing, method_name)
                    view = method(*args)
                    if view is not None:
                        api_method = method_name
                        break
                except Exception as exc:
                    api_errors.append(f"{method_name}: {exc!r}")
            if view is None:
                raise RuntimeError("; ".join(api_errors) or "Section view API returned no view.")

            view_type = self._view_type(view)
            if view_type != SW_DRAWING_SECTION_VIEW:
                raise RuntimeError(f"Section API returned a non-section drawing view: type={view_type!r}")
            geometry_evidence = self._view_geometry_evidence(drawing, view)
            if not geometry_evidence["has_model_geometry"]:
                raise RuntimeError("The native section view contains no visible model entities.")

            self._clear_selection(drawing)
            return {
                "type": "section_view",
                "success": True,
                "required": required,
                "label": label,
                "source_view": self.connector._get_view_name(source_view),
                "cutting_plane": {
                    "orientation": orientation,
                    "through": cutting_plane.get("through", "center"),
                    "line_points_m": list(line_points),
                    "coordinate_space": "drawing_sheet_m",
                },
                "placement_m": list(target),
                "api_method": api_method,
                "view_name": self.connector._get_view_name(view),
                "view_type": view_type,
                "native_type_verified": True,
                "visible_entity_count": geometry_evidence["visible_entity_count"],
                "geometry_evidence": geometry_evidence,
                "view": view,
            }
        except Exception as exc:
            return {
                "type": "section_view",
                "success": False,
                "required": required,
                "label": label,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }

    def _create_detail_view(
        self,
        drawing: Any,
        source_view: Any,
        request: dict[str, Any],
        source_outline: tuple[float, float, float, float],
    ) -> dict[str, Any]:
        label = str(request.get("label") or "B")
        required = bool(request.get("required", True))
        boundary = dict(request.get("boundary") or {})
        scale = float(request.get("scale", 2.0) or 2.0)
        try:
            if scale <= 1.0:
                raise ValueError("Detail view scale must be greater than 1.0.")
            left, bottom, right, top = source_outline
            view_scale = max(self.connector._get_view_scale(source_view), 1e-9)
            width = max((right - left) / view_scale, 0.001)
            height = max((top - bottom) / view_scale, 0.001)
            center_x = 0.0
            center_y = 0.0
            normalized_center = boundary.get("normalized_center")
            if isinstance(normalized_center, (list, tuple)) and len(normalized_center) >= 2:
                center_x = (float(normalized_center[0]) - 0.5) * width
                center_y = (float(normalized_center[1]) - 0.5) * height
            radius_ratio = float(boundary.get("radius_ratio", 0.20) or 0.20)
            radius = max(min(width, height) * radius_ratio, 0.003)

            self._activate_view(drawing, source_view)
            self._clear_selection(drawing)
            sketch_manager = self._member(drawing, "SketchManager")
            if sketch_manager is None:
                raise RuntimeError("Drawing SketchManager is unavailable.")
            circle = sketch_manager.CreateCircle(
                center_x,
                center_y,
                0.0,
                center_x + radius,
                center_y,
                0.0,
            )
            if circle is None:
                raise RuntimeError("CreateCircle returned no detail boundary.")

            target = self._initial_target_position(drawing, "detail_view")
            view = drawing.CreateDetailViewAt4(
                target[0],
                target[1],
                0.0,
                SW_DETAIL_VIEW_STANDARD,
                scale,
                1.0,
                label,
                SW_DETAIL_CIRCLE,
                True,
                True,
                False,
                5,
            )
            if view is None:
                raise RuntimeError("CreateDetailViewAt4 returned no view.")
            view_type = self._view_type(view)
            if view_type != SW_DRAWING_DETAIL_VIEW:
                raise RuntimeError(f"Detail API returned a non-detail drawing view: type={view_type!r}")
            geometry_evidence = self._view_geometry_evidence(drawing, view)
            if not geometry_evidence["has_model_geometry"]:
                raise RuntimeError("The native detail view contains no visible model entities.")
            self._clear_selection(drawing)
            return {
                "type": "detail_view",
                "success": True,
                "required": required,
                "label": label,
                "source_view": self.connector._get_view_name(source_view),
                "boundary": {
                    "center_m": [center_x, center_y],
                    "radius_m": radius,
                    "radius_ratio": radius_ratio,
                    "coordinate_space": "active_drawing_view_local",
                },
                "scale": scale,
                "placement_m": list(target),
                "api_method": "CreateDetailViewAt4",
                "view_name": self.connector._get_view_name(view),
                "view_type": view_type,
                "native_type_verified": True,
                "visible_entity_count": geometry_evidence["visible_entity_count"],
                "geometry_evidence": geometry_evidence,
                "view": view,
            }
        except Exception as exc:
            return {
                "type": "detail_view",
                "success": False,
                "required": required,
                "label": label,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }

    def _layout_views(
        self,
        drawing: Any,
        source_views: dict[str, Any],
        created_views: dict[str, Any],
        source_layout: dict[str, Any],
    ) -> dict[str, Any]:
        if not created_views:
            return {"success": True, "status": "not_required", "source_layout": source_layout}
        try:
            ordered = [
                ("front", source_views.get("front")),
                ("top", source_views.get("top")),
                ("right", source_views.get("right")),
                ("isometric", source_views.get("isometric")),
                ("section_view", created_views.get("section_view")),
                ("detail_view", created_views.get("detail_view")),
            ]
            ordered = [(name, view) for name, view in ordered if view is not None]
            sheet_width, sheet_height, _ = self.connector._get_sheet_size(drawing)
            area = self.connector._drawing_effective_area(sheet_width, sheet_height)
            self.connector._rebuild_and_zoom(drawing)
            final_outlines = {name: self.connector._get_view_outline(view) for name, view in ordered}
            inside = self.connector._outlines_inside_area(final_outlines, area)
            overlap = self.connector._has_overlap(final_outlines)
            positions = {
                name: list(self._current_view_position(view))
                for name, view in ordered
            }
            return {
                "success": bool(inside and not overlap),
                "status": "success" if inside and not overlap else "failed",
                "effective_area_m": list(area),
                "positions_m": positions,
                "outlines_m": {name: list(value) for name, value in final_outlines.items()},
                "inside_effective_area": inside,
                "overlap": overlap,
                "view_count": len(ordered),
                "source_layout": source_layout,
            }
        except Exception as exc:
            return {"success": False, "status": "failed", "error": repr(exc), "traceback": traceback.format_exc()}

    def _layout_source_views(self, drawing: Any, source_views: dict[str, Any]) -> dict[str, Any]:
        try:
            ordered = [(name, source_views.get(name)) for name in ("front", "top", "right", "isometric")]
            if any(view is None for _, view in ordered):
                return {"success": False, "status": "failed", "error": "Four standard source views are required."}
            sheet_width, sheet_height, _ = self.connector._get_sheet_size(drawing)
            area = self.connector._drawing_effective_area(sheet_width, sheet_height)
            left, bottom, right, top = area
            cell_width = (right - left) / 3.0
            cell_height = (top - bottom) / 2.0
            self.connector._rebuild_and_zoom(drawing)
            outlines = {name: self.connector._get_view_outline(view) for name, view in ordered}
            fit_factors = []
            for outline in outlines.values():
                width = max(outline[2] - outline[0], 0.001)
                height = max(outline[3] - outline[1], 0.001)
                fit_factors.append(min(cell_width * 0.72 / width, cell_height * 0.68 / height))
            common_factor = min([1.0, *fit_factors])
            if common_factor < 1.0:
                common_factor = max(common_factor * 0.94, 0.08)
                for name, view in ordered:
                    current_scale = self.connector._get_view_scale(view)
                    self.connector._set_view_scale(view, current_scale * common_factor, name)
                self.connector._rebuild_and_zoom(drawing)

            positions: dict[str, list[float]] = {}
            for (name, view), (column, row) in zip(ordered, self._layout_slots(4)):
                position = (
                    left + cell_width * (column + 0.5),
                    bottom + cell_height * (row + 0.5),
                )
                self.connector._set_view_position(view, position)
                positions[name] = [float(position[0]), float(position[1])]
            self.connector._rebuild_and_zoom(drawing)
            final_outlines = {name: self.connector._get_view_outline(view) for name, view in ordered}
            inside = self.connector._outlines_inside_area(final_outlines, area)
            overlap = self.connector._has_overlap(final_outlines)
            return {
                "success": bool(inside and not overlap),
                "status": "success" if inside and not overlap else "failed",
                "effective_area_m": list(area),
                "positions_m": positions,
                "outlines_m": {name: list(value) for name, value in final_outlines.items()},
                "inside_effective_area": inside,
                "overlap": overlap,
                "common_scale_factor": common_factor,
            }
        except Exception as exc:
            return {"success": False, "status": "failed", "error": repr(exc), "traceback": traceback.format_exc()}

    def _standard_source_views(self, drawing: Any) -> dict[str, Any]:
        views = self.connector._get_drawing_views(drawing, verbose=False)
        mapping: dict[str, Any] = {}
        ordered_names = ("front", "top", "right", "isometric")
        for name, view in zip(ordered_names, views[:4]):
            mapping[name] = view
        return mapping

    @staticmethod
    def _largest_orthographic_view(
        outlines: dict[str, tuple[float, float, float, float]],
    ) -> str:
        candidates = ("front", "top", "right")
        return max(
            candidates,
            key=lambda name: max(outlines[name][2] - outlines[name][0], 0.0)
            * max(outlines[name][3] - outlines[name][1], 0.0),
        )

    def _initial_target_position(self, drawing: Any, view_type: str) -> tuple[float, float]:
        sheet_width, sheet_height, _ = self.connector._get_sheet_size(drawing)
        left, bottom, right, top = self.connector._drawing_effective_area(sheet_width, sheet_height)
        cell_width = (right - left) / 3.0
        cell_height = (top - bottom) / 2.0
        x = left + cell_width * 2.5
        row = 0.5 if view_type == "section_view" else 1.5
        return x, bottom + cell_height * row

    def _activate_view(self, drawing: Any, view: Any) -> None:
        view_name = self.connector._get_view_name(view)
        if not self.connector._activate_drawing_view(drawing, view_name):
            raise RuntimeError(f"Could not activate drawing view: {view_name}")

    @staticmethod
    def _clear_selection(drawing: Any) -> None:
        try:
            drawing.ClearSelection2(True)
        except Exception:
            pass

    def _select_sketch_segment(self, drawing: Any, source_view: Any, segment: Any) -> bool:
        try:
            selection_manager = self._member(drawing, "SelectionManager")
            select_data = selection_manager.CreateSelectData() if selection_manager is not None else None
            if select_data is not None:
                try:
                    select_data.View = source_view
                except Exception:
                    pass
                try:
                    if segment.Select4(False, select_data):
                        return True
                except Exception:
                    pass
            try:
                return bool(segment.Select2(False, 0))
            except Exception:
                return False
        except Exception:
            return False

    def _selection_count(self, drawing: Any) -> int:
        try:
            selection_manager = self._member(drawing, "SelectionManager")
            if selection_manager is None:
                return 0
            return int(selection_manager.GetSelectedObjectCount2(-1))
        except Exception:
            return 0

    def _visible_entity_count(self, view: Any) -> int | None:
        method = getattr(self.connector, "_get_visible_view_entities", None)
        if not callable(method):
            return None
        try:
            return len(method(view))
        except Exception:
            return None

    def _view_geometry_evidence(self, drawing: Any, view: Any) -> dict[str, Any]:
        """Collect stable evidence that a native drawing view contains model geometry."""
        try:
            self.connector._rebuild_and_zoom(drawing)
        except Exception:
            pass

        entity_count = self._visible_entity_count(view)
        components = self._member(view, "GetVisibleComponents")
        if isinstance(components, (list, tuple)):
            component_count = len([item for item in components if item is not None])
        elif components is None:
            component_count = 0
        else:
            component_count = 1

        outline = self.connector._get_view_outline(view)
        width = max(float(outline[2]) - float(outline[0]), 0.0)
        height = max(float(outline[3]) - float(outline[1]), 0.0)
        outline_area = width * height
        has_model_geometry = bool(
            (entity_count is not None and entity_count > 0)
            or (component_count > 0 and outline_area > 1e-8)
        )
        return {
            "has_model_geometry": has_model_geometry,
            "visible_entity_count": entity_count,
            "visible_component_count": component_count,
            "outline_m": [float(value) for value in outline],
            "outline_area_m2": outline_area,
            "validation_method": (
                "visible_entities"
                if entity_count is not None and entity_count > 0
                else "visible_components_and_outline"
                if component_count > 0 and outline_area > 1e-8
                else "none"
            ),
        }

    @classmethod
    def _current_view_position(cls, view: Any) -> tuple[float, float]:
        value = cls._member(view, "Position")
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
        value = getattr(view, "position", None)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
        raise RuntimeError("Drawing view position is unavailable.")

    @staticmethod
    def _layout_slots(count: int) -> list[tuple[int, int]]:
        preferred = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)]
        return preferred[:count]

    @classmethod
    def _view_type(cls, view: Any) -> int | None:
        value = cls._member(view, "Type")
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _member(obj: Any, name: str) -> Any:
        try:
            value = getattr(obj, name)
            if hasattr(value, "_oleobj_"):
                return value
            return value() if callable(value) else value
        except Exception:
            return None
