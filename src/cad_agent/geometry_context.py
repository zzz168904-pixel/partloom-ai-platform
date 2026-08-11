from __future__ import annotations

"""Stable, serializable geometry context for face-attached production features."""

from dataclasses import dataclass
from math import sqrt
from typing import Any


REQUIRED_FACE_CONTEXT_KEYS = (
    "target_body",
    "target_feature",
    "target_face_role",
    "face_normal",
    "face_origin",
    "local_u_axis",
    "local_v_axis",
    "placement_uv_mm",
)


def _vector(value: Any, key: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{key} must contain exactly three coordinates.")
    return [float(item) for item in value]


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _norm(vector: list[float]) -> float:
    return sqrt(_dot(vector, vector))


def _unit(vector: list[float], key: str) -> list[float]:
    length = _norm(vector)
    if length <= 1e-9:
        raise ValueError(f"{key} must not be a zero vector.")
    return [item / length for item in vector]


@dataclass(frozen=True)
class FaceContext:
    target_body: str
    target_feature: str
    target_face_role: str
    face_normal: list[float]
    face_origin: list[float]
    local_u_axis: list[float]
    local_v_axis: list[float]
    placement_uv_mm: list[list[float]]
    approximate_area_mm2: float | None = None
    bounding_box_location: str | None = None
    distance_to_reference_point_mm: float | None = None

    @classmethod
    def from_request(cls, request: dict[str, Any]) -> "FaceContext":
        raw = request.get("geometry_context") or request.get("target_geometry")
        if not isinstance(raw, dict):
            raise ValueError("geometry_context is required; ambiguous target_face values are not accepted.")
        missing = [key for key in REQUIRED_FACE_CONTEXT_KEYS if key not in raw]
        if missing:
            raise ValueError(f"geometry_context missing required keys: {', '.join(missing)}")
        normal = _unit(_vector(raw["face_normal"], "face_normal"), "face_normal")
        u_axis = _unit(_vector(raw["local_u_axis"], "local_u_axis"), "local_u_axis")
        v_axis = _unit(_vector(raw["local_v_axis"], "local_v_axis"), "local_v_axis")
        if abs(_dot(normal, u_axis)) > 1e-6 or abs(_dot(normal, v_axis)) > 1e-6 or abs(_dot(u_axis, v_axis)) > 1e-6:
            raise ValueError("face_normal, local_u_axis, and local_v_axis must be orthogonal.")
        points = raw["placement_uv_mm"]
        if not isinstance(points, list) or not points:
            raise ValueError("placement_uv_mm must contain at least one [u, v] point.")
        uv = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Each placement_uv_mm entry must contain [u_mm, v_mm].")
            uv.append([float(point[0]), float(point[1])])
        return cls(
            target_body=str(raw["target_body"]),
            target_feature=str(raw["target_feature"]),
            target_face_role=str(raw["target_face_role"]),
            face_normal=normal,
            face_origin=_vector(raw["face_origin"], "face_origin"),
            local_u_axis=u_axis,
            local_v_axis=v_axis,
            placement_uv_mm=uv,
            approximate_area_mm2=float(raw["approximate_area_mm2"]) if raw.get("approximate_area_mm2") is not None else None,
            bounding_box_location=str(raw["bounding_box_location"]) if raw.get("bounding_box_location") else None,
            distance_to_reference_point_mm=float(raw["distance_to_reference_point_mm"]) if raw.get("distance_to_reference_point_mm") is not None else None,
        )

    def model_point_m(self, uv_mm: list[float]) -> list[float]:
        return [
            self.face_origin[index] / 1000.0
            + self.local_u_axis[index] * uv_mm[0] / 1000.0
            + self.local_v_axis[index] * uv_mm[1] / 1000.0
            for index in range(3)
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_body": self.target_body,
            "target_feature": self.target_feature,
            "target_face_role": self.target_face_role,
            "face_normal": self.face_normal,
            "face_origin": self.face_origin,
            "local_u_axis": self.local_u_axis,
            "local_v_axis": self.local_v_axis,
            "placement_uv_mm": self.placement_uv_mm,
            "approximate_area_mm2": self.approximate_area_mm2,
            "bounding_box_location": self.bounding_box_location,
            "distance_to_reference_point_mm": self.distance_to_reference_point_mm,
        }
