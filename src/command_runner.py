from __future__ import annotations

import logging

from commands.close_document import close_document
from commands.create_drawing import create_drawing
from commands.export_pdf import export_pdf
from commands.insert_views import insert_views
from commands.open_model import open_model
from models import DrawingRequest
from sw_connector import SolidWorksConnector


LOGGER = logging.getLogger(__name__)


class CommandRunner:
    def __init__(self, connector: SolidWorksConnector) -> None:
        self.connector = connector

    def run(self, request: DrawingRequest) -> None:
        model = None
        drawing = None

        try:
            LOGGER.info("Task started: %s", request.task)
            request.validate_files()
            self.connector.connect()
            model = open_model(self.connector, request)
            drawing = create_drawing(self.connector, request)
            insert_views(drawing, request.model_path, request.views)
            export_pdf(self.connector, drawing, request)
            LOGGER.info("Task completed successfully: %s", request.output_path)
        except Exception:
            LOGGER.exception("Task failed.")
            raise
        finally:
            close_errors: list[Exception] = []
            for label, document in (("drawing", drawing), ("model", model)):
                try:
                    close_document(self.connector, document, label)
                except Exception as exc:
                    close_errors.append(exc)
            if close_errors:
                LOGGER.warning("One or more documents failed to close.")
