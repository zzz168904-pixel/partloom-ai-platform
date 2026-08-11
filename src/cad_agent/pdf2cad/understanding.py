from __future__ import annotations

from typing import Any

from .models import PdfDrawingIR


def ir_to_design_json(ir: PdfDrawingIR, mode: str = "2d") -> dict[str, Any]:
    params = {
        "unit": "mm",
        "material": ir.materials[0] if ir.materials else "",
    }
    for dimension in ir.dimensions:
        dtype = dimension.get("type")
        value = dimension.get("value")
        if dtype == "overall_length":
            params["length"] = float(value)
        elif dtype == "overall_width":
            params["width"] = float(value)
        elif dtype == "grid_pitch_x":
            params["grid_pitch_x"] = float(value)
        elif dtype == "grid_pitch_y":
            params["grid_pitch_y"] = float(value)
        elif dtype == "diameter" and dimension.get("name") == "wire_diameter":
            params["wire_diameter"] = float(value)

    for item in ir.geometry:
        if item.get("type") == "overall_rectangle":
            params.setdefault("length", float(item.get("length", 0)))
            params.setdefault("width", float(item.get("width", 0)))
        elif item.get("type") == "grid":
            params.setdefault("grid_pitch_x", float(item.get("pitch_x", 0)))
            params.setdefault("grid_pitch_y", float(item.get("pitch_y", 0)))

    features: list[dict[str, Any]] = []
    if params.get("grid_pitch_x") and params.get("grid_pitch_y"):
        features.append(
            {
                "name": "WireGrid",
                "type": "wire_grid_2d",
                "params": {
                    "pitch_x": params["grid_pitch_x"],
                    "pitch_y": params["grid_pitch_y"],
                    "wire_diameter": params.get("wire_diameter"),
                },
                "required": True,
                "target_skill": "pdf2cad",
            }
        )

    risks: list[dict[str, Any]] = []
    for note in ir.notes:
        if note.startswith("material_conflict:"):
            risks.append(
                {
                    "type": "material_conflict",
                    "message": note,
                    "candidates": list(ir.materials),
                    "requires_confirmation": True,
                }
            )
        elif note.startswith("ocr_inference:"):
            risks.append(
                {
                    "type": "ocr_spatial_inference",
                    "message": note,
                    "requires_confirmation": False,
                }
            )

    return {
        "schema_version": "pdf2cad.design.v1",
        "source_type": ir.source_type,
        "intent": "pdf_to_cad_2d" if mode == "2d" else "pdf_to_cad_3d",
        "part_family": "pdf_reconstructed_drawing",
        "parameters": params,
        "material_candidates": list(ir.materials),
        "features": features,
        "dimension_evidence": list(ir.dimensions),
        "title_block": ir.title_block,
        "views": ir.views,
        "notes": ir.notes,
        "risks": risks,
        "skill_pipeline": [
            "pdf_parse",
            "drawing_understanding",
            "cad_reconstruction",
            "autocad_generate_dwg",
            "autocad_annotation",
            "export_pdf_dwg",
        ],
    }
