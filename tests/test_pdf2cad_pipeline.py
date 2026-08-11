from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cad_agent.pdf2cad.models import PdfDrawingIR
from src.cad_agent.pdf2cad.parser import _enrich_ir_from_ocr, _ir_from_markdown, parse_pdf_to_ir
from src.cad_agent.pdf2cad.runner import PDF2CADPipelineRunner
from src.cad_agent.pdf2cad.understanding import ir_to_design_json


def test_pdf2cad_markdown_to_design_json_grid_drawing() -> None:
    markdown = """
    名称：网2
    图号：SYN-GRID-001
    材料：SUS316
    整体尺寸：1080 x 860
    网格间距：20 x 20
    钢丝直径：Φ2.5
    比例：1:1
    """
    ir = _ir_from_markdown(markdown)
    design = ir_to_design_json(ir)
    params = design["parameters"]
    assert params["length"] == 1080.0
    assert params["width"] == 860.0
    assert params["grid_pitch_x"] == 20.0
    assert params["grid_pitch_y"] == 20.0
    assert params["wire_diameter"] == 2.5
    assert params["material"] == "SUS316"
    assert design["title_block"]["drawing_no"] == "SYN-GRID-001"
    assert design["title_block"]["name"] == "网2"
    assert "autocad_generate_dwg" in design["skill_pipeline"]


def test_pdf2cad_ir_schema_shape() -> None:
    ir = PdfDrawingIR()
    data = ir.as_dict()
    assert data["source_type"] == "pdf_engineering_drawing"
    for key in ("geometry", "dimensions", "materials", "title_block", "views", "notes"):
        assert key in data


def test_spatial_ocr_extracts_rotated_overall_and_grid_dimensions() -> None:
    ir = PdfDrawingIR()
    observations = [
        {"image": "drawing.jpg", "rotation": 0, "text": "1080", "score": 0.99, "center": [0.40, 0.95]},
        {"image": "drawing.jpg", "rotation": 90, "text": "950", "score": 0.98, "center": [0.50, 0.08]},
        {"image": "drawing.jpg", "rotation": 0, "text": "2.5", "score": 0.99, "center": [0.82, 0.50]},
        {"image": "drawing.jpg", "rotation": 0, "text": "20", "score": 0.99, "center": [0.87, 0.52]},
    ]
    _enrich_ir_from_ocr(ir, observations)
    values = {item["type"]: item["value"] for item in ir.dimensions}
    assert values["overall_length"] == 1080.0
    assert values["overall_width"] == 950.0
    assert values["grid_pitch_x"] == 20.0
    assert values["grid_pitch_y"] == 20.0
    assert any(item.get("name") == "wire_diameter" and item["value"] == 2.5 for item in ir.dimensions)


def test_production_parser_does_not_use_filename_fixture(tmp_path: Path | None = None) -> None:
    from pypdf import PdfWriter

    import tempfile

    if tmp_path is None:
        with tempfile.TemporaryDirectory() as temp:
            return test_production_parser_does_not_use_filename_fixture(Path(temp))
    source = tmp_path / "synthetic_fixture_guard.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as stream:
        writer.write(stream)
    previous = os.environ.get("PDF2CAD_DISABLE_EXTERNAL_OCR")
    os.environ["PDF2CAD_DISABLE_EXTERNAL_OCR"] = "1"
    try:
        ir = parse_pdf_to_ir(source, tmp_path / "parse")
        assert ir.parser == "unresolved-image-pdf"
        assert not ir.geometry
        planned = PDF2CADPipelineRunner(tmp_path / "out").plan(source)
        assert planned["summary"]["needs_confirmation"]
        assert "trustworthy_pdf_parse" in planned["summary"]["unexecutable_required_features"][-1]["values"]
    finally:
        if previous is None:
            os.environ.pop("PDF2CAD_DISABLE_EXTERNAL_OCR", None)
        else:
            os.environ["PDF2CAD_DISABLE_EXTERNAL_OCR"] = previous


if __name__ == "__main__":
    test_pdf2cad_markdown_to_design_json_grid_drawing()
    test_pdf2cad_ir_schema_shape()
    test_spatial_ocr_extracts_rotated_overall_and_grid_dimensions()
    test_production_parser_does_not_use_filename_fixture()
    print("PDF2CAD tests passed")
