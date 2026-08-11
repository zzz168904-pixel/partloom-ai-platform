from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.feature_reference_registry import (
    ResolvedReference,
    SolidWorksPersistentReferenceService,
    encode_persistent_token,
)
from cad_agent.geometry_resolver import (
    FaceGeometryResolver,
    GeometryResolutionError,
    LocalCoordinateFrame,
    SolidWorksFaceInspector,
    cross,
    dot,
)


class FakeSurface:
    def __init__(self, normal=(0.0, 0.0, 1.0), origin=(0.0, 0.0, 0.0), planar=True) -> None:
        self.IsPlane = planar
        self.PlaneParams = (*normal, *origin)


class FakeFeature:
    def __init__(self, name: str) -> None:
        self.Name = name


class FakeFace:
    def __init__(
        self,
        box,
        *,
        normal=(0.0, 0.0, 1.0),
        origin=(0.0, 0.0, 0.0),
        area=None,
        feature_name="BasePlate",
        planar=True,
    ) -> None:
        self._box = list(box)
        self.Normal = normal if planar else (0.0, 0.0, 0.0)
        self._surface = FakeSurface(normal, origin, planar)
        self._area = area
        self._feature = FakeFeature(feature_name)

    def GetBox(self):
        return self._box

    def GetSurface(self):
        return self._surface

    def GetArea(self):
        return self._area

    def GetFeature(self):
        return self._feature


class FakeBody:
    def __init__(self, faces, name="Body-1") -> None:
        self._faces = list(faces)
        self.Name = name

    def GetFaces(self):
        return self._faces


class FakeModel:
    GetPathName = r"C:\models\plate.SLDPRT"


class FakePersistentBackend:
    def __init__(self, entity, error_code=0) -> None:
        self.entity = entity
        self.error_code = error_code
        self.resolve_calls = 0

    def capture(self, model_doc, entity):
        return b"token"

    def resolve(self, model_doc, token):
        self.resolve_calls += 1
        return self.entity, self.error_code


def _reference(*, token=None, signature=None, model_path=FakeModel.GetPathName) -> ResolvedReference:
    metadata = {}
    if token:
        metadata = {"persistent_reference": {"model_path": model_path}}
    return ResolvedReference(
        reference_id="doc_01:base_01:outer_planar_face",
        document_id="doc_01",
        feature_id="base_01",
        role="outer_planar_face",
        entity_type="Face",
        body_id="body_01",
        persistent_token=token,
        geometry_signature=signature or {},
        state="resolved" if token else "semantic",
        metadata=metadata,
    )


def _top_face(z=0.01, *, box=None, area=0.006, feature_name="BasePlate") -> FakeFace:
    return FakeFace(
        box or (-0.05, -0.03, z, 0.05, 0.03, z),
        normal=(0.0, 0.0, 1.0),
        origin=(0.0, 0.0, z),
        area=area,
        feature_name=feature_name,
    )


def _bottom_face(z=0.0) -> FakeFace:
    return FakeFace(
        (-0.05, -0.03, z, 0.05, 0.03, z),
        normal=(0.0, 0.0, -1.0),
        origin=(0.0, 0.0, z),
        area=0.006,
    )


def test_local_frame_projects_origin_and_is_right_handed() -> None:
    frame = LocalCoordinateFrame.from_plane(
        (0.0, 0.0, 0.01),
        (0.0, 0.0, 1.0),
        preferred_origin_m=(0.02, -0.01, 0.014),
        preferred_u_axis=(1.0, 0.0, 0.0),
    )

    assert frame.origin_m == pytest.approx((0.02, -0.01, 0.01))
    assert dot(cross(frame.u_axis, frame.v_axis), frame.normal) == pytest.approx(1.0)


def test_local_world_round_trip_uses_millimeters_at_boundary() -> None:
    frame = LocalCoordinateFrame.from_plane((0.01, 0.0, 0.0), (1.0, 0.0, 0.0))

    world = frame.world_from_local_mm(12.5, -7.25, 0.5)

    assert frame.local_mm_from_world(world) == pytest.approx((12.5, -7.25, 0.5))


def test_local_frame_rejects_left_handed_axes() -> None:
    with pytest.raises(GeometryResolutionError, match="U x V"):
        LocalCoordinateFrame(
            origin_m=(0, 0, 0),
            u_axis=(1, 0, 0),
            v_axis=(0, -1, 0),
            normal=(0, 0, 1),
        )


def test_inspector_uses_face_normal_plane_params_area_and_feature() -> None:
    signature = SolidWorksFaceInspector().inspect_face(
        _top_face(),
        body_index=2,
        face_index=4,
        body=FakeBody([], "MainBody"),
    )

    assert signature["planar"] is True
    assert signature["normal"] == [0.0, 0.0, 1.0]
    assert signature["plane_origin_m"] == [0.0, 0.0, 0.01]
    assert signature["area_m2"] == pytest.approx(0.006)
    assert signature["feature_name"] == "BasePlate"
    assert signature["body_name"] == "MainBody"


def test_inspector_supports_dynamic_dispatch_property_shape_without_getsurface() -> None:
    class DynamicDispatchFace:
        GetBox = (-0.05, -0.03, 0.01, 0.05, 0.03, 0.01)
        Normal = (0.0, 0.0, 1.0)
        GetArea = 0.006

    signature = SolidWorksFaceInspector().inspect_face(DynamicDispatchFace())

    assert signature["planar"] is True
    assert signature["plane_origin_m"] == pytest.approx([0.0, 0.0, 0.01])


def test_geometry_fallback_selects_unique_face_by_normal_and_plane() -> None:
    top = _top_face()
    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([_bottom_face(), top])],
        expected_signature={"planar": True, "normal": [0, 0, 1], "plane_coordinate_m": 0.01},
        preferred_origin_m=(0.0, 0.0, 0.01),
        preferred_u_axis=(1.0, 0.0, 0.0),
    )

    assert result.success
    assert result.entity is top
    assert result.source == "geometry_signature"
    assert result.frame is not None


def test_geometry_fallback_blocks_equal_coplanar_candidates() -> None:
    left = _top_face(box=(-0.05, -0.03, 0.01, -0.005, 0.03, 0.01), area=0.0027)
    right = _top_face(box=(0.005, -0.03, 0.01, 0.05, 0.03, 0.01), area=0.0027)

    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([left, right])],
        expected_signature={"planar": True, "normal": [0, 0, 1], "plane_coordinate_m": 0.01},
    )

    assert not result.success
    assert result.reason == "geometry_match_ambiguous"
    assert result.entity is None
    assert result.candidate_count == 2


def test_bbox_signature_disambiguates_coplanar_faces() -> None:
    left_box = (-0.05, -0.03, 0.01, -0.005, 0.03, 0.01)
    left = _top_face(box=left_box, area=0.0027)
    right = _top_face(box=(0.005, -0.03, 0.01, 0.05, 0.03, 0.01), area=0.0027)

    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([left, right])],
        expected_signature={"planar": True, "normal": [0, 0, 1], "face_box_m": left_box},
    )

    assert result.success
    assert result.entity is left


def test_body_and_feature_provenance_are_hard_constraints() -> None:
    first = _top_face(feature_name="Boss1")
    second = _top_face(feature_name="BasePlate")
    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([first], "ToolBody"), FakeBody([second], "MainBody")],
        expected_signature={
            "planar": True,
            "body_index": 1,
            "body_name": "MainBody",
            "feature_name": "BasePlate",
            "normal": [0, 0, 1],
        },
    )

    assert result.success
    assert result.entity is second


def test_missing_geometry_signature_never_guesses() -> None:
    result = FaceGeometryResolver().resolve(model_doc=None, bodies=[FakeBody([_top_face()])])

    assert not result.success
    assert result.reason == "geometry_signature_missing"


def test_wrong_normal_is_rejected_before_execution() -> None:
    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([_top_face()])],
        expected_signature={"normal": [1, 0, 0]},
    )

    assert not result.success
    assert result.reason == "geometry_match_not_found"


def test_non_planar_faces_are_not_fallback_candidates() -> None:
    curved = FakeFace(
        (-0.05, -0.03, 0.0, 0.05, 0.03, 0.01),
        normal=(0, 0, 1),
        origin=(0, 0, 0),
        planar=False,
    )
    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([curved])],
        expected_signature={"planar": True},
    )

    assert not result.success
    assert result.reason == "geometry_match_not_found"


def test_persistent_token_wins_without_geometry_enumeration() -> None:
    face = _top_face()
    backend = FakePersistentBackend(face)
    resolver = FaceGeometryResolver(
        persistent_service=SolidWorksPersistentReferenceService(backend),
    )
    reference = _reference(
        token=encode_persistent_token(b"token"),
        signature={"normal": [1, 0, 0]},
    )

    result = resolver.resolve(
        model_doc=FakeModel(),
        bodies=[],
        reference=reference,
        document_id="doc_01",
    )

    assert result.success
    assert result.source == "persistent_token"
    assert result.entity is face
    assert backend.resolve_calls == 1


def test_stale_token_can_fallback_to_one_matching_signature() -> None:
    face = _top_face()
    backend = FakePersistentBackend(None, error_code=2)
    resolver = FaceGeometryResolver(
        persistent_service=SolidWorksPersistentReferenceService(backend),
    )
    reference = _reference(
        token=encode_persistent_token(b"token"),
        signature={"normal": [0, 0, 1], "plane_coordinate_m": 0.01},
    )

    result = resolver.resolve(
        model_doc=FakeModel(),
        bodies=[FakeBody([face])],
        reference=reference,
        document_id="doc_01",
    )

    assert result.success
    assert result.source == "geometry_signature"
    assert result.entity is face
    assert result.persistent_resolution["reason"] == "persistent_object_stale"


def test_cross_model_token_failure_cannot_fallback() -> None:
    face = _top_face()
    backend = FakePersistentBackend(face)
    resolver = FaceGeometryResolver(
        persistent_service=SolidWorksPersistentReferenceService(backend),
    )
    reference = _reference(
        token=encode_persistent_token(b"token"),
        signature={"normal": [0, 0, 1], "plane_coordinate_m": 0.01},
        model_path=r"C:\models\different.SLDPRT",
    )

    result = resolver.resolve(
        model_doc=FakeModel(),
        bodies=[FakeBody([face])],
        reference=reference,
        document_id="doc_01",
    )

    assert not result.success
    assert result.reason == "persistent_reference_guard_failed"
    assert backend.resolve_calls == 0


def test_stale_token_fallback_can_be_disabled() -> None:
    backend = FakePersistentBackend(None, error_code=4)
    resolver = FaceGeometryResolver(
        persistent_service=SolidWorksPersistentReferenceService(backend),
    )
    result = resolver.resolve(
        model_doc=FakeModel(),
        bodies=[FakeBody([_top_face()])],
        reference=_reference(
            token=encode_persistent_token(b"token"),
            signature={"normal": [0, 0, 1]},
        ),
        document_id="doc_01",
        allow_geometry_fallback=False,
    )

    assert not result.success
    assert result.reason == "persistent_reference_failed"


def test_resolution_report_never_serializes_live_com_entity() -> None:
    result = FaceGeometryResolver().resolve(
        model_doc=None,
        bodies=[FakeBody([_top_face()])],
        expected_signature={"normal": [0, 0, 1], "plane_coordinate_m": 0.01},
    )

    report = result.as_dict()
    assert report["entity_resolved"] is True
    assert "entity" not in report
    assert all("entity" not in candidate for candidate in report["candidates"])


def test_normal_tolerance_rejects_excessive_angle() -> None:
    angle = math.radians(5.0)
    tilted = FakeFace(
        (-0.05, -0.03, 0.01, 0.05, 0.03, 0.01),
        normal=(math.sin(angle), 0.0, math.cos(angle)),
        origin=(0.0, 0.0, 0.01),
        area=0.006,
    )
    result = FaceGeometryResolver(normal_tolerance_deg=2.0).resolve(
        model_doc=None,
        bodies=[FakeBody([tilted])],
        expected_signature={"normal": [0, 0, 1]},
    )

    assert not result.success
    assert result.reason == "geometry_match_not_found"
