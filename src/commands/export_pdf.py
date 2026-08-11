from __future__ import annotations

import logging
from typing import Any

from models import DrawingRequest
from sw_connector import SolidWorksConnector


LOGGER = logging.getLogger(__name__)


def export_pdf(connector: SolidWorksConnector, drawing: Any, request: DrawingRequest) -> None:
    try:
        LOGGER.info("Export PDF command started.")
        connector.export_pdf(drawing, request.output_path)
    except Exception:
        LOGGER.exception("Export PDF command failed.")
        raise
