from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable


Vector3 = tuple[float, float, float]
_EPSILON = 1e-10


class GeometryResolutionError(ValueError):
    pass


def vector3(value: Iterable[float], name: str = "vector") -> Vector3:
    values = tuple(float(item) for item in value)
    if len(values) != 3:
        raise GeometryResolutionError(f"{name} must contain exactly three values.")
    if not all(math.isfinite(item) for item in values):
        raise GeometryResolutionError(f"{name} must contain finite values.")
    return values  # type: ignore[return-value]


def dot(left: Vector3, right: Vector3) -> float:
    return sum(a * b for a, b in zip(left, right))


def cross(left: Vector3, right: Vector3) -> Vector3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def magnitude(value: Vector3) -> float:
    return math.sqrt(dot(value, value))


def unit(value: Iterable[float], name: str = "vector") -> Vector3:
    result = vector3(value, name)
    length = magnitude(result)
    if length <= _EPSILON:
        raise GeometryResolutionError(f"{name} must not be a zero vector.")
    return tuple(item / length for item in result)  # type: ignore[return-value]


def subtract(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]


def add(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def scale(value: Vector3, factor: float) -> Vector3:
    return tuple(item * factor for item in value)  # type: ignore[return-value]


@dataclass(frozen=True)
class LocalCoordinateFrame:
    """Right-handed planar frame. Origin is in meters; axes are unit vectors."""

    origin_m: Vector3
    u_axis: Vector3
    v_axis: Vector3
    normal: Vector3

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin_m", vector3(self.origin_m, "origin_m"))
        object.__setattr__(self, "u_axis", unit(self.u_axis, "u_axis"))
        object.__setattr__(self, "v_axis", unit(self.v_axis, "v_axis"))
        object.__setattr__(self, "normal", unit(self.normal, "normal"))
        if any(
            abs(value) > 1e-7
            for value in (
                dot(self.u_axis, self.v_axis),
                dot(self.u_axis, self.normal),
                dot(self.v_axis, self.normal),
            )
        ):
            raise GeometryResolutionError("Local frame axes must be orthogonal.")
        if dot(cross(self.u_axis, self.v_axis), self.normal) < 1.0 - 1e-7:
            raise GeometryResolutionError("Local frame must satisfy U x V = N.")

    @classmethod
    def from_plane(
        cls,
        plane_point_m: Iterable[float],
        normal: Iterable[float],
        *,
        preferred_origin_m: Iterable[float] | None = None,
        preferred_u_axis: Iterable[float] | None = None,
    ) -> "LocalCoordinateFrame":
        plane_point = vector3(plane_point_m, "plane_point_m")
        n_axis = unit(normal, "normal")
        preferred_origin = (
            vector3(preferred_origin_m, "preferred_origin_m")
            if preferred_origin_m is not None
            else plane_point
        )
        signed_distance = dot(subtract(preferred_origin, plane_point), n_axis)
        origin = subtract(preferred_origin, scale(n_axis, signed_distance))

        u_axis = cls._projected_axis(preferred_u_axis, n_axis)
        v_axis = unit(cross(n_axis, u_axis), "v_axis")
        return cls(origin_m=origin, u_axis=u_axis, v_axis=v_axis, normal=n_axis)

    @staticmethod
    def _projected_axis(
        preferred: Iterable[float] | None,
        normal: Vector3,
    ) -> Vector3:
        candidates: list[Vector3] = []
        if preferred is not None:
            candidates.append(vector3(preferred, "preferred_u_axis"))
        candidates.extend(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        for candidate in candidates:
            projected = subtract(candidate, scale(normal, dot(candidate, normal)))
            if magnitude(projected) > _EPSILON:
                return unit(projected, "u_axis")
        raise GeometryResolutionError("Could not derive a stable in-plane U axis.")

    def world_from_local_m(self, u_m: float, v_m: float, n_m: float = 0.0) -> Vector3:
        return add(
            self.origin_m,
            add(scale(self.u_axis, float(u_m)), add(scale(self.v_axis, float(v_m)), scale(self.normal, float(n_m)))),
        )

    def world_from_local_mm(self, u_mm: float, v_mm: float, n_mm: float = 0.0) -> Vector3:
        return self.world_from_local_m(u_mm / 1000.0, v_mm / 1000.0, n_mm / 1000.0)

    def local_m_from_world(self, point_m: Iterable[float]) -> Vector3:
        delta = subtract(vector3(point_m, "point_m"), self.origin_m)
        return dot(delta, self.u_axis), dot(delta, self.v_axis), dot(delta, self.normal)

    def local_mm_from_world(self, point_m: Iterable[float]) -> Vector3:
        return tuple(value * 1000.0 for value in self.local_m_from_world(point_m))  # type: ignore[return-value]

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin_m": list(self.origin_m),
            "origin_mm": [value * 1000.0 for value in self.origin_m],
            "u_axis": list(self.u_axis),
            "v_axis": list(self.v_axis),
            "normal": list(self.normal),
            "handedness": "right_handed",
            "length_unit": "m",
        }


@dataclass(frozen=True)
class GeometryCandidate:
    signature: dict[str, Any]
    score: float = 0.0
    matched_constraints: tuple[str, ...] = ()
    mismatched_constraints: tuple[str, ...] = ()
    entity: Any | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "score": round(self.score, 6),
            "matched_constraints": list(self.matched_constraints),
            "mismatched_constraints": list(self.mismatched_constraints),
            "entity_resolved": self.entity is not None,
        }


@dataclass(frozen=True)
class GeometryResolution:
    success: bool
    reason: str
    message: str
    source: str
    reference_id: str = ""
    score: float = 0.0
    ambiguity_margin: float | None = None
    candidate_count: int = 0
    expected_signature: dict[str, Any] = field(default_factory=dict)
    resolved_signature: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[GeometryCandidate, ...] = ()
    persistent_resolution: dict[str, Any] | None = None
    frame: LocalCoordinateFrame | None = None
    entity: Any | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "reason": self.reason,
            "message": self.message,
            "source": self.source,
            "reference_id": self.reference_id,
            "score": round(self.score, 6),
            "ambiguity_margin": (
                round(self.ambiguity_margin, 6) if self.ambiguity_margin is not None else None
            ),
            "candidate_count": self.candidate_count,
            "expected_signature": self.expected_signature,
            "resolved_signature": self.resolved_signature,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "persistent_resolution": self.persistent_resolution,
            "local_frame": self.frame.as_dict() if self.frame else None,
            "entity_resolved": self.entity is not None,
        }
