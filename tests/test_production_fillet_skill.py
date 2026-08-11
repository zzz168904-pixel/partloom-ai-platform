from __future__ import annotations

from types import SimpleNamespace

from src.cad_agent.models import SkillResult
from src.cad_agent.production_fillet_skill import ProductionFilletSkill
from src.cad_agent.isolated_feature_fillet import _feature_by_name
from src.cad_agent.pipeline.pipeline_executor import PipelineExecutor
from src.cad_agent.stage_planner import _supported_fillet


class FakeFeature:
    def __init__(self, name: str, next_feature: "FakeFeature | None" = None) -> None:
        self.Name = name
        self._next = next_feature
        self.selected = False

    def GetNextFeature(self):
        return self._next

    def Select2(self, append: bool, mark: int) -> bool:
        assert append is False
        assert mark == 0
        self.selected = True
        return True


class FakeModel:
    def __init__(self, first: FakeFeature | None) -> None:
        self.FirstFeature = first
        self.clear_calls = 0

    def ClearSelection2(self, clear_all: bool) -> None:
        assert clear_all is True
        self.clear_calls += 1


class DirectLookupModel(FakeModel):
    def __init__(self, target: FakeFeature) -> None:
        super().__init__(FakeFeature("unrelated"))
        self.target = target

    def FeatureByName(self, name: str):
        return self.target if name.casefold() == self.target.Name.casefold() else None


class FakeApp:
    def __init__(self, opened) -> None:
        self.opened = opened
        self.ActiveDoc = opened
        self.closed = []

    def CloseDoc(self, title: str) -> None:
        self.closed.append(title)

    def OpenDoc6(self, path: str, doc_type: int, options: int, configuration: str, errors: int, warnings: int):
        assert path.endswith("task.SLDPRT")
        assert (doc_type, options, configuration, errors, warnings) == (1, 1, "", 0, 0)
        return self.opened, 0, 0

    def ActivateDoc3(self, title: str, rebuild: bool, doc_type: int, errors: int):
        assert (title, rebuild, doc_type, errors) == ("task.SLDPRT", False, 1, 0)
        return self.opened, 0


class FakeSavedModel:
    GetTitle = "task.SLDPRT"

    def Save(self) -> bool:
        return True


class FakeCircularCurve:
    def __init__(self, circle_params) -> None:
        self.CircleParams = circle_params


class FakeCircularEdge:
    def __init__(self, circle_params) -> None:
        self._curve = FakeCircularCurve(circle_params)
        self.selected = False

    def GetCurve(self):
        return self._curve

    def Select2(self, append: bool, mark: int) -> bool:
        assert mark == 0
        self.selected = True
        return True


class FakeVertex:
    def __init__(self, point) -> None:
        self.GetPoint = point


class FakeSignatureCurve:
    def __init__(self, circle_params=None) -> None:
        self.CircleParams = circle_params


class FakeSignatureEdge:
    def __init__(self, start, end, circle_params=None) -> None:
        self._start = FakeVertex(start)
        self._end = FakeVertex(end)
        self._curve = FakeSignatureCurve(circle_params)
        self.selected = False
        self.append = None

    def GetStartVertex(self):
        return self._start

    def GetEndVertex(self):
        return self._end

    def GetCurve(self):
        return self._curve

    def Select2(self, append: bool, mark: int) -> bool:
        assert mark == 0
        self.selected = True
        self.append = append
        return True


class FakeBody:
    def __init__(self, edges) -> None:
        self._edges = edges

    def GetEdges(self):
        return self._edges


def test_feature_edge_selector_selects_named_feature_case_insensitively() -> None:
    target = FakeFeature("wing_nut_wings")
    model = FakeModel(FakeFeature("wing_nut_body", target))

    assert ProductionFilletSkill._select_feature_by_name(model, "WING_NUT_WINGS") is True
    assert target.selected is True
    assert model.clear_calls == 1


def test_feature_edge_selector_rejects_missing_target_feature() -> None:
    model = FakeModel(FakeFeature("wing_nut_body"))

    assert ProductionFilletSkill._select_feature_by_name(model, "missing") is False


def test_isolated_selector_finds_same_stable_feature_id() -> None:
    target = FakeFeature("wing_nut_wings")
    model = FakeModel(FakeFeature("wing_nut_body", target))

    assert _feature_by_name(model, "WING_NUT_WINGS") is target


def test_feature_edge_selectors_prefer_native_feature_by_name() -> None:
    target = FakeFeature("bearing_outer_ring")
    model = DirectLookupModel(target)

    assert ProductionFilletSkill._select_feature_by_name(model, "bearing_outer_ring") is True
    assert _feature_by_name(model, "bearing_outer_ring") is target
    assert target.selected is True


def test_feature_edge_selector_requires_semantic_target_reference() -> None:
    params = {"radius": 0.2, "edge_selector": "all_feature_edges"}

    assert _supported_fillet({"params": params}, params) is False
    assert _supported_fillet(
        {"params": params, "target_reference": {"feature_id": "wing_nut_wings", "role": "selected_edges"}},
        params,
    ) is True


def test_legacy_outer_corner_fillet_remains_supported() -> None:
    params = {"radius": 5.0, "target": "four_outer_corners"}

    assert ProductionFilletSkill._edge_selector(params) == "outer_vertical_edges"
    assert _supported_fillet({"params": params}, params) is True


def test_axial_bearing_selector_keeps_bore_and_od_end_edges_only() -> None:
    target_circles = [
        (-0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.015),
        (-0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.031),
        (0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.015),
        (0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.031),
    ]
    excluded_circles = [
        (-0.002967994085, 0.0, 0.0, -1.0, 0.0, 0.0, 0.025666666667),
        (0.002967994085, 0.0, 0.0, -1.0, 0.0, 0.0, 0.025666666667),
        (-0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.020333333333),
        (0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.020333333333),
        (-0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.025666666667),
        (0.008, 0.0, 0.0, -1.0, 0.0, 0.0, 0.025666666667),
    ]
    targets = [FakeCircularEdge(value) for value in target_circles]
    excluded = [FakeCircularEdge(value) for value in excluded_circles]
    model = FakeModel(None)
    bbox = {
        "xmin": -0.008,
        "xmax": 0.008,
        "ymin": -0.031,
        "ymax": 0.031,
        "zmin": -0.031,
        "zmax": 0.031,
    }

    selected = ProductionFilletSkill._select_axial_bearing_boundary_end_edges(
        model,
        [FakeBody(targets + excluded)],
        bbox,
        axis="x",
    )

    assert selected == 4
    assert all(edge.selected for edge in targets)
    assert not any(edge.selected for edge in excluded)
    assert ProductionFilletSkill._edge_selector({"edge_selector": "bearing_end_edges"}) == "axial_bearing_boundary_end_edges"
    assert _supported_fillet(
        {"params": {"radius": 1.0, "edge_selector": "axial_bearing_boundary_end_edges", "axis": "x"}},
        {"radius": 1.0, "edge_selector": "axial_bearing_boundary_end_edges", "axis": "x"},
    ) is True


def test_explicit_edge_signatures_select_exact_line_and_arc() -> None:
    line = FakeSignatureEdge((0.008, 0.0, 0.0), (-0.008, 0.0, 0.0))
    arc = FakeSignatureEdge(
        (0.008, 0.012857556682, -0.018464377492),
        (0.008, 0.018568029721, 0.012707410132),
        (0.008, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0225),
    )
    unrelated = FakeSignatureEdge((0.0, 0.0, 0.0), (0.0, 0.01, 0.0))
    model = FakeModel(None)
    signatures = [
        {
            "curve_type": "line",
            "start_mm": [-8.0, 0.0, 0.0],
            "end_mm": [8.0, 0.0, 0.0],
        },
        {
            "curve_type": "arc",
            "start_mm": [8.0, 12.857556682, -18.464377492],
            "end_mm": [8.0, 18.568029721, 12.707410132],
            "radius_mm": 22.5,
        },
    ]

    selected = ProductionFilletSkill._select_explicit_edge_signatures(
        model,
        [FakeBody([line, arc, unrelated])],
        signatures,
        tolerance_mm=0.01,
    )

    assert selected == 2
    assert line.selected and line.append is False
    assert arc.selected and arc.append is True
    assert not unrelated.selected
    params = {
        "radius": 1.0,
        "edge_selector": "explicit_edge_signatures",
        "edge_tolerance_mm": 0.01,
        "edge_signatures": signatures,
    }
    assert _supported_fillet({"params": params}, params) is True


def test_explicit_closed_circle_signature_selects_by_center_axis_and_radius() -> None:
    target = FakeSignatureEdge(None, None, (0.061, 0.0, 0.0, -1.0, 0.0, 0.0, 0.07))
    wrong_radius = FakeSignatureEdge(None, None, (0.061, 0.0, 0.0, 1.0, 0.0, 0.0, 0.065))
    arc = FakeSignatureEdge(
        (0.061, 0.07, 0.0),
        (0.061, 0.0, 0.07),
        (0.061, 0.0, 0.0, 1.0, 0.0, 0.0, 0.07),
    )
    model = FakeModel(None)
    signature = {
        "curve_type": "circle",
        "center_mm": [61.0, 0.0, 0.0],
        "axis": [1.0, 0.0, 0.0],
        "radius_mm": 70.0,
    }

    selected = ProductionFilletSkill._select_explicit_edge_signatures(
        model,
        [FakeBody([wrong_radius, arc, target])],
        [signature],
        tolerance_mm=0.01,
    )

    assert selected == 1
    assert target.selected is True
    assert wrong_radius.selected is False
    assert arc.selected is False
    params = {
        "radius": 7.875,
        "edge_selector": "explicit_edge_signatures",
        "edge_tolerance_mm": 0.01,
        "edge_signatures": [signature],
    }
    assert _supported_fillet({"params": params}, params) is True


def test_explicit_edge_signature_scope_requires_exact_count() -> None:
    complete = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "explicit_edge_signatures",
            "selected_edges": 9,
            "expected_edge_signatures": 9,
        },
    )
    incomplete = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "explicit_edge_signatures",
            "selected_edges": 8,
            "expected_edge_signatures": 9,
        },
    )

    assert PipelineExecutor._fillet_operation_matches(complete) is True
    assert PipelineExecutor._fillet_operation_matches(incomplete) is False


def test_ordered_fillet_batch_executes_every_feature_and_aggregates_gates(
    tmp_path,
    monkeypatch,
) -> None:
    skill = ProductionFilletSkill(tmp_path)
    calls: list[str] = []

    def run_one(plan):
        feature = plan["features"][0]
        calls.append(feature["id"])
        return SkillResult(
            True,
            "created",
            data={
                "success": True,
                "feature_created": True,
                "edge_selector": "explicit_edge_signatures",
                "selected_edges": 1,
                "expected_edge_signatures": 1,
                "saved_by_this_skill": False,
            },
        )

    monkeypatch.setattr(skill, "_run_single_plan_dispatch", run_one)
    result = skill.run_plan(
        {
            "features": [
                {"id": "housing_fillet_1", "type": "fillet", "params": {"radius": 2}},
                {"id": "housing_fillet_2", "type": "fillet", "params": {"radius": 6}},
            ]
        }
    )

    assert result.success is True
    assert calls == ["housing_fillet_1", "housing_fillet_2"]
    assert result.data["features_created"] == 2
    assert result.data["features_requested"] == 2
    assert result.data["selected_edges"] == 2
    assert result.data["expected_edge_signatures"] == 2
    record = SimpleNamespace(status="success", data=result.data)
    assert PipelineExecutor._fillet_operation_matches(record) is True


def test_ordered_fillet_batch_gate_rejects_incomplete_child() -> None:
    record = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "ordered_batch",
            "features_created": 2,
            "features_requested": 2,
            "operations": [
                {
                    "success": True,
                    "feature_created": True,
                    "edge_selector": "explicit_edge_signatures",
                    "selected_edges": 1,
                    "expected_edge_signatures": 1,
                },
                {
                    "success": True,
                    "feature_created": True,
                    "edge_selector": "explicit_edge_signatures",
                    "selected_edges": 0,
                    "expected_edge_signatures": 1,
                },
            ],
        },
    )

    assert PipelineExecutor._fillet_operation_matches(record) is False


def test_task_model_reopen_unwraps_open_doc_tuple(tmp_path) -> None:
    path = tmp_path / "task.SLDPRT"
    path.write_bytes(b"solidworks-test")
    reopened = object()
    app = FakeApp(reopened)

    result = ProductionFilletSkill._reopen_task_model(app, FakeSavedModel(), path)

    assert result is reopened
    assert app.closed == ["task.SLDPRT"]


def test_scope_guard_accepts_legacy_four_edge_fillet() -> None:
    record = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "outer_vertical_edges",
            "selected_outer_vertical_edges": 4,
        },
    )

    assert PipelineExecutor._fillet_operation_matches(record) is True


def test_scope_guard_accepts_verified_semantic_feature_fillet() -> None:
    record = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "all_feature_edges",
            "target_feature_id": "wing_nut_wings",
            "selected_outer_vertical_edges": 1,
            "geometry_postcondition": {"success": True},
            "isolated_execution": {"success": True},
        },
    )

    assert PipelineExecutor._fillet_operation_matches(record) is True


def test_scope_guard_rejects_failed_semantic_feature_fillet() -> None:
    record = SimpleNamespace(
        status="success",
        data={
            "feature_created": True,
            "edge_selector": "all_feature_edges",
            "target_feature_id": "wing_nut_wings",
            "selected_outer_vertical_edges": 1,
            "geometry_postcondition": {"success": True},
            "isolated_execution": {"success": False},
        },
    )

    assert PipelineExecutor._fillet_operation_matches(record) is False
