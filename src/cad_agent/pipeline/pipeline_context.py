from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..brain_models import BrainPlan


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class PipelineStepRecord:
    skill_key: str
    action: str
    required: bool
    status: str = "pending"
    reason: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    lifecycle: dict[str, str] = field(default_factory=dict)
    message: str = ""
    output_path: str | None = None
    output_files: list[str] = field(default_factory=list)
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_key": self.skill_key,
            "action": self.action,
            "required": self.required,
            "status": self.status,
            "reason": self.reason,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "lifecycle": self.lifecycle,
            "message": self.message,
            "output_path": self.output_path,
            "output_files": self.output_files,
            "error": self.error,
            "data": self.data,
        }


@dataclass
class PipelineContext:
    prompt: str
    output_root: Path
    run_dir: Path
    brain_plan: BrainPlan | None = None
    design_json: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    status: str = "running"
    artifacts: dict[str, str] = field(default_factory=dict)
    step_records: list[PipelineStepRecord] = field(default_factory=list)
    unexpected_outputs: list[str] = field(default_factory=list)
    final_model_validation: dict[str, Any] = field(default_factory=dict)
    log_path: Path | None = None
    report_path: Path | None = None

    def add_artifact(self, key: str, value: str | Path | None) -> None:
        if value:
            self.artifacts[key] = str(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "run_dir": str(self.run_dir),
            "log_path": str(self.log_path) if self.log_path else None,
            "report_path": str(self.report_path) if self.report_path else None,
            "model_provider": self.brain_plan.model_provider if self.brain_plan else None,
            "design_json": self.design_json,
            "skill_pipeline": [record.as_dict() for record in self.step_records],
            "artifacts": self.artifacts,
            "unexpected_outputs": self.unexpected_outputs,
            "final_model_validation": self.final_model_validation,
        }
