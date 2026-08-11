from __future__ import annotations

import logging
from typing import Any

from sw_connector import SolidWorksConnector


LOGGER = logging.getLogger(__name__)


def close_document(connector: SolidWorksConnector, document: Any, label: str) -> None:
    try:
        if document is None:
            LOGGER.info("No %s document to close.", label)
            return
        LOGGER.info("Close %s command started.", label)
        connector.close_document(document)
    except Exception:
        LOGGER.exception("Close %s command failed.", label)
        raise
