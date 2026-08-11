from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Iterable

from ..feature_reference_registry import ResolvedReference, SolidWorksPersistentReferenceService
from .models import (
    GeometryCandidate,
    GeometryResolution,
    GeometryResolutionError,
    LocalCoordinateFrame,
    dot,
    unit,
    vector3,
)
from .solidworks_adapter import SolidWorksFaceInspector


class FaceGeometryResolver:
    """Resolve a face by token first, then by a guarded and non-guessing signature fallback."""

    SAFE_PERSISTENT_FALLBACK_REASONS = {
        "persistent_object_not_found",
        "persistent_object_stale",
    }

    def __init__(
        self,
        *,
        persistent_service: SolidWorksPersistentReferenceService | None = None,
        inspector: SolidWorksFaceInspector | None = None,
        minimum_score: float = 0.85,
        minimum_margin: float = 0.08,
        linear_tolerance_m: float = 0.00025,
        normal_tolerance_deg: float = 2.0,
        area_relative_tolerance: float = 0.03,
    ) -> None:
        self.persistent_service = persistent_service or SolidWorksPersistentReferenceService()
        self.inspector = inspector or SolidWorksFaceInspector()
        self.minimum_score = float(minimum_score)
        self.minimum_margin = float(minimum_margin)
        self.linear_tolerance_m = float(linear_tolerance_m)
        self.normal_cosine = math.cos(math.radians(float(normal_tolerance_deg)))
        self.area_relative_tolerance = float(area_relative_tolerance)

    def resolve(
        self,
        *,
        model_doc: Any | None,
        bodies: Iterable[Any],
        reference: ResolvedReference | None = None,
        expected_signature: dict[str, Any] | None = None,
        document_id: str | None = None,
        allow_geometry_fallback: bool = True,
        preferred_origin_m: Iterable[float] | None = None,
        preferred_u_axis: Iterable[float] | None = None,
    ) -> GeometryResolution:
        reference_id = reference.reference_id if reference else ""
        expected = dict(reference.geometry_signature if reference else {})
        expected.update(expected_signature or {})
        persistent_report: dict[str, Any] | None = None

        if reference is not None and reference.persistent_token:
            if model_doc is None:
                return self._failure(
                    "persistent_model_missing",
                    "A ModelDoc2 object is required to resolve the persistent token.",
                    reference_id,
                    expected,
                )
            persistent = self.persistent_service.resolve(
                reference,
                model_doc,
                document_id=document_id,
            )
            persistent_report = persistent.as_dict()
            if persistent.success and persistent.entity is not None:
                try:
                    signature = self.inspector.inspect_face(persistent.entity)
                    frame = self._frame(signature, preferred_origin_m, preferred_u_axis)
                except GeometryResolutionError as exc:
                    return self._failure(
                        "persistent_entity_invalid",
                        str(exc),
                        reference_id,
                        expected,
                        persistent_report=persistent_report,
                    )
                return GeometryResolution(
                    success=True,
                    reason="persistent_reference_resolved",
                    message="Resolved the requested face from its SolidWorks persistent token.",
                    source="persistent_token",
                    reference_id=reference_id,
                    score=1.0,
                    candidate_count=1,
                    expected_signature=expected,
                    resolved_signature=signature,
                    persistent_resolution=persistent_report,
                    frame=frame,
                    entity=persistent.entity,
                )
            if persistent.reason not in self.SAFE_PERSISTENT_FALLBACK_REASONS:
                return self._failure(
                    "persistent_reference_guard_failed",
                    (
                        "Persistent reference failure is not eligible for geometry fallback: "
                        f"{persistent.reason}."
                    ),
                    reference_id,
                    expected,
                    persistent_report=persistent_report,
                )
            if not allow_geometry_fallback:
                return self._failure(
                    "persistent_reference_failed",
                    "Persistent reference failed and geometry fallback is disabled.",
                    reference_id,
                    expected,
                    persistent_report=persistent_report,
                )

        if not allow_geometry_fallback:
            return self._failure(
                "geometry_fallback_disabled",
                "The reference has no usable token and geometry fallback is disabled.",
                reference_id,
                expected,
                persistent_report=persistent_report,
            )
        if not expected:
            return self._failure(
                "geometry_signature_missing",
                "A geometry signature is required when no persistent token can be resolved.",
                reference_id,
                expected,
                persistent_report=persistent_report,
            )

        raw_candidates = self.inspector.enumerate_planar_faces(bodies)
        scored = tuple(self._score(candidate, expected) for candidate in raw_candidates)
        eligible = sorted(
            (candidate for candidate in scored if not candidate.mismatched_constraints),
            key=lambda item: item.score,
            reverse=True,
        )
        if not eligible:
            return self._failure(
                "geometry_match_not_found",
                "No planar face satisfies the supplied geometry signature.",
                reference_id,
                expected,
                candidates=scored,
                persistent_report=persistent_report,
            )
        top = eligible[0]
        if top.score < self.minimum_score:
            return self._failure(
                "geometry_match_below_threshold",
                f"Best geometry candidate score {top.score:.3f} is below {self.minimum_score:.3f}.",
                reference_id,
                expected,
                candidates=tuple(eligible),
                persistent_report=persistent_report,
            )
        margin = top.score - eligible[1].score if len(eligible) > 1 else None
        if len(eligible) > 1 and margin is not None and margin < self.minimum_margin:
            return GeometryResolution(
                success=False,
                reason="geometry_match_ambiguous",
                message=(
                    f"The top two geometry candidates differ by {margin:.3f}; "
                    f"at least {self.minimum_margin:.3f} is required."
                ),
                source="geometry_signature",
                reference_id=reference_id,
                score=top.score,
                ambiguity_margin=margin,
                candidate_count=len(eligible),
                expected_signature=expected,
                candidates=tuple(eligible),
                persistent_resolution=persistent_report,
            )
        try:
            frame = self._frame(top.signature, preferred_origin_m, preferred_u_axis)
        except GeometryResolutionError as exc:
            return self._failure(
                "local_frame_failed",
                str(exc),
                reference_id,
                expected,
                candidates=tuple(eligible),
                persistent_report=persistent_report,
            )
        return GeometryResolution(
            success=True,
            reason="geometry_signature_resolved",
            message="Resolved one unambiguous planar face from the geometry signature.",
            source="geometry_signature",
            reference_id=reference_id,
            score=top.score,
            ambiguity_margin=margin,
            candidate_count=len(eligible),
            expected_signature=expected,
            resolved_signature=top.signature,
            candidates=tuple(eligible),
            persistent_resolution=persistent_report,
            frame=frame,
            entity=top.entity,
        )

    def _score(self, candidate: GeometryCandidate, expected: dict[str, Any]) -> GeometryCandidate:
        signature = candidate.signature
        weighted: list[tuple[str, float, float, bool]] = []

        if "planar" in expected:
            matched = bool(signature.get("planar")) is bool(expected["planar"])
            weighted.append(("planar", 0.05, 1.0 if matched else 0.0, not matched))
        if expected.get("surface_type"):
            matched = str(signature.get("surface_type")) == str(expected["surface_type"])
            weighted.append(("surface_type", 0.05, 1.0 if matched else 0.0, not matched))
        if expected.get("body_index") is not None:
            matched = int(signature.get("body_index", -1)) == int(expected["body_index"])
            weighted.append(("body_index", 0.15, 1.0 if matched else 0.0, not matched))
        if expected.get("body_name"):
            matched = str(signature.get("body_name", "")) == str(expected["body_name"])
            weighted.append(("body_name", 0.15, 1.0 if matched else 0.0, not matched))
        if expected.get("feature_name"):
            matched = str(signature.get("feature_name", "")) == str(expected["feature_name"])
            weighted.append(("feature_name", 0.20, 1.0 if matched else 0.0, not matched))

        expected_normal = expected.get("normal", expected.get("face_normal"))
        if expected_normal is not None:
            try:
                alignment = dot(unit(expected_normal, "expected_normal"), unit(signature["normal"], "candidate_normal"))
            except (GeometryResolutionError, KeyError):
                alignment = -1.0
            matched = alignment >= self.normal_cosine
            weighted.append(("normal", 0.25, max(0.0, alignment), not matched))
        expected_axis = expected.get("normal_axis", expected.get("face_normal_axis"))
        if expected_axis is not None:
            matched = int(signature.get("normal_axis", -1)) == int(expected_axis)
            weighted.append(("normal_axis", 0.10, 1.0 if matched else 0.0, not matched))

        expected_origin = expected.get("plane_origin_m", expected.get("face_origin_m"))
        if expected_origin is not None:
            try:
                candidate_normal = unit(signature["normal"], "candidate_normal")
                candidate_origin = vector3(signature["plane_origin_m"], "candidate_origin")
                requested_origin = vector3(expected_origin, "expected_origin")
                distance = abs(dot(
                    tuple(requested_origin[index] - candidate_origin[index] for index in range(3)),
                    candidate_normal,
                ))
                value = max(0.0, 1.0 - distance / self.linear_tolerance_m)
                matched = distance <= self.linear_tolerance_m
            except (GeometryResolutionError, KeyError):
                value, matched = 0.0, False
            weighted.append(("plane_origin_m", 0.25, value, not matched))
        if expected.get("plane_coordinate_m") is not None:
            distance = abs(float(signature.get("plane_coordinate_m", math.inf)) - float(expected["plane_coordinate_m"]))
            matched = distance <= self.linear_tolerance_m
            weighted.append(("plane_coordinate_m", 0.20, max(0.0, 1.0 - distance / self.linear_tolerance_m), not matched))

        expected_box = expected.get("face_box_m", expected.get("bbox_m"))
        if expected_box is not None:
            try:
                left = tuple(float(item) for item in expected_box)
                right = tuple(float(item) for item in signature["face_box_m"])
                delta = max(abs(a - b) for a, b in zip(left, right)) if len(left) == len(right) == 6 else math.inf
                matched = delta <= self.linear_tolerance_m
                value = max(0.0, 1.0 - delta / self.linear_tolerance_m)
            except (TypeError, ValueError, KeyError):
                value, matched = 0.0, False
            weighted.append(("face_box_m", 0.30, value, not matched))

        exact_area_requested = expected.get("area_m2") is not None
        expected_area = expected.get("area_m2", expected.get("approximate_area_m2"))
        if expected_area is not None:
            target = abs(float(expected_area))
            candidate_area_key = "area_m2" if exact_area_requested else "approximate_area_m2"
            actual = abs(float(signature.get(candidate_area_key, 0.0)))
            relative = abs(actual - target) / max(target, 1e-12)
            matched = relative <= self.area_relative_tolerance
            value = max(0.0, 1.0 - relative / self.area_relative_tolerance)
            weighted.append((candidate_area_key, 0.20, value, not matched))

        if not weighted:
            return replace(candidate, score=0.0, mismatched_constraints=("no_supported_constraints",))
        total_weight = sum(weight for _name, weight, _value, _hard in weighted)
        score = sum(weight * value for _name, weight, value, _hard in weighted) / total_weight
        matched_names = tuple(name for name, _weight, value, hard in weighted if value > 0.0 and not hard)
        mismatched_names = tuple(name for name, _weight, _value, hard in weighted if hard)
        return replace(
            candidate,
            score=score,
            matched_constraints=matched_names,
            mismatched_constraints=mismatched_names,
        )

    @staticmethod
    def _frame(
        signature: dict[str, Any],
        preferred_origin_m: Iterable[float] | None,
        preferred_u_axis: Iterable[float] | None,
    ) -> LocalCoordinateFrame:
        return LocalCoordinateFrame.from_plane(
            signature["plane_origin_m"],
            signature["normal"],
            preferred_origin_m=preferred_origin_m,
            preferred_u_axis=preferred_u_axis,
        )

    @staticmethod
    def _failure(
        reason: str,
        message: str,
        reference_id: str,
        expected: dict[str, Any],
        *,
        candidates: tuple[GeometryCandidate, ...] = (),
        persistent_report: dict[str, Any] | None = None,
    ) -> GeometryResolution:
        return GeometryResolution(
            success=False,
            reason=reason,
            message=message,
            source="geometry_signature",
            reference_id=reference_id,
            candidate_count=len(candidates),
            expected_signature=expected,
            candidates=candidates,
            persistent_resolution=persistent_report,
        )
