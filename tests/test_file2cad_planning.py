from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.file2cad import CADFilePipelineRunner


def test_dwg_and_dxf_route_to_autocad() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        runner = CADFilePipelineRunner(root / "out")
        dwg = root / "source.dwg"
        dxf = root / "source.dxf"
        dwg.write_bytes(b"DWG-test")
        dxf.write_text("0\nEOF\n", encoding="ascii")
        dwg_plan = runner.plan(dwg, requested_outputs=["DWG", "PDF"], prompt="add dimensions")
        assert dwg_plan["design_json"]["route"] == "autocad"
        assert "autocad_annotation" in dwg_plan["design_json"]["skill_pipeline"]

        no_annotation = runner.plan(dwg, requested_outputs=["DWG"], prompt="open without annotation")
        assert not no_annotation["design_json"]["annotate"]
        assert "autocad_annotation" not in no_annotation["design_json"]["skill_pipeline"]

        chinese_no_annotation = runner.plan(dwg, requested_outputs=["DWG"], prompt="打开DWG，不要添加尺寸标注")
        assert not chinese_no_annotation["design_json"]["annotate"]
        assert not dwg_plan["summary"]["needs_confirmation"]
        dxf_plan = runner.plan(dxf)
        assert dxf_plan["design_json"]["outputs"] == ["DWG", "DXF"]


def test_exchange_formats_route_to_solidworks_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        runner = CADFilePipelineRunner(root / "out")
        step = root / "source.step"
        step.write_text("ISO-10303-21;END-ISO-10303-21;", encoding="ascii")
        normal = runner.plan(step, requested_outputs=["SLDPRT", "STEP"])
        assert normal["design_json"]["route"] == "solidworks"
        assert not normal["summary"]["needs_confirmation"]
        blocked = runner.plan(step, requested_outputs=["PDF"])
        assert blocked["summary"]["needs_confirmation"]
        assert blocked["summary"]["unexecutable_required_features"]


if __name__ == "__main__":
    test_dwg_and_dxf_route_to_autocad()
    test_exchange_formats_route_to_solidworks_and_fail_closed()
    print("File2CAD planning tests passed")
