from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.engineering_knowledge import EngineeringKnowledgeBase


def test_command_catalog_and_drawing_reasoning() -> None:
    knowledge = EngineeringKnowledgeBase()
    design = {
        "source_brief": "Create an ISO metric mounting base with drawing and AutoCAD dimensions.",
        "parameters": {"unit": "mm", "length": 180.0, "width": 120.0, "thickness": 18.0},
        "features": [
            {"name": "CenterHole", "type": "through_hole", "params": {"diameter": 40.0}, "required": True},
            {"name": "PCDThreads", "type": "bolt_circle_pattern", "params": {"count": 6, "pcd": 80.0, "thread": "M6"}, "required": True},
            {"name": "TopBoss", "type": "boss", "params": {"length": 100.0, "width": 55.0, "height": 20.0}, "required": True},
            {"name": "Pocket", "type": "pocket", "params": {"length": 60.0, "width": 25.0, "depth": 10.0}, "required": True},
            {"name": "Slots", "type": "slot", "params": {"length": 45.0, "width": 12.0}, "required": True},
            {"name": "OuterFillet", "type": "fillet", "params": {"radius": 8.0}, "required": True},
            {"name": "OuterChamfer", "type": "chamfer", "params": {"distance": 1.0}, "required": True},
        ],
        "outputs": ["SLDPRT", "DWG", "PDF"],
        "requested_stages": ["model_3d", "drawing", "autocad_annotation", "export_files"],
        "unsupported_features": [],
        "needs_confirmation": False,
    }
    enriched = knowledge.enrich_design(design)
    commands = {item["command"] for item in enriched["cad_command_plan"]}
    assert {"base_plate", "through_hole", "bolt_circle_pattern", "boss", "pocket", "slot", "fillet", "chamfer"} <= commands
    assert {"drawing", "autocad_annotation", "export_sldprt", "export_dwg", "export_pdf"} <= commands
    assert not enriched["unsupported_features"]
    intents = set(enriched["drawing_plan"]["dimension_intents"])
    assert {"overall_length", "diameter", "pcd", "thread_callout", "slot_length", "radius", "chamfer_callout"} <= intents
    assert enriched["standards_profile"]["name"] == "ISO_metric"


def test_revolve_command_is_production_when_profile_is_explicit() -> None:
    knowledge = EngineeringKnowledgeBase()
    design = {
        "source_brief": "Revolve a shaft profile around its center axis.",
        "parameters": {"unit": "mm", "length": 100.0, "width": 20.0, "thickness": 20.0},
        "features": [
            {
                "name": "ShaftRevolve",
                "type": "revolve",
                "params": {
                    "operation": "base",
                    "mode": "new_model",
                    "profile": [[0, 0], [0, 10], [100, 10], [100, 0], [0, 0]],
                    "axis": "horizontal",
                    "angle": 360.0,
                },
                "required": True,
            }
        ],
        "outputs": ["SLDPRT"],
        "requested_stages": ["model_3d"],
        "unsupported_features": [],
        "needs_confirmation": False,
    }
    enriched = knowledge.enrich_design(design)
    assert not enriched["needs_confirmation"]
    assert not any(item.get("command") == "revolve" for item in enriched["unsupported_features"])
    command = next(item for item in enriched["cad_command_plan"] if item["command"] == "revolve")
    assert command["execution_status"] == "production"


def test_knowledge_sources_are_versioned_official_references() -> None:
    knowledge = EngineeringKnowledgeBase()
    context = knowledge.planner_context()
    assert context["knowledge_version"] == "2026.07.12.1"
    source_urls = {item["url"] for item in knowledge.rules["sources"]}
    assert any("iso.org" in url for url in source_urls)
    assert any("asme.org" in url for url in source_urls)
    assert any("help.solidworks.com" in url for url in source_urls)
    assert any("help.autodesk.com" in url for url in source_urls)


if __name__ == "__main__":
    test_command_catalog_and_drawing_reasoning()
    test_revolve_command_is_production_when_profile_is_explicit()
    test_knowledge_sources_are_versioned_official_references()
    print("Engineering knowledge tests passed")
