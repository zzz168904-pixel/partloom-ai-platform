from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "logs" / "test_skill_outputs"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cad_agent.autocad_annotation_engine import DimensionStrategy, DrawingAnalysis, EntityInfo, ViewRegion
from src.cad_agent.autocad_annotation_skill import AutoCADAnnotationSkill
from src.cad_agent.autocad_skill import AutoCADSkill


def _sample_analysis() -> DrawingAnalysis:
    return DrawingAnalysis(
        entities=[
            EntityInfo("L1", "AcDbLine", "line", "OUTLINE", bbox=(-50, -30, 50, -30), start=(-50, -30, 0), end=(50, -30, 0)),
            EntityInfo("L2", "AcDbLine", "line", "OUTLINE", bbox=(50, -30, 50, 30), start=(50, -30, 0), end=(50, 30, 0)),
            EntityInfo("C1", "AcDbCircle", "circle", "HOLES", bbox=(-10, -10, 10, 10), center=(0, 0, 0), radius=10),
            EntityInfo("A1", "AcDbArc", "arc", "OUTLINE", bbox=(40, 20, 50, 30), center=(45, 25, 0), radius=5, start=(50, 25, 0)),
        ],
        bbox=(-50, -30, 50, 30),
        counts={"line": 2, "circle": 1, "arc": 1},
    )


def test_dimension_strategy_generates_mechanical_annotation_plan() -> None:
    plans = DimensionStrategy().build(_sample_analysis())
    reasons = {plan.reason for plan in plans}
    types = [plan.dim_type for plan in plans]
    assert "overall length" in reasons
    assert "overall height/width" in reasons
    assert "hole diameter" in reasons
    assert "hole horizontal position" in reasons
    assert "hole vertical position" in reasons
    assert "arc/fillet radius" in reasons
    assert types.count("rotated") >= 4
    assert "diametric" in types
    assert "radial" in types
    assert types.count("centerline") == 2
    assert "center_mark" in types


def test_dimension_strategy_uses_model_view_regions_not_sheet_border() -> None:
    front_entities = [
        EntityInfo("M1", "AcDbLine", "line", "0", bbox=(300, 200, 620, 200), start=(300, 200, 0), end=(620, 200, 0)),
        EntityInfo("M2", "AcDbLine", "line", "0", bbox=(620, 200, 620, 390), start=(620, 200, 0), end=(620, 390, 0)),
        EntityInfo("M3", "AcDbLine", "line", "0", bbox=(300, 390, 620, 390), start=(300, 390, 0), end=(620, 390, 0)),
        EntityInfo("M4", "AcDbLine", "line", "0", bbox=(300, 200, 300, 390), start=(300, 390, 0), end=(300, 200, 0)),
        EntityInfo("H1", "AcDbCircle", "circle", "0", bbox=(445, 280, 475, 310), center=(460, 295, 0), radius=15),
        EntityInfo("H2", "AcDbCircle", "circle", "0", bbox=(535, 280, 555, 300), center=(545, 290, 0), radius=10),
    ]
    analysis = DrawingAnalysis(
        entities=[
            EntityInfo("B1", "AcDbLine", "line", "5", bbox=(0, 0, 1189, 0), start=(0, 0, 0), end=(1189, 0, 0)),
            EntityInfo("B2", "AcDbLine", "line", "5", bbox=(0, 0, 0, 841), start=(0, 0, 0), end=(0, 841, 0)),
            *front_entities,
        ],
        bbox=(0, 0, 1189, 841),
        counts={"line": 6, "circle": 1},
        view_regions=[ViewRegion("front_1", (300, 200, 620, 390), front_entities, "front")],
    )
    plans = DimensionStrategy().build(analysis)
    overall = [plan for plan in plans if "overall" in plan.reason]
    assert overall
    assert all(plan.p1 and plan.p1[0] >= 300 for plan in overall)
    assert all(plan.p2 and plan.p2[0] <= 620 for plan in overall)
    assert not any(plan.p1 == (0, 0, 0.0) and plan.p2 == (1189, 0, 0.0) for plan in overall)
    assert sum(1 for plan in plans if plan.reason == "front_1 hole diameter") == 2
    assert sum(1 for plan in plans if "hole horizontal position" in plan.reason) == 2
    assert sum(1 for plan in plans if "hole vertical position" in plan.reason) == 2
    assert sum(1 for plan in plans if plan.dim_type == "centerline") == 4
    assert sum(1 for plan in plans if plan.dim_type == "center_mark") == 2


def test_real_autocad_annotation_engine_when_enabled() -> None:
    if os.environ.get("RUN_REAL_ACAD_ANNOTATION") != "1":
        return

    plan = {
        "parameters": {"length": 100, "width": 60, "thickness": 10},
        "features": [{"type": "through_hole", "params": {"diameter": 20, "position": "center"}}],
    }
    source_path = _latest_existing_dwg()
    if source_path is None:
        source_result = AutoCADSkill(OUTPUT_ROOT).run_plan(plan)
        assert source_result.success, source_result.output
        assert source_result.path
        source_path = Path(source_result.path)

    result = AutoCADAnnotationSkill(OUTPUT_ROOT).run_plan(plan, source_dwg_path=source_path)
    assert result.success, result.output
    assert result.path and Path(result.path).exists()
    assert result.data["source_mode"] == "solidworks_exported_dwg"
    assert result.data["dimensions_created"] > 0
    assert result.data["centerlines_created"] >= 2
    assert result.data["report_path"] and Path(result.data["report_path"]).exists()


def _latest_existing_dwg() -> Path | None:
    candidates = sorted(OUTPUT_ROOT.glob("autocad_*/vibecad_2d.dwg"), key=lambda item: item.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    desktop_candidates = sorted(
        (Path.home() / "Documents" / "PartLoom_AI_Output").glob("*.dwg"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return desktop_candidates[0] if desktop_candidates else None


if __name__ == "__main__":
    test_dimension_strategy_generates_mechanical_annotation_plan()
    test_real_autocad_annotation_engine_when_enabled()
    print("AutoCAD annotation engine tests passed")
