from __future__ import annotations

import logging
from typing import Any

from models import DrawingRequest
from sw_connector import SolidWorksConnector


LOGGER = logging.getLogger(__name__)


def create_drawing(connector: SolidWorksConnector, request: DrawingRequest) -> Any:
    try:
        LOGGER.info("Create drawing command started.")
        return connector.new_drawing(request.drawing_template)
    except Exception:
        LOGGER.exception("Create drawing command failed.")
        raise
