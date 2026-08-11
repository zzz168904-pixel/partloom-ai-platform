from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.agents_orchestrator.dimension_parser import parse_body_dimensions


def assert_dims(text: str, expected: tuple[float, float, float]) -> None:
    result = parse_body_dimensions(text)
    assert result.ok, result
    params = result.numeric_parameters()
    assert (params["length"], params["width"], params["thickness"]) == expected
    details = result.details()
    for key in ("length", "width", "thickness"):
        assert details[key]["value"] > 0
        assert details[key]["unit"] == "mm"
        assert details[key]["source_text"]
        assert details[key]["confidence"] > 0.8
        assert details[key]["source"]


def test_chinese_labeled_dimensions() -> None:
    assert_dims("整体尺寸为长180mm，宽120mm，厚18mm，材料6061铝。", (180.0, 120.0, 18.0))


def test_multiply_dimensions_with_unit() -> None:
    assert_dims("外形尺寸180mm×120mm×18mm，四角R8。", (180.0, 120.0, 18.0))


def test_star_dimensions_without_unit() -> None:
    assert_dims("安装板 180*120*18，中心Φ40孔。", (180.0, 120.0, 18.0))


def test_mixed_english_lwh() -> None:
    assert_dims("6061 aluminum base plate L180 W120 H18 with M6 holes.", (180.0, 120.0, 18.0))


def test_feature_dimensions_do_not_become_body_dimensions() -> None:
    assert_dims("整体尺寸180×120×18 mm，R8圆角，R3型腔，φ8.5孔，φ40中心孔，PCD80，6个M6孔，距边15mm。", (180.0, 120.0, 18.0))


def test_missing_dimensions_are_not_guessed() -> None:
    result = parse_body_dimensions("设计安装底座，中心Φ40孔，四角R8，6个M6孔，距边15mm。")
    assert not result.ok
    assert result.risks
    assert result.unsupported_features[0]["type"] == "missing_body_dimensions"


def test_multiple_dimension_sets_prefers_body_context() -> None:
    assert_dims("外形尺寸180×120×18，凸台长100mm，宽55mm，高20mm，型腔60×25×10。", (180.0, 120.0, 18.0))


def test_multiple_uncontextualized_dimension_sets_are_ambiguous() -> None:
    result = parse_body_dimensions("180×120×18，100×55×20，60×25×10。")
    assert not result.ok
    assert result.unsupported_features[0]["type"] == "ambiguous_body_dimensions"


if __name__ == "__main__":
    test_chinese_labeled_dimensions()
    test_multiply_dimensions_with_unit()
    test_star_dimensions_without_unit()
    test_mixed_english_lwh()
    test_feature_dimensions_do_not_become_body_dimensions()
    test_missing_dimensions_are_not_guessed()
    test_multiple_dimension_sets_prefers_body_context()
    test_multiple_uncontextualized_dimension_sets_are_ambiguous()
    print("Dimension parser tests passed")
