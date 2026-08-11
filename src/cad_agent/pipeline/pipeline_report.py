from __future__ import annotations

import json
from pathlib import Path

from .pipeline_context import PipelineContext, utc_now_iso


class PipelineReportWriter:
    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path
        self.report_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, context: PipelineContext) -> Path:
        context.ended_at = context.ended_at or utc_now_iso()
        context.report_path = self.report_path
        self.report_path.write_text(
            json.dumps(context.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.report_path
