from __future__ import annotations

import logging
from typing import Any

from models import DrawingRequest
from sw_connector import SolidWorksConnector


LOGGER = logging.getLogger(__name__)


def open_model(connector: SolidWorksConnector, request: DrawingRequest) -> Any:
    try:
        LOGGER.info("Opening model command started.")
        return connector.open_document(request.model_path)
    except Exception:
        LOGGER.exception("Opening model command failed.")
        raise
