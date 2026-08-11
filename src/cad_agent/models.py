from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    output: str = ""


@dataclass(frozen=True)
class AgentRoute:
    skill_key: str
    skill_name: str
    actions: list[str]
    confidence: float
    reason: str


@dataclass(frozen=True)
class SkillPaths:
    project_root: Path
    output_root: Path
    solidworks_skill_dir: Path
    vibecad_skill_dir: Path
    threaded_holes_skill_dir: Path
    fillet_chamfer_skill_dir: Path
    autocad_skill_dir: Path
