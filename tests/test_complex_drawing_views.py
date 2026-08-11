from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.engineering_knowledge import EngineeringKnowledgeBase
from cad_agent.solidworks_complex_drawing import SolidWorksComplexDrawingViewEngine


class FakeView:
    def __init__(
        self,
        name: str,
        view_type: int,
        position: tuple[float, float],
        size: tuple[float, float] = (0.12, 0.08),
        scale: float = 1.0,
    ) -> None:
        self.name = name
        self.Type = view_type
        self.position = position
        self.size = size
        self.scale = scale
        self.GetVisibleComponents = (object(),)

    def outline(self) -> tuple[float, float, float, float]:
        width = self.size[0] * self.scale
        height = self.size[1] * self.scale
        return (
            self.position[0] - width / 2.0,
            self.position[1] - height / 2.0,
            self.position[0] + width / 2.0,
            self.position[1] + height / 2.0,
        )


class FakeSelectData:
    View: Any = None


class FakeSelectionManager:
    @staticmethod
    def CreateSelectData() -> FakeSelectData:
        return FakeSelectData()


class FakeSketchSegment:
    def __init__(self, drawing: "FakeDrawing", kind: str) -> None:
        self.drawing = drawing
        self.kind = kind

    def Select4(self, _append: bool, _select_data: Any) -> bool:
        self.drawing.selected_segment = self
        return True

    def Select2(self, _append: bool, _mark: int) -> bool:
        self.drawing.selected_segment = self
        return True


class FakeSketchManager:
    def __init__(self, drawing: "FakeDrawing") -> None:
        self.drawing = drawing

    def CreateLine(self, *coordinates: float) -> FakeSketchSegment:
        self.drawing.section_line = list(coordinates)
        return FakeSketchSegment(self.drawing, "line")

    def CreateCircle(self, *coordinates: float) -> FakeSketchSegment:
        self.drawing.detail_circle = list(coordinates)
        return FakeSketchSegment(self.drawing, "circle")


class FakeDrawing:
    def __init__(self) -> None:
        self.views = [
            FakeView("Drawing View1", 6, (0.23, 0.25)),
            FakeView("Drawing View2", 6, (0.23, 0.53)),
            FakeView("Drawing View3", 6, (0.54, 0.25)),
            FakeView("Drawing View4", 7, (0.54, 0.53), size=(0.10, 0.10), scale=0.7),
        ]
        self.SelectionManager = FakeSelectionManager()
        self.SketchManager = FakeSketchManager(self)
        self.active_view = ""
        self.selected_segment: FakeSketchSegment | None = None
        self.section_line: list[float] = []
        self.detail_circle: list[float] = []
        self.section_calls: list[tuple[Any, ...]] = []
        self.detail_calls: list[tuple[Any, ...]] = []

    def ActivateView(self, name: str) -> bool:
        self.active_view = name
        return any(view.name == name for view in self.views)

    def ClearSelection2(self, _all: bool) -> None:
        self.selected_segment = None

    def CreateSectionViewAt5(self, *args: Any) -> FakeView:
        assert self.selected_segment is not None
        self.section_calls.append(args)
        view = FakeView("Section A-A", 2, (float(args[0]), float(args[1])))
        self.views.append(view)
        return view

    def CreateDetailViewAt4(self, *args: Any) -> FakeView:
        assert self.detail_circle
        self.detail_calls.append(args)
        view = FakeView(
            "Detail B",
            3,
            (float(args[0]), float(args[1])),
            size=(0.055, 0.055),
            scale=float(args[4]) / float(args[5]),
        )
        self.views.append(view)
        return view


class FakeConnector:
    @staticmethod
    def _get_drawing_views(drawing: FakeDrawing, verbose: bool = False) -> list[FakeView]:
        return list(drawing.views)

    @staticmethod
    def _get_view_outline(view: FakeView) -> tuple[float, float, float, float]:
        return view.outline()

    @staticmethod
    def _get_view_name(view: FakeView) -> str:
        return view.name

    @staticmethod
    def _activate_drawing_view(drawing: FakeDrawing, view_name: str) -> bool:
        return drawing.ActivateView(view_name)

    @staticmethod
    def _get_sheet_size(_drawing: FakeDrawing) -> tuple[float, float, str]:
        return 1.189, 0.841, "A0 Sheet1"

    @staticmethod
    def _drawing_effective_area(_width: float, _height: float) -> tuple[float, float, float, float]:
        return 0.04, 0.10, 1.149, 0.80

    @staticmethod
    def _rebuild_and_zoom(_drawing: FakeDrawing) -> None:
        return None

    @staticmethod
    def _get_view_scale(view: FakeView) -> float:
        return view.scale

    @staticmethod
    def _set_view_scale(view: FakeView, scale: float, _name: str) -> None:
        view.scale = float(scale)

    @staticmethod
    def _set_view_position(view: FakeView, position: tuple[float, float]) -> None:
        view.position = position

    @staticmethod
    def _outlines_inside_area(
        outlines: dict[str, tuple[float, float, float, float]],
        area: tuple[float, float, float, float],
    ) -> bool:
        left, bottom, right, top = area
        return all(value[0] >= left and value[1] >= bottom and value[2] <= right and value[3] <= top for value in outlines.values())

    @staticmethod
    def _has_overlap(outlines: dict[str, tuple[float, float, float, float]]) -> bool:
        values = list(outlines.values())
        for index, first in enumerate(values):
            for second in values[index + 1 :]:
                if not (first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1]):
                    return True
        return False


def test_native_section_and_detail_calls() -> None:
    drawing = FakeDrawing()
    plan = {
        "special_views": [
            {
                "type": "section_view",
                "source_view": "front",
                "cutting_plane": {"orientation": "vertical", "through": "center"},
                "label": "A",
                "required": True,
            },
            {
                "type": "detail_view",
                "source_view": "front",
                "boundary": {"normalized_center": [0.5, 0.5], "radius_ratio": 0.2},
                "scale": 2.0,
                "label": "B",
                "required": True,
            },
        ]
    }
    result = SolidWorksComplexDrawingViewEngine(FakeConnector()).apply(drawing, plan)
    assert result["success"], result
    assert result["created"] == 2
    assert len(drawing.section_calls) == 1
    assert len(drawing.section_calls[0]) == 7
    assert drawing.section_calls[0][3] == "A"
    assert len(drawing.detail_calls) == 1
    assert len(drawing.detail_calls[0]) == 12
    assert drawing.detail_calls[0][4:7] == (2.0, 1.0, "B")
    assert {item["view_type"] for item in result["views"]} == {2, 3}
    assert all(item["native_type_verified"] for item in result["views"])
    assert all(item["geometry_evidence"]["has_model_geometry"] for item in result["views"])
    assert result["layout"]["view_count"] == 6
    assert result["layout"]["inside_effective_area"]
    assert not result["layout"]["overlap"]


def test_engineering_plan_routes_special_drawing_views() -> None:
    design = {
        "source_brief": "把当前模型生成工程图，包含中心剖视图和2:1局部放大图",
        "parameters": {"unit": "mm"},
        "features": [],
        "outputs": ["SLDDRW"],
        "requested_stages": ["drawing"],
        "unsupported_features": [],
        "needs_confirmation": False,
    }
    enriched = EngineeringKnowledgeBase().enrich_design(design)
    special = enriched["drawing_plan"]["special_views"]
    assert [item["type"] for item in special] == ["section_view", "detail_view"]
    assert special[0]["cutting_plane"]["through"] == "center"
    assert special[1]["scale"] == 2.0
    commands = {item["command"]: item for item in enriched["cad_command_plan"]}
    assert commands["section_view"]["execution_status"] == "production"
    assert commands["detail_view"]["execution_status"] == "production"
    assert not enriched["unsupported_features"]


if __name__ == "__main__":
    test_native_section_and_detail_calls()
    test_engineering_plan_routes_special_drawing_views()
    print("Complex drawing view tests passed")
