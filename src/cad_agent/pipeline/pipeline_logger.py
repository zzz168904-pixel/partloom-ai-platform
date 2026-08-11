from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .pipeline_context import utc_now_iso


class PipelineLogger:
    def __init__(self, log_path: Path, event_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.log_path = log_path
        self.event_callback = event_callback
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event_type: str, **payload: Any) -> None:
        entry = {
            "time": utc_now_iso(),
            "event": event_type,
            **payload,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.event_callback is not None:
            self.event_callback(entry)
