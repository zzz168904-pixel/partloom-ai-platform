from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.dwg2cad_ir import (
    build_impeller_cad_ir,
    extract_drawing_ir,
    normalize_dxf_text,
)


def _dxf(pairs: list[tuple[int, object]]) -> str:
    return "\n".join(f"{code:>3}\n{value}" for code, value in pairs) + "\n"


def test_extract_drawing_ir_recovers_caxa_dimension_text_and_measurement(tmp_path: Path) -> None:
    source = tmp_path / "sample.dxf"
    source.write_text(
        _dxf(
            [
                (0, "SECTION"),
                (2, "BLOCKS"),
                (0, "BLOCK"),
                (2, "*D1"),
                (0, "MTEXT"),
                (5, "11"),
                (8, "DIM"),
                (10, 0),
                (20, 0),
                (1, r"\fSimSun|b0|i0|p0;8{%%P}0.018"),
                (0, "ENDBLK"),
                (0, "ENDSEC"),
                (0, "SECTION"),
                (2, "ENTITIES"),
                (0, "DIMENSION"),
                (5, "D1"),
                (8, "DIM"),
                (100, "AcDbDimension"),
                (2, "*D1"),
                (42, -1),
                (70, 32),
                (100, "AcDbAlignedDimension"),
                (13, 0),
                (23, 0),
                (33, 0),
                (14, 0),
                (24, 8),
                (34, 0),
                (50, 90),
                (100, "AcDbRotatedDimension"),
                (0, "ENDSEC"),
                (0, "EOF"),
            ]
        ),
        encoding="utf-8",
    )

    drawing_ir = extract_drawing_ir(source)

    assert drawing_ir["version"] == "drawing.ir.v1"
    assert drawing_ir["statistics"]["dimension_count"] == 1
    assert drawing_ir["dimensions"][0]["display_text"] == "8±0.018"
    assert drawing_ir["dimensions"][0]["measurement_mm"] == 8.0


def test_normalize_dxf_text_preserves_engineering_symbols() -> None:
    assert normalize_dxf_text(r"\fSimSun|b0|i0|p0;φ30{%%P}0.01") == "φ30±0.01"
    assert normalize_dxf_text(r"未注倒角1*45{%%D}") == "未注倒角1*45°"


def _line(handle: str, start: list[float], end: list[float]) -> dict:
    return {
        "handle": handle,
        "layer": "CAXA0",
        "type": "line",
        "start_mm": start,
        "end_mm": end,
    }


def _arc(
    handle: str,
    center: list[float],
    radius: float,
    start_angle: float,
    end_angle: float,
) -> dict:
    return {
        "handle": handle,
        "layer": "CAXA0",
        "type": "arc",
        "center_mm": center,
        "radius_mm": radius,
        "start_angle_deg": start_angle,
        "end_angle_deg": end_angle,
    }


def _impeller_drawing_ir() -> dict:
    geometry = [
        {
            "handle": "outer",
            "layer": "CAXA0",
            "type": "circle",
            "center_mm": [-170.0, 70.0, 0.0],
            "radius_mm": 113.0,
        },
        _line("129", [90.0, 183.0, 0.0], [95.0, 183.0, 0.0]),
        _line("12A", [95.0, 183.0, 0.0], [93.0, 142.0, 0.0]),
        _arc("108", [-111.0, 152.0, 0.0], 204.2, 348.0, 357.0),
        _arc("10A", [72.0, 113.0, 0.0], 17.0, 270.0, 348.0),
        _line("10C", [58.0, 96.0, 0.0], [72.0, 96.0, 0.0]),
        _line("10E", [58.0, 103.0, 0.0], [77.0, 103.0, 0.0]),
        _arc("10D", [75.5, 112.0, 0.0], 9.0, 279.0, 345.0),
        _arc("10F", [72.0, 113.0, 0.0], 12.5, 345.0, 348.0),
        _arc("10B", [-111.0, 152.0, 0.0], 199.7, 348.0, 357.0),
        _line("128", [90.0, 183.0, 0.0], [88.0, 142.0, 0.0]),
        _line("81", [-174.0, 101.0, 0.0], [-174.0, 159.0, 0.0]),
        _arc("80", [-170.0, 70.0, 0.0], 89.0, 87.0, 93.0),
        _line("83", [-166.0, 101.0, 0.0], [-166.0, 159.0, 0.0]),
        _arc("84", [-161.0, 101.0, 0.0], 5.0, 180.0, 254.0),
        _arc("82", [-179.0, 101.0, 0.0], 5.0, 286.0, 360.0),
    ]
    geometry.extend(
        _arc(f"tip_{index}", [-170.0, 70.0, 0.0], 89.0, index * 60.0, index * 60.0 + 5.0)
        for index in range(1, 6)
    )
    drawing_ir = {
        "version": "drawing.ir.v1",
        "source": {"units": "mm"},
        "geometry": geometry,
        "dimensions": [],
        "texts": [
            {
                "handle": "290",
                "text": "5.叶片5片均布。",
            }
        ],
    }
    return drawing_ir


def test_impeller_cad_ir_blocks_conflicting_blade_count() -> None:
    drawing_ir = _impeller_drawing_ir()

    cad_ir = build_impeller_cad_ir(drawing_ir)

    assert cad_ir["version"] == "cad.ir.v1"
    assert cad_ir["valid"] is False
    assert cad_ir["parameters"]["graphic_blade_count"] == 6
    assert cad_ir["parameters"]["noted_blade_count"] == 5
    assert cad_ir["errors"][0]["code"] == "drawing_conflict_blade_count"
    assert cad_ir["unsupported_features"][0]["required"] is True
    assert len(cad_ir["features"]) == 1


def test_impeller_cad_ir_uses_confirmed_five_blade_pattern() -> None:
    cad_ir = build_impeller_cad_ir(
        _impeller_drawing_ir(),
        selected_blade_count=5,
        decision_source="user_confirmation",
    )

    assert cad_ir["valid"] is False
    assert cad_ir["parameters"]["selected_blade_count"] == 5
    assert cad_ir["errors"] == []
    assert cad_ir["warnings"][0]["code"] == "drawing_conflict_blade_count_resolved"
    assert cad_ir["warnings"][0]["selected_value"] == 5
    assert [feature["operation"] for feature in cad_ir["features"]] == [
        "revolve",
        "profile_extrude",
        "circular_pattern",
    ]
    assert cad_ir["features"][2]["parameters"]["count"] == 5
    assert (
        cad_ir["recognized_feature_candidates"]["seed_blade"]["execution_ready"]
        is True
    )
    assert {
        item["type"] for item in cad_ir["unsupported_features"]
    } == {"incomplete_section_segmentation"}
