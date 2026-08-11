from __future__ import annotations

from pathlib import Path
from typing import Any


MODEL_VIEW_NAMES = {
    "Front": "*Front",
    "Top": "*Top",
    "Right": "*Right",
    "Isometric": "*Isometric",
}


def create_standard_view(drawing: Any, model_path: Path, view_name: str, x: float, y: float):
    try:
        sw_view_name = MODEL_VIEW_NAMES[view_name]
        view = drawing.CreateDrawViewFromModelView3(str(model_path), sw_view_name, x, y, 0.0)
        if view is None:
            raise RuntimeError(f"CreateDrawViewFromModelView3 returned no view: {view_name}")
        return view
    except Exception as exc:
        raise RuntimeError(f"Failed to create drawing view: {view_name}") from exc
