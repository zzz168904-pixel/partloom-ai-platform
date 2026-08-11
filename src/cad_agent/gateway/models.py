from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PlanTaskRequest(BaseModel):
    source_type: Literal["text", "pdf", "cad_file"] = "text"
    prompt: str = ""
    source_path: str | None = None
    stage_mode: str = "auto"
    conversion_mode: Literal["2d", "3d"] = "2d"
    provider: str = "auto"
    requested_outputs: list[str] = Field(default_factory=list)
    execute_real_skills: bool = True

    @model_validator(mode="after")
    def validate_source(self) -> "PlanTaskRequest":
        if self.source_type == "text" and not self.prompt.strip():
            raise ValueError("prompt is required for text tasks")
        if self.source_type in {"pdf", "cad_file"} and not str(self.source_path or "").strip():
            raise ValueError("source_path is required for file tasks")
        return self


class TaskActionResponse(BaseModel):
    task_id: str
    status: str
    message: str
