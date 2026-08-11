from __future__ import annotations

import copy
import math
from typing import Any


UNIT_FACTORS_MM = {
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "millimetre": 1.0,
    "millimetres": 1.0,
    "cm": 10.0,
    "centimeter": 10.0,
    "centimeters": 10.0,
    "m": 1000.0,
    "meter": 1000.0,
    "meters": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    '"': 25.4,
}

PLANE_NAMES = {
    "front": "Front Plane",
    "front_plane": "Front Plane",
    "front plane": "Front Plane",
    "top": "Top Plane",
    "top_plane": "Top Plane",
    "top plane": "Top Plane",
    "right": "Right Plane",
    "right_plane": "Right Plane",
    "right plane": "Right Plane",
}

POINT_TOLERANCE_MM = 1e-4
AREA_TOLERANCE_MM2 = 1e-6


def normalize_profile_extrude_request(
    params: dict[str, Any],
    task_type: str | None = None,
    default_unit: str | None = None,
) -> dict[str, Any]:
    """Normalize and preflight a deterministic closed-profile extrusion."""

    source = copy.deepcopy(dict(params or {}))
    errors: list[dict[str, Any]] = []
    unit = _unit_token(default_unit or source.get("unit") or "mm")
    if unit is None:
        errors.append(_issue("unsupported_unit", f"Unsupported profile unit: {default_unit or source.get('unit')!r}", "unit"))
        unit = "mm"

    body_operation = str(
        _first(source, "body_operation", "extrude_operation", "result_operation", "operation") or ""
    ).strip().lower()
    body_operation = {
        "add": "boss",
        "additive": "boss",
        "remove": "cut",
        "subtractive": "cut",
        "new_body": "base",
    }.get(body_operation, body_operation)
    if body_operation not in {"base", "boss", "cut"}:
        errors.append(_issue(
            "invalid_body_operation",
            "profile_extrude requires body_operation=base, boss, or cut.",
            "body_operation",
        ))

    mode = str(source.get("mode") or ("new_model" if body_operation == "base" else "active_model")).strip().lower()
    if mode not in {"new_model", "active_model"}:
        errors.append(_issue("invalid_mode", "profile_extrude mode must be new_model or active_model.", "mode"))
    if body_operation == "base" and mode != "new_model":
        errors.append(_issue("operation_mode_conflict", "A base profile extrusion must create a new model.", "mode"))
    if body_operation in {"boss", "cut"} and mode != "active_model":
        errors.append(_issue(
            "operation_mode_conflict",
            "Boss and cut profile extrusions require active_model mode.",
            "mode",
        ))
    if task_type == "modify_3d" and mode == "new_model":
        errors.append(_issue("task_mode_conflict", "modify_3d cannot create a new profile-extruded Part.", "mode"))

    plane_token = str(_first(source, "sketch_plane", "plane") or "").strip().lower()
    sketch_plane = PLANE_NAMES.get(plane_token)
    if sketch_plane is None:
        errors.append(_issue(
            "missing_or_invalid_sketch_plane",
            "profile_extrude requires an explicit front, top, or right sketch_plane.",
            "sketch_plane",
        ))

    end_condition = str(_first(source, "end_condition", "extent") or "blind").strip().lower()
    end_condition = {
        "midplane": "mid_plane",
        "mid-plane": "mid_plane",
        "through": "through_all",
        "throughall": "through_all",
        "throughnext": "through_next",
        "through-next": "through_next",
        "through_all_both_directions": "through_all_both",
        "throughallboth": "through_all_both",
        "through-both": "through_all_both",
    }.get(end_condition, end_condition)
    if body_operation == "base":
        allowed_end_conditions = {"blind", "mid_plane"}
    elif body_operation == "boss":
        allowed_end_conditions = {"blind", "mid_plane", "through_next"}
    else:
        allowed_end_conditions = {"blind", "mid_plane", "through_all", "through_all_both"}
    if body_operation in {"base", "boss", "cut"} and end_condition not in allowed_end_conditions:
        errors.append(_issue(
            "invalid_end_condition",
            f"{body_operation or 'profile_extrude'} does not support end_condition={end_condition!r}.",
            "end_condition",
        ))

    depth_mm = _length_from_aliases(source, unit, errors, "depth_mm", "depth")
    depthless_end_conditions = {"through_all", "through_all_both", "through_next"}
    if end_condition not in depthless_end_conditions and (
        depth_mm is None or depth_mm <= 0.0
    ):
        errors.append(_issue(
            "missing_or_invalid_depth",
            "Blind and mid-plane profile extrusions require depth_mm > 0.",
            "depth_mm",
        ))
    if end_condition in depthless_end_conditions:
        depth_mm = 0.0

    reverse_direction = _boolean(source.get("reverse_direction", False), errors, "reverse_direction")
    merge_result = _boolean(source.get("merge_result", True), errors, "merge_result")
    flip_side_to_cut = _boolean(source.get("flip_side_to_cut", False), errors, "flip_side_to_cut")
    if flip_side_to_cut and body_operation != "cut":
        errors.append(_issue(
            "flip_side_to_cut_operation_conflict",
            "flip_side_to_cut is valid only for cut profile extrusions.",
            "flip_side_to_cut",
        ))
    start_offset_mm = _length_from_aliases(
        source,
        unit,
        errors,
        "start_offset_mm",
        "start_offset",
    )
    if start_offset_mm is None:
        start_offset_mm = 0.0
    if start_offset_mm < 0.0:
        errors.append(_issue(
            "invalid_start_offset",
            "profile_extrude start_offset_mm must be greater than or equal to zero.",
            "start_offset_mm",
        ))
    flip_start_offset = _boolean(source.get("flip_start_offset", False), errors, "flip_start_offset")
    start_offset_supported = (
        (body_operation == "boss" and end_condition in {"blind", "through_next"})
        or (body_operation == "cut" and end_condition == "blind")
    )
    if start_offset_mm > POINT_TOLERANCE_MM and not start_offset_supported:
        errors.append(_issue(
            "unsupported_start_offset_combination",
            "A non-zero start offset is supported only for blind/through-next bosses or blind cuts.",
            "start_offset_mm",
        ))

    raw_profiles = _first(source, "profiles", "loops", "profile")
    loops, loop_errors = _normalize_loops(raw_profiles, unit)
    errors.extend(loop_errors)
    metrics: dict[str, Any] = {}
    if loops and not loop_errors:
        loops, metrics, geometry_errors = _validate_profile_set(loops)
        errors.extend(geometry_errors)

    if errors:
        return {
            "success": False,
            "message": errors[0]["message"],
            "errors": errors,
        }

    request = {
        "success": True,
        "message": "Closed profile extrusion request is valid.",
        "body_operation": body_operation,
        "operation": body_operation,
        "mode": mode,
        "sketch_plane": sketch_plane,
        "end_condition": end_condition,
        "depth_mm": float(depth_mm or 0.0),
        "depth": float(depth_mm or 0.0),
        "reverse_direction": bool(reverse_direction),
        "merge_result": bool(merge_result),
        "flip_side_to_cut": bool(flip_side_to_cut),
        "start_offset_mm": float(start_offset_mm),
        "flip_start_offset": bool(flip_start_offset),
        "profiles": loops,
        "profile_metrics": metrics,
    }
    return request


def _normalize_loops(raw_profiles: Any, default_unit: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if raw_profiles in (None, "", []):
        return [], [_issue("missing_profiles", "profile_extrude requires at least one closed profile loop.", "profiles")]
    if isinstance(raw_profiles, dict):
        raw_loops = [raw_profiles]
    elif isinstance(raw_profiles, (list, tuple)):
        if raw_profiles and all(_looks_like_point(item) for item in raw_profiles):
            raw_loops = [{"role": "outer", "points": list(raw_profiles)}]
        else:
            raw_loops = list(raw_profiles)
    else:
        return [], [_issue("profiles_not_list", "profiles must be a loop object or a list of loop objects.", "profiles")]

    loops: list[dict[str, Any]] = []
    for loop_index, raw_loop in enumerate(raw_loops):
        field = f"profiles[{loop_index}]"
        if not isinstance(raw_loop, dict):
            errors.append(_issue("profile_loop_not_object", "Each profile loop must be an object.", field))
            continue
        role = str(raw_loop.get("role") or "auto").strip().lower()
        if role not in {"outer", "inner", "auto"}:
            errors.append(_issue("invalid_loop_role", "Profile loop role must be outer, inner, or auto.", f"{field}.role"))
            continue

        raw_points = _first(raw_loop, "points_mm", "points")
        raw_segments = raw_loop.get("segments")
        if raw_points is not None and raw_segments is not None:
            errors.append(_issue(
                "profile_definition_conflict",
                "A profile loop cannot define both points and segments.",
                field,
            ))
            continue
        if raw_points is not None:
            point_unit = "mm" if "points_mm" in raw_loop else default_unit
            points, point_errors = _normalize_point_loop(raw_points, point_unit, bool(raw_loop.get("closed", False)), field)
            errors.extend(point_errors)
            if point_errors:
                continue
            segments = [
                {"type": "line", "start_mm": first, "end_mm": second}
                for first, second in zip(points, points[1:])
            ]
        else:
            if not isinstance(raw_segments, (list, tuple)) or not raw_segments:
                errors.append(_issue("missing_loop_geometry", "Each profile loop requires points or segments.", field))
                continue
            segments, segment_errors = _normalize_segments(raw_segments, default_unit, field)
            errors.extend(segment_errors)
            if segment_errors:
                continue
        loops.append({
            "id": str(raw_loop.get("id") or f"loop_{loop_index + 1}"),
            "role": role,
            "segments": segments,
        })
    return loops, errors


def _normalize_point_loop(
    raw_points: Any,
    unit: str,
    explicitly_closed: bool,
    field: str,
) -> tuple[list[list[float]], list[dict[str, Any]]]:
    if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 3:
        return [], [_issue("profile_too_short", "A point profile requires at least three points.", field)]
    errors: list[dict[str, Any]] = []
    points: list[list[float]] = []
    for index, value in enumerate(raw_points):
        point = _point_mm(value, unit, errors, f"{field}.points[{index}]")
        if point is not None:
            points.append(point)
    if errors:
        return [], errors
    if not _same_point(points[0], points[-1]):
        if explicitly_closed:
            points.append(list(points[0]))
        else:
            errors.append(_issue(
                "profile_not_closed",
                "Point profiles must repeat the first point or set closed=true.",
                field,
            ))
    return points, errors


def _normalize_segments(
    raw_segments: Any,
    default_unit: str,
    field: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(raw_segments):
        segment_field = f"{field}.segments[{index}]"
        if not isinstance(raw_segment, dict):
            errors.append(_issue("segment_not_object", "Each profile segment must be an object.", segment_field))
            continue
        segment_type = str(raw_segment.get("type") or "").strip().lower()
        segment_type = {"segment": "line", "circular_arc": "arc"}.get(segment_type, segment_type)
        if segment_type not in {"line", "arc", "circle"}:
            errors.append(_issue(
                "unsupported_profile_segment",
                "Profile segments currently support only line, arc, and circle.",
                f"{segment_field}.type",
            ))
            continue
        point_unit = "mm" if any(key.endswith("_mm") for key in raw_segment) else default_unit
        if segment_type == "line":
            start = _point_mm(_first(raw_segment, "start_mm", "start"), point_unit, errors, f"{segment_field}.start")
            end = _point_mm(_first(raw_segment, "end_mm", "end"), point_unit, errors, f"{segment_field}.end")
            if start is not None and end is not None:
                if _same_point(start, end):
                    errors.append(_issue("zero_length_segment", "Line segment length must be greater than zero.", segment_field))
                else:
                    segments.append({"type": "line", "start_mm": start, "end_mm": end})
            continue
        if segment_type == "arc":
            center = _point_mm(_first(raw_segment, "center_mm", "center"), point_unit, errors, f"{segment_field}.center")
            start = _point_mm(_first(raw_segment, "start_mm", "start"), point_unit, errors, f"{segment_field}.start")
            end = _point_mm(_first(raw_segment, "end_mm", "end"), point_unit, errors, f"{segment_field}.end")
            direction = _arc_direction(raw_segment.get("direction"), errors, f"{segment_field}.direction")
            if center is not None and start is not None and end is not None and direction is not None:
                start_radius = math.dist(center, start)
                end_radius = math.dist(center, end)
                tolerance = max(POINT_TOLERANCE_MM, max(start_radius, end_radius) * 1e-5)
                if start_radius <= POINT_TOLERANCE_MM:
                    errors.append(_issue("invalid_arc_radius", "Arc radius must be greater than zero.", segment_field))
                elif abs(start_radius - end_radius) > tolerance:
                    errors.append(_issue(
                        "arc_radius_mismatch",
                        "Arc start and end points must lie on the same circle.",
                        segment_field,
                    ))
                elif _same_point(start, end):
                    errors.append(_issue("ambiguous_full_arc", "Use a circle segment for a full circle.", segment_field))
                else:
                    segments.append({
                        "type": "arc",
                        "center_mm": center,
                        "start_mm": start,
                        "end_mm": end,
                        "direction": direction,
                        "radius_mm": (start_radius + end_radius) / 2.0,
                    })
            continue

        center = _point_mm(_first(raw_segment, "center_mm", "center"), point_unit, errors, f"{segment_field}.center")
        radius_unit = "mm" if "radius_mm" in raw_segment else default_unit
        radius = _length(_first(raw_segment, "radius_mm", "radius"), radius_unit, errors, f"{segment_field}.radius")
        if center is not None and radius is not None:
            if radius <= 0.0:
                errors.append(_issue("invalid_circle_radius", "Circle radius must be greater than zero.", segment_field))
            else:
                segments.append({"type": "circle", "center_mm": center, "radius_mm": radius})
    return segments, errors


def _validate_profile_set(
    loops: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []
    for index, loop in enumerate(loops):
        segments = loop["segments"]
        field = f"profiles[{index}]"
        circles = [segment for segment in segments if segment["type"] == "circle"]
        if circles and len(segments) != 1:
            errors.append(_issue(
                "circle_loop_mixed_with_chain",
                "A circle loop must contain exactly one circle segment.",
                field,
            ))
            continue
        if circles:
            polygon = _sample_circle(circles[0])
        else:
            if len(segments) < 2:
                errors.append(_issue("profile_too_short", "A chained profile requires at least two segments.", field))
                continue
            for segment_index, (first, second) in enumerate(zip(segments, segments[1:])):
                if not _same_point(_segment_end(first), _segment_start(second)):
                    errors.append(_issue(
                        "profile_segment_gap",
                        f"Profile segment {segment_index + 1} does not meet segment {segment_index + 2}.",
                        field,
                    ))
            if not _same_point(_segment_end(segments[-1]), _segment_start(segments[0])):
                errors.append(_issue("profile_not_closed", "Profile segment chain is not closed.", field))
            if errors:
                continue
            polygon = _sample_chain(segments)
        if _self_intersects(polygon):
            errors.append(_issue("self_intersecting_profile", "Profile loop self-intersects.", field))
            continue
        area = _exact_loop_area(segments)
        if abs(area) <= AREA_TOLERANCE_MM2:
            errors.append(_issue("zero_profile_area", "Profile loop area must be greater than zero.", field))
            continue
        evaluated.append({**loop, "_polygon": polygon, "_signed_area_mm2": area})
    if errors:
        return loops, {}, errors

    roles = [loop["role"] for loop in evaluated]
    if all(role == "auto" for role in roles):
        # Classify each disconnected contour by containment instead of assuming
        # every contour except the largest one is a hole. A sketch may contain
        # several disjoint boss/cut regions, as in a symmetric wing nut.
        for index, loop in enumerate(evaluated):
            point = loop["_polygon"][0]
            containment_depth = sum(
                _point_in_polygon(point, candidate["_polygon"])
                for candidate_index, candidate in enumerate(evaluated)
                if candidate_index != index
            )
            loop["role"] = "outer" if containment_depth % 2 == 0 else "inner"
    elif any(role == "auto" for role in roles):
        errors.append(_issue(
            "mixed_explicit_and_auto_roles",
            "Use explicit roles for every loop or auto for every loop.",
            "profiles.role",
        ))

    outer_loops = [loop for loop in evaluated if loop["role"] == "outer"]
    inner_loops = [loop for loop in evaluated if loop["role"] == "inner"]
    if not outer_loops:
        errors.append(_issue("missing_outer_profile", "profile_extrude requires at least one outer loop.", "profiles.role"))
        return loops, {}, errors

    for first_index, first in enumerate(evaluated):
        for second in evaluated[first_index + 1:]:
            if _polygons_intersect(first["_polygon"], second["_polygon"]):
                errors.append(_issue("profile_loops_intersect", "Profile loops cannot intersect or touch.", "profiles"))

    for first_index, first in enumerate(outer_loops):
        for second in outer_loops[first_index + 1:]:
            if (
                _point_in_polygon(first["_polygon"][0], second["_polygon"])
                or _point_in_polygon(second["_polygon"][0], first["_polygon"])
            ):
                errors.append(_issue(
                    "overlapping_outer_profiles",
                    "Outer profile loops must be disjoint and cannot contain each other.",
                    "profiles",
                ))

    for inner in inner_loops:
        containing_outers = [
            outer
            for outer in outer_loops
            if all(_point_in_polygon(point, outer["_polygon"]) for point in inner["_polygon"][:-1])
        ]
        if len(containing_outers) != 1:
            errors.append(_issue("inner_profile_outside_outer", "Every inner loop must be strictly inside the outer loop.", "profiles"))
    for first_index, first in enumerate(inner_loops):
        for second in inner_loops[first_index + 1:]:
            if (
                _polygons_intersect(first["_polygon"], second["_polygon"])
                or _point_in_polygon(first["_polygon"][0], second["_polygon"])
                or _point_in_polygon(second["_polygon"][0], first["_polygon"])
            ):
                errors.append(_issue("overlapping_inner_profiles", "Inner profile loops cannot overlap or contain each other.", "profiles"))
    if errors:
        return loops, {}, errors

    outer_area = sum(abs(float(loop["_signed_area_mm2"])) for loop in outer_loops)
    inner_area = sum(abs(float(loop["_signed_area_mm2"])) for loop in inner_loops)
    net_area = outer_area - inner_area
    if net_area <= AREA_TOLERANCE_MM2:
        return loops, {}, [_issue("nonpositive_net_profile_area", "Profile inner loops remove the complete outer area.", "profiles")]

    outer_points = [point for loop in outer_loops for point in loop["_polygon"][:-1]]
    clean_loops: list[dict[str, Any]] = []
    for loop in evaluated:
        clean_loops.append({key: copy.deepcopy(value) for key, value in loop.items() if not key.startswith("_")})
    metrics = {
        "outer_area_mm2": outer_area,
        "inner_area_mm2": inner_area,
        "net_area_mm2": net_area,
        "loop_count": len(evaluated),
        "outer_loop_count": len(outer_loops),
        "inner_loop_count": len(inner_loops),
        "bbox_mm": {
            "xmin": min(point[0] for point in outer_points),
            "xmax": max(point[0] for point in outer_points),
            "ymin": min(point[1] for point in outer_points),
            "ymax": max(point[1] for point in outer_points),
        },
    }
    return clean_loops, metrics, []


def _sample_chain(segments: list[dict[str, Any]]) -> list[list[float]]:
    points: list[list[float]] = []
    for segment in segments:
        sampled = _sample_segment(segment)
        if points and sampled and _same_point(points[-1], sampled[0]):
            sampled = sampled[1:]
        points.extend(sampled)
    if points and not _same_point(points[0], points[-1]):
        points.append(list(points[0]))
    return points


def _sample_segment(segment: dict[str, Any]) -> list[list[float]]:
    if segment["type"] == "line":
        return [list(segment["start_mm"]), list(segment["end_mm"])]
    if segment["type"] == "circle":
        return _sample_circle(segment)
    center = segment["center_mm"]
    start = segment["start_mm"]
    end = segment["end_mm"]
    radius = float(segment["radius_mm"])
    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    if int(segment["direction"]) > 0:
        sweep = (end_angle - start_angle) % (2.0 * math.pi)
    else:
        sweep = -((start_angle - end_angle) % (2.0 * math.pi))
    count = max(2, int(math.ceil(abs(sweep) / math.radians(1.0))))
    return [
        [
            center[0] + radius * math.cos(start_angle + sweep * index / count),
            center[1] + radius * math.sin(start_angle + sweep * index / count),
        ]
        for index in range(count + 1)
    ]


def _sample_circle(segment: dict[str, Any]) -> list[list[float]]:
    center = segment["center_mm"]
    radius = float(segment["radius_mm"])
    count = 360
    points = [
        [
            center[0] + radius * math.cos(2.0 * math.pi * index / count),
            center[1] + radius * math.sin(2.0 * math.pi * index / count),
        ]
        for index in range(count)
    ]
    points.append(list(points[0]))
    return points


def _self_intersects(points: list[list[float]]) -> bool:
    segments = list(zip(points, points[1:]))
    for first_index, (a, b) in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            if second_index == first_index + 1:
                continue
            if first_index == 0 and second_index == len(segments) - 1:
                continue
            c, d = segments[second_index]
            if _segments_intersect(a, b, c, d):
                return True
    return False


def _polygons_intersect(first: list[list[float]], second: list[list[float]]) -> bool:
    return any(
        _segments_intersect(a, b, c, d)
        for a, b in zip(first, first[1:])
        for c, d in zip(second, second[1:])
    )


def _segments_intersect(a: list[float], b: list[float], c: list[float], d: list[float]) -> bool:
    def orientation(p: list[float], q: list[float], r: list[float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    tolerance = 1e-8
    if o1 * o2 < -tolerance and o3 * o4 < -tolerance:
        return True
    return (
        abs(o1) <= tolerance and _point_on_segment(c, a, b)
        or abs(o2) <= tolerance and _point_on_segment(d, a, b)
        or abs(o3) <= tolerance and _point_on_segment(a, c, d)
        or abs(o4) <= tolerance and _point_on_segment(b, c, d)
    )


def _point_in_polygon(point: list[float], polygon: list[list[float]]) -> bool:
    if any(_point_on_segment(point, first, second) for first, second in zip(polygon, polygon[1:])):
        return False
    inside = False
    x, y = point
    for first, second in zip(polygon, polygon[1:]):
        y1, y2 = first[1], second[1]
        if (y1 > y) == (y2 > y):
            continue
        x_intersection = (second[0] - first[0]) * (y - y1) / (y2 - y1) + first[0]
        if x < x_intersection:
            inside = not inside
    return inside


def _point_on_segment(point: list[float], first: list[float], second: list[float]) -> bool:
    cross = (second[0] - first[0]) * (point[1] - first[1]) - (second[1] - first[1]) * (point[0] - first[0])
    if abs(cross) > 1e-7:
        return False
    return (
        min(first[0], second[0]) - POINT_TOLERANCE_MM <= point[0] <= max(first[0], second[0]) + POINT_TOLERANCE_MM
        and min(first[1], second[1]) - POINT_TOLERANCE_MM <= point[1] <= max(first[1], second[1]) + POINT_TOLERANCE_MM
    )


def _polygon_area(points: list[list[float]]) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:])
    )


def _exact_loop_area(segments: list[dict[str, Any]]) -> float:
    if len(segments) == 1 and segments[0]["type"] == "circle":
        radius = float(segments[0]["radius_mm"])
        return math.pi * radius * radius
    area = 0.0
    for segment in segments:
        if segment["type"] == "line":
            start = segment["start_mm"]
            end = segment["end_mm"]
            area += 0.5 * (start[0] * end[1] - end[0] * start[1])
            continue
        center = segment["center_mm"]
        start = segment["start_mm"]
        end = segment["end_mm"]
        radius = float(segment["radius_mm"])
        start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
        end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
        if int(segment["direction"]) > 0:
            sweep = (end_angle - start_angle) % (2.0 * math.pi)
        else:
            sweep = -((start_angle - end_angle) % (2.0 * math.pi))
        area += 0.5 * (
            center[0] * (end[1] - start[1])
            - center[1] * (end[0] - start[0])
            + radius * radius * sweep
        )
    return area


def _segment_start(segment: dict[str, Any]) -> list[float]:
    return list(segment["start_mm"])


def _segment_end(segment: dict[str, Any]) -> list[float]:
    return list(segment["end_mm"])


def _arc_direction(value: Any, errors: list[dict[str, Any]], field: str) -> int | None:
    if value in (None, ""):
        errors.append(_issue("missing_arc_direction", "Arc segments require direction=ccw/cw or 1/-1.", field))
        return None
    token = str(value).strip().lower()
    if value == 1 or token in {"1", "+1", "ccw", "counterclockwise", "counter_clockwise"}:
        return 1
    if value == -1 or token in {"-1", "cw", "clockwise"}:
        return -1
    errors.append(_issue("invalid_arc_direction", "Arc direction must be ccw/cw or 1/-1.", field))
    return None


def _point_mm(value: Any, unit: str, errors: list[dict[str, Any]], field: str) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x = _length(value[0], unit, errors, f"{field}.x")
        y = _length(value[1], unit, errors, f"{field}.y")
    elif isinstance(value, dict):
        x_key = "x_mm" if "x_mm" in value else "x"
        y_key = "y_mm" if "y_mm" in value else "y"
        x = _length(value.get(x_key), "mm" if x_key.endswith("_mm") else unit, errors, f"{field}.x")
        y = _length(value.get(y_key), "mm" if y_key.endswith("_mm") else unit, errors, f"{field}.y")
    else:
        errors.append(_issue("invalid_profile_point", "Profile points must contain two coordinates.", field))
        return None
    if x is None or y is None:
        return None
    return [x, y]


def _length_from_aliases(
    source: dict[str, Any],
    default_unit: str,
    errors: list[dict[str, Any]],
    mm_key: str,
    plain_key: str,
) -> float | None:
    values: list[tuple[str, float]] = []
    for key, unit in ((mm_key, "mm"), (plain_key, default_unit)):
        if key not in source or source[key] in (None, ""):
            continue
        before = len(errors)
        value = _length(source[key], unit, errors, key)
        if value is not None and len(errors) == before:
            values.append((key, value))
    if not values:
        return None
    tolerance = max(1e-6, max(abs(item[1]) for item in values) * 1e-6)
    if any(abs(item[1] - values[0][1]) > tolerance for item in values[1:]):
        errors.append(_issue(
            "conflicting_parameter_aliases",
            f"Conflicting values for {mm_key}: {values!r}",
            mm_key,
        ))
    return values[0][1]


def _length(value: Any, default_unit: str, errors: list[dict[str, Any]], field: str) -> float | None:
    unit = default_unit
    raw = value
    if isinstance(value, dict):
        raw = value.get("value")
        unit = str(value.get("unit") or default_unit).strip().lower()
    unit = _unit_token(unit)
    if unit is None:
        errors.append(_issue("unsupported_unit", f"Unsupported unit for {field}.", field))
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        errors.append(_issue("invalid_quantity", f"{field} must be numeric.", field))
        return None
    if not math.isfinite(numeric):
        errors.append(_issue("invalid_quantity", f"{field} must be finite.", field))
        return None
    return numeric * UNIT_FACTORS_MM[unit]


def _boolean(value: Any, errors: list[dict[str, Any]], field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"true", "yes", "on"}:
        return True
    if token in {"false", "no", "off"}:
        return False
    errors.append(_issue("invalid_boolean", f"{field} must be boolean.", field))
    return False


def _unit_token(value: Any) -> str | None:
    token = str(value or "").strip().lower()
    return token if token in UNIT_FACTORS_MM else None


def _same_point(first: list[float], second: list[float]) -> bool:
    return math.dist(first, second) <= POINT_TOLERANCE_MM


def _looks_like_point(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= 2 or isinstance(value, dict) and (
        {"x", "y"}.issubset(value) or {"x_mm", "y_mm"}.issubset(value)
    )


def _first(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def _issue(code: str, message: str, field: str) -> dict[str, Any]:
    return {"code": code, "message": message, "field": field}
