from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.geometry_context import FaceContext
from cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor


def _vertical_flange_context() -> dict:
    return {
        "target_body": "primary_solid",
        "target_feature": "edge_flange_1",
        "target_face_role": "outer_vertical_face",
        "face_normal": [1, 0, 0],
        "face_origin": [38.5, 32.5, 0],
        "local_u_axis": [0, 1, 0],
        "local_v_axis": [0, 0, 1],
        "placement_uv_mm": [[0, 0]],
    }


def test_face_context_maps_uv_to_vertical_flange_model_point() -> None:
    context = FaceContext.from_request({"geometry_context": _vertical_flange_context()})
    assert context.model_point_m([0, 0]) == [0.0385, 0.0325, 0.0]
    assert context.model_point_m([10, -5]) == [0.0385, 0.0425, -0.005]


def test_face_context_rejects_ambiguous_or_non_orthogonal_target() -> None:
    with pytest.raises(ValueError, match="geometry_context is required"):
        FaceContext.from_request({})
    invalid = _vertical_flange_context()
    invalid["local_u_axis"] = [1, 0, 0]
    with pytest.raises(ValueError, match="orthogonal"):
        FaceContext.from_request({"geometry_context": invalid})


class _Plane:
    IsPlane = True


class _Face:
    def __init__(self, box: list[float]) -> None:
        self._box = box

    def GetBox(self) -> list[float]:
        return self._box

    def GetSurface(self) -> _Plane:
        return _Plane()


class _Body:
    def __init__(self, faces: list[_Face]) -> None:
        self._faces = faces

    def GetFaces(self) -> list[_Face]:
        return self._faces


def test_semantic_resolver_selects_outer_vertical_flange_not_top_face() -> None:
    context = FaceContext.from_request({"geometry_context": _vertical_flange_context()})
    top_face = _Face([-0.0325, -0.003, 0.0325, 0.0385, 0.065, 0.0325])
    vertical_face = _Face([0.0385, 0.0, -0.0325, 0.0385, 0.065, 0.0325])
    result = ActiveModelThroughHoleExecutor._resolve_semantic_face([_Body([top_face, vertical_face])], context)
    assert result is not None
    _face, signature = result
    assert signature["face_index"] == 1
    assert signature["target_face_role"] == "outer_vertical_face"


def test_intersection_precheck_blocks_floating_sketch_point() -> None:
    context = FaceContext.from_request({"geometry_context": _vertical_flange_context()})
    bbox = {"xmin": -0.0325, "xmax": 0.0385, "ymin": -0.003, "ymax": 0.065, "zmin": -0.0325, "zmax": 0.0325}
    assert ActiveModelThroughHoleExecutor._intersection_precheck(bbox, [[0.0385, 0.0325, 0.0]], context)["success"]
    assert not ActiveModelThroughHoleExecutor._intersection_precheck(bbox, [[0.12, 0.0325, 0.0]], context)["success"]
