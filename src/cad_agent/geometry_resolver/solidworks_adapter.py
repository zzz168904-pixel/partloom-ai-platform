from __future__ import annotations

from typing import Any, Iterable

from .models import GeometryCandidate, GeometryResolutionError, Vector3, unit, vector3


def com_value(target: Any, name: str, *args: Any, default: Any = None) -> Any:
    if target is None:
        return default
    try:
        value = getattr(target, name)
        return value(*args) if callable(value) else value
    except Exception:
        return default


def _sequence(value: Any, length: int) -> tuple[float, ...] | None:
    raw = getattr(value, "value", value)
    if not isinstance(raw, (tuple, list)) or len(raw) < length:
        return None
    try:
        result = tuple(float(item) for item in raw[:length])
    except (TypeError, ValueError):
        return None
    return result


def _normal_from_box(box: tuple[float, ...]) -> Vector3:
    spans = [abs(box[index + 3] - box[index]) for index in range(3)]
    axis = min(range(3), key=spans.__getitem__)
    result = [0.0, 0.0, 0.0]
    result[axis] = 1.0
    return vector3(result)


def _bbox_area(box: tuple[float, ...], normal: Vector3) -> float:
    spans = [abs(box[index + 3] - box[index]) for index in range(3)]
    axis = max(range(3), key=lambda index: abs(normal[index]))
    in_plane = [spans[index] for index in range(3) if index != axis]
    return in_plane[0] * in_plane[1]


class SolidWorksFaceInspector:
    """Read-only extraction of stable-enough face attributes for guarded fallback."""

    def inspect_face(
        self,
        face: Any,
        *,
        body_index: int | None = None,
        face_index: int | None = None,
        body: Any | None = None,
    ) -> dict[str, Any]:
        box = _sequence(com_value(face, "GetBox"), 6)
        if box is None:
            raise GeometryResolutionError("SolidWorks face does not expose a valid GetBox result.")
        normal_raw = _sequence(com_value(face, "Normal"), 3)
        has_planar_normal = bool(
            normal_raw is not None and sum(item * item for item in normal_raw) > 1e-16
        )
        surface = com_value(face, "GetSurface")
        is_plane = (
            bool(com_value(surface, "IsPlane", default=False))
            if surface is not None
            else has_planar_normal
        )
        plane_params = _sequence(com_value(surface, "PlaneParams"), 6) if surface is not None else None
        if normal_raw is None or sum(item * item for item in normal_raw) <= 1e-16:
            normal_raw = plane_params[:3] if plane_params else _normal_from_box(box)
        normal = unit(normal_raw, "face_normal")
        box_center = tuple((box[index] + box[index + 3]) / 2.0 for index in range(3))
        plane_origin = vector3(plane_params[3:6] if plane_params else box_center, "plane_origin_m")
        approximate_area = _bbox_area(box, normal)
        area_value = com_value(face, "GetArea", default=None)
        try:
            area = float(area_value)
        except (TypeError, ValueError):
            area = approximate_area
        if area <= 0.0:
            area = approximate_area
        feature = com_value(face, "GetFeature")
        feature_name = str(com_value(feature, "Name", default="") or "")
        body_name = str(com_value(body, "Name", default="") or "")
        normal_axis = max(range(3), key=lambda index: abs(normal[index]))
        signature: dict[str, Any] = {
            "entity_type": "Face",
            "surface_type": "plane" if is_plane else "other",
            "planar": is_plane,
            "normal": list(normal),
            "normal_axis": normal_axis,
            "plane_origin_m": list(plane_origin),
            "plane_coordinate_m": plane_origin[normal_axis],
            "face_box_m": list(box),
            "area_m2": area,
            "approximate_area_m2": approximate_area,
        }
        if body_index is not None:
            signature["body_index"] = int(body_index)
        if face_index is not None:
            signature["face_index"] = int(face_index)
        if body_name:
            signature["body_name"] = body_name
        if feature_name:
            signature["feature_name"] = feature_name
        return signature

    def enumerate_planar_faces(self, bodies: Iterable[Any]) -> tuple[GeometryCandidate, ...]:
        candidates: list[GeometryCandidate] = []
        for body_index, body in enumerate(bodies):
            faces = com_value(body, "GetFaces", default=()) or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face_index, face in enumerate(faces):
                if face is None:
                    continue
                try:
                    signature = self.inspect_face(
                        face,
                        body_index=body_index,
                        face_index=face_index,
                        body=body,
                    )
                except GeometryResolutionError:
                    continue
                if signature.get("planar"):
                    candidates.append(GeometryCandidate(signature=signature, entity=face))
        return tuple(candidates)
