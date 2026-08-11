from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class PlanningRegressionStore:
    """Append-only audit store for planner mistakes and later corrections."""

    def __init__(self, output_root: Path) -> None:
        self.root = output_root / "planning_regressions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "planning_errors.jsonl"

    def record(
        self,
        *,
        original_prompt: str,
        provider: str,
        raw_llm_output: Any,
        normalized_cad_ir: dict[str, Any],
        validation_errors: list[dict[str, Any]],
        user_correction: str | None = None,
        final_correct_cad_ir: dict[str, Any] | None = None,
        execution_result: dict[str, Any] | None = None,
    ) -> Path:
        case_id = f"planner_case_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        payload = {
            "case_id": case_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "original_prompt": original_prompt,
            "provider": provider,
            "raw_llm_output": raw_llm_output,
            "normalized_cad_ir": normalized_cad_ir,
            "validation_errors": validation_errors,
            "user_correction": user_correction,
            "final_correct_cad_ir": final_correct_cad_ir,
            "execution_result": execution_result,
        }
        case_path = self.root / f"{case_id}.json"
        case_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.index_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return case_path

    def complete_case(
        self,
        case_path: str | Path,
        *,
        user_correction: str,
        final_correct_cad_ir: dict[str, Any],
        execution_result: dict[str, Any],
    ) -> Path:
        path = Path(case_path)
        if not path.is_file() or path.parent.resolve() != self.root.resolve():
            raise ValueError("planning_regression_case_not_found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update({
            "updated_at": datetime.now().astimezone().isoformat(),
            "user_correction": user_correction,
            "final_correct_cad_ir": final_correct_cad_ir,
            "execution_result": execution_result,
        })
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.index_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path
