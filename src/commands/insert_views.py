from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

MODEL_VIEW_NAMES = {
    "Front": "*Front",
    "Top": "*Top",
    "Right": "*Right",
    "Isometric": "*Isometric",
}

VIEW_POSITIONS_METERS = {
    "Front": (0.12, 0.18, 0.0),
    "Top": (0.12, 0.30, 0.0),
    "Right": (0.28, 0.18, 0.0),
    "Isometric": (0.28, 0.30, 0.0),
}


def insert_views(drawing: Any, model_path: Path, views: list[str]) -> list[Any]:
    created_views: list[Any] = []

    for view in views:
        try:
            sw_view_name = MODEL_VIEW_NAMES[view]
            x, y, z = VIEW_POSITIONS_METERS[view]
            created_view = drawing.CreateDrawViewFromModelView3(
                str(model_path),
                sw_view_name,
                x,
                y,
                z,
            )
            if created_view is None:
                raise RuntimeError(f"CreateDrawViewFromModelView3 returned no view: {view}")
            created_views.append(created_view)
            LOGGER.info("Inserted drawing view: %s", view)
        except Exception as exc:
            LOGGER.exception("Failed to insert drawing view: %s", view)
            raise RuntimeError(f"Failed to insert drawing view: {view}") from exc

    return created_views
