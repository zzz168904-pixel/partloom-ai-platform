from __future__ import annotations

import json
from pathlib import Path

from cad_agent.direct_cad_ir import DirectCADIRService
from cad_agent.runtime_config import application_data_dir, output_root


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_paths_are_partloom_scoped(monkeypatch) -> None:
    monkeypatch.delenv("PARTLOOM_APP_DATA", raising=False)
    monkeypatch.delenv("PARTLOOM_OUTPUT_DIR", raising=False)
    assert application_data_dir().name == "PartLoomAI"
    assert output_root().name == "PartLoom_AI_Output"


def test_runtime_path_overrides(monkeypatch, tmp_path: Path) -> None:
    app_data = tmp_path / "state"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("PARTLOOM_APP_DATA", str(app_data))
    monkeypatch.setenv("PARTLOOM_OUTPUT_DIR", str(outputs))
    assert application_data_dir() == app_data
    assert output_root() == outputs


def test_public_examples_are_synthetic_json() -> None:
    paths = sorted((ROOT / "examples" / "cad_ir").glob("*.json"))
    assert len(paths) == 5
    for path in paths:
        assert "selected" not in path.name.lower()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


def test_plate_example_passes_direct_cad_ir_planning(tmp_path: Path) -> None:
    example = ROOT / "examples" / "cad_ir" / "gui_cad_ir_plate_with_center_hole.json"
    planned = DirectCADIRService(tmp_path).plan(str(example), stage_mode="model_3d")
    assert planned["planning_validation"]["allow_pipeline"] is True
    assert planned["requested_stages"] == ["model_3d"]
