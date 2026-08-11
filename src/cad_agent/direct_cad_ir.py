from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .cad_ir import CADIRCompiler, CAD_IR_VERSION
from .planner_candidate import CANDIDATE_SCHEMA_VERSION, PLANNING_CONTRACT_VERSION
from .planner_validator import PlannerValidator
from .stage_planner import apply_stage_plan


DIRECT_CAD_IR_PROVIDER = "direct_cad_ir"
_MAX_CAD_IR_FILE_BYTES = 16 * 1024 * 1024
_FENCED_JSON_PATTERN = re.compile(
    r"```(?:json|cad-ir|cad_ir|text|plaintext|path)?\s*(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_JSON_LINK_PATTERN = re.compile(
    r"^\s*\[[^\]]+\]\(\s*<?(.+?\.json)>?\s*\)\s*$",
    flags=re.IGNORECASE,
)
_OBJECT_BODY_PATTERN = re.compile(r'^"(?:[^"\\]|\\.)+"\s*:')
_STAGES = ("model_3d", "drawing", "autocad_annotation", "export_files")
_TASK_STAGES = {
    "model_3d": ["model_3d"],
    "modify_3d": ["model_3d"],
    "create_drawing": ["drawing"],
    "annotate_drawing": ["autocad_annotation"],
    "export_files": ["export_files"],
    "full_pipeline": list(_STAGES),
}
_STOP_AFTER = {
    "model_3d": "model_3d",
    "modify_3d": "model_3d",
    "create_drawing": "drawing",
    "annotate_drawing": "autocad_annotation",
    "export_files": "export_files",
    "full_pipeline": "export_files",
}
_DEFAULT_OUTPUTS = {
    "model_3d": ["SLDPRT"],
    "modify_3d": [],
    "create_drawing": ["SLDDRW"],
    "annotate_drawing": ["Annotated DWG", "annotation_report_json"],
    "export_files": [],
    "full_pipeline": ["SLDPRT", "SLDDRW"],
}
_OUTPUT_NAMES = {
    "sldprt": "SLDPRT",
    "sldasm": "SLDASM",
    "slddrw": "SLDDRW",
    "step": "STEP",
    "stp": "STEP",
    "stl": "STL",
    "dwg": "DWG",
    "dxf": "DXF",
    "pdf": "PDF",
    "annotated dwg": "Annotated DWG",
    "annotation_report_json": "annotation_report_json",
    "brain_plan_json": "brain_plan_json",
    "design_plan_json": "design_plan_json",
    "pipeline_report_json": "pipeline_report_json",
}


class DirectCADIRInputError(ValueError):
    """Raised when GUI text is not a supported CAD-IR JSON document."""


class DirectCADIRService:
    """Turn direct GUI CAD-IR JSON into an authorized deterministic plan."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.compiler = CADIRCompiler()
        self.validator = PlannerValidator()
        self.input_format = "json"

    def plan(self, text: str, stage_mode: str = "auto") -> dict[str, Any]:
        raw = self._parse_json(text)
        design = self._to_design(raw)
        task_type = self._task_type(raw, design, stage_mode)
        design["task_type"] = task_type
        design.setdefault("source_brief", "Direct CAD-IR GUI input")
        design.setdefault("part_family", str(raw.get("part_type") or "cad_ir_part"))
        design.setdefault("parameters", {"unit": "mm"})
        design.setdefault("features", [])
        design.setdefault("unsupported_features", [])
        design.setdefault("risks", [])
        design.setdefault("assumptions", [])
        design.setdefault("unresolved", [])
        design.setdefault("review_plan", {"expected_outputs": [], "views": [], "checks": []})

        first_compile = self.compiler.compile(design)
        normalized = first_compile.design
        stage_plan = self._stage_plan(raw, normalized, task_type)
        normalized = apply_stage_plan(normalized, stage_plan)
        normalized = self.compiler.compile(normalized).design

        candidate = self._candidate_from_design(normalized)
        validation = self.validator.validate(candidate, normalized)
        run_dir = self._write_artifacts(raw, candidate, validation, normalized)

        normalized["planning_mode"] = DIRECT_CAD_IR_PROVIDER
        normalized["direct_cad_ir_input_format"] = self.input_format
        normalized["planning_contract"] = PLANNING_CONTRACT_VERSION
        normalized["planning_validation"] = copy.deepcopy(validation)
        normalized["planning_preview"] = self.validator.preview(
            candidate,
            validation,
            DIRECT_CAD_IR_PROVIDER,
        )
        normalized["planning_artifacts"] = {
            "run_dir": str(run_dir),
            "direct_cad_ir_input": str(run_dir / "direct_cad_ir_input.json"),
            "candidate_plan": str(run_dir / "candidate_plan.json"),
            "planner_validation": str(run_dir / "planner_validation.json"),
            "normalized_design": str(run_dir / "normalized_design.json"),
        }
        normalized["llm_provider"] = {
            "id": DIRECT_CAD_IR_PROVIDER,
            "model": "deterministic",
            "role": "llm_disabled",
            "enabled": False,
        }
        normalized["planner_agent"] = {
            "name": "DirectCADIRService",
            "provider": DIRECT_CAD_IR_PROVIDER,
            "llm_enabled": False,
        }

        status = str(validation.get("status") or "blocked")
        normalized["needs_confirmation"] = status != "approved"
        if status == "blocked":
            normalized["confirmation_reason"] = self._first_error(validation)
            policy = dict(normalized.get("execution_policy") or {})
            policy["allowed_skills"] = []
            normalized["execution_policy"] = policy
        else:
            normalized["confirmation_reason"] = ""
        return normalized

    def _parse_json(self, text: str) -> dict[str, Any]:
        source = str(text or "").lstrip("\ufeff").strip()
        if not source:
            raise DirectCADIRInputError("CAD-IR input is empty.")
        source, input_format = self._normalize_input_source(source)
        self.input_format = input_format
        try:
            value = json.loads(source)
        except json.JSONDecodeError as exc:
            repaired = self._repair_missing_outer_object(source)
            if repaired is None:
                raise DirectCADIRInputError(
                    f"CAD-IR must be one JSON object (line {exc.lineno}, column {exc.colno}): {exc.msg}"
                ) from None
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError:
                raise DirectCADIRInputError(
                    f"CAD-IR must be one JSON object (line {exc.lineno}, column {exc.colno}): {exc.msg}"
                ) from None
            self.input_format = f"{input_format}_outer_object_repaired"
        if not isinstance(value, dict):
            raise DirectCADIRInputError("CAD-IR root must be a JSON object.")
        return value

    @classmethod
    def _normalize_input_source(cls, source: str) -> tuple[str, str]:
        fenced = list(_FENCED_JSON_PATTERN.finditer(source))
        if fenced:
            if len(fenced) != 1:
                raise DirectCADIRInputError("CAD-IR input must contain exactly one JSON code block.")
            source = fenced[0].group(1).strip()
            if not source:
                raise DirectCADIRInputError("CAD-IR JSON code block is empty.")
            input_format = "markdown_json_block"
        else:
            input_format = "json"

        path_text = source
        markdown_link = _MARKDOWN_JSON_LINK_PATTERN.fullmatch(source)
        if markdown_link:
            path_text = markdown_link.group(1).strip()
            input_format = "markdown_json_file_link"
        else:
            path_text = path_text.strip().strip("`").strip()
            if (
                len(path_text) >= 2
                and path_text[0] == path_text[-1]
                and path_text[0] in {"'", '"'}
            ):
                path_text = path_text[1:-1].strip()

        if cls._looks_like_json_path(path_text):
            path = Path(path_text).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            if not path.is_file():
                raise DirectCADIRInputError(f"CAD-IR JSON file does not exist: {path}")
            size = path.stat().st_size
            if size <= 0:
                raise DirectCADIRInputError(f"CAD-IR JSON file is empty: {path}")
            if size > _MAX_CAD_IR_FILE_BYTES:
                raise DirectCADIRInputError(
                    f"CAD-IR JSON file exceeds {_MAX_CAD_IR_FILE_BYTES // (1024 * 1024)} MB: {path}"
                )
            try:
                source = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError) as exc:
                raise DirectCADIRInputError(f"Unable to read CAD-IR JSON file: {path}: {exc}") from None
            input_format = (
                "markdown_json_file_link"
                if input_format == "markdown_json_file_link"
                else "json_file_path"
            )
        return source, input_format

    @staticmethod
    def _looks_like_json_path(source: str) -> bool:
        value = str(source or "").strip()
        return (
            bool(value)
            and "\n" not in value
            and "\r" not in value
            and len(value) <= 4096
            and not value.startswith(("{", "["))
            and value.lower().endswith(".json")
        )

    @staticmethod
    def _repair_missing_outer_object(source: str) -> str | None:
        body = str(source or "").strip()
        if not _OBJECT_BODY_PATTERN.match(body):
            return None
        if not body.endswith("}"):
            body = f"{body}}}"
        return f"{{{body}"

    def _to_design(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("candidate_schema_version"):
            raise DirectCADIRInputError(
                "Planner candidate JSON is not executable CAD-IR. Paste cad.ir.v1 or vibecad.design.v1 JSON."
            )
        if raw.get("version") == CAD_IR_VERSION:
            return self._canonical_ir_to_design(raw)
        if isinstance(raw.get("cad_ir"), dict) and raw["cad_ir"].get("version") == CAD_IR_VERSION:
            if isinstance(raw.get("features"), list):
                result = copy.deepcopy(raw)
                self._clear_old_planning_state(result)
                return result
            return self._canonical_ir_to_design(raw["cad_ir"], wrapper=raw)
        if raw.get("schema_version") == "vibecad.design.v1" and isinstance(raw.get("features"), list):
            result = copy.deepcopy(raw)
            self._clear_old_planning_state(result)
            return result
        raise DirectCADIRInputError(
            "Unsupported JSON schema. Expected version='cad.ir.v1' or schema_version='vibecad.design.v1'."
        )

    @staticmethod
    def _clear_old_planning_state(design: dict[str, Any]) -> None:
        for key in (
            "cad_ir",
            "cad_ir_validation",
            "planning_contract",
            "planning_validation",
            "planning_preview",
            "planning_artifacts",
            "planning_retry",
            "execution_policy",
            "requested_stages",
            "forbidden_stages",
            "stop_after",
            "llm_provider",
            "planner_agent",
        ):
            design.pop(key, None)
        design["needs_confirmation"] = False
        design["confirmation_reason"] = ""
        design["unsupported_features"] = [
            item
            for item in list(design.get("unsupported_features") or [])
            if not (isinstance(item, dict) and item.get("type") == "cad_ir_validation_error")
        ]

    def _canonical_ir_to_design(
        self,
        ir: dict[str, Any],
        wrapper: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        features: list[dict[str, Any]] = []
        for index, item in enumerate(list(ir.get("features") or []), start=1):
            if not isinstance(item, dict):
                features.append(item)
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            params = copy.deepcopy(item.get("options") or {})
            params.update(copy.deepcopy(item.get("parameters") or {}))
            dependencies = self._dependency_ids(item.get("dependencies"))
            target_reference = None
            if target.get("feature_ref") or target.get("face_role"):
                target_reference = {
                    "feature_id": target.get("feature_ref"),
                    "role": target.get("face_role"),
                }
            local_frame = target.get("local_frame")
            if isinstance(local_frame, dict) and any(value is not None for value in local_frame.values()):
                params["geometry_context"] = {
                    "target_body": target.get("body_ref"),
                    "target_feature": target.get("feature_ref"),
                    "target_face_role": target.get("face_role"),
                    "face_origin": local_frame.get("origin_mm"),
                    "local_u_axis": local_frame.get("u_axis"),
                    "local_v_axis": local_frame.get("v_axis"),
                    "face_normal": local_frame.get("normal"),
                }
            feature = {
                # CAD-IR dependencies reference stable ids, not display names.
                "name": str(item.get("id") or item.get("name") or f"feature_{index}"),
                "display_name": str(item.get("name") or item.get("id") or f"feature_{index}"),
                "type": str(item.get("operation") or ""),
                "required": bool(item.get("required", True)),
                "params": params,
                "depends_on": dependencies,
                "target_body": target.get("body_ref"),
                "target_reference": target_reference,
            }
            features.append(feature)

        source = wrapper or ir
        parameters = copy.deepcopy(source.get("parameters") or {})
        parameters["unit"] = "mm"
        return {
            "schema_version": "vibecad.design.v1",
            "source_brief": "Direct CAD-IR GUI input",
            "part_family": str(source.get("part_family") or source.get("part_type") or "cad_ir_part"),
            "task_type": str(source.get("task_type") or ir.get("task_type") or "model_3d"),
            "parameters": parameters,
            "features": features,
            "outputs": copy.deepcopy(source.get("outputs") or ir.get("outputs") or []),
            "reconstruction_contract": copy.deepcopy(
                source.get("reconstruction_contract")
                or ir.get("reconstruction_contract")
                or {}
            ),
            "unsupported_features": copy.deepcopy(
                source.get("unsupported_features")
                or ir.get("unsupported_features")
                or []
            ),
            "risks": copy.deepcopy(source.get("risks") or ir.get("risks") or []),
            "assumptions": copy.deepcopy(
                source.get("assumptions")
                or ir.get("assumptions")
                or []
            ),
            "unresolved": copy.deepcopy(
                source.get("unresolved")
                or ir.get("unresolved")
                or []
            ),
        }

    @staticmethod
    def _dependency_ids(value: Any) -> list[str]:
        entries = value if isinstance(value, list) else ([] if value is None else [value])
        result: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                entry = entry.get("feature_id") or entry.get("ref")
            token = str(entry or "").strip()
            if token and token not in result:
                result.append(token)
        return result

    @staticmethod
    def _task_type(raw: dict[str, Any], design: dict[str, Any], stage_mode: str) -> str:
        explicit_mode = str(stage_mode or "auto").strip()
        if explicit_mode != "auto":
            if explicit_mode not in _TASK_STAGES:
                raise DirectCADIRInputError(f"Unsupported GUI task mode: {explicit_mode!r}.")
            return explicit_mode
        task_type = str(raw.get("task_type") or design.get("task_type") or "model_3d").strip()
        if task_type not in _TASK_STAGES:
            raise DirectCADIRInputError(f"Unsupported CAD-IR task_type: {task_type!r}.")
        return task_type

    def _stage_plan(
        self,
        raw: dict[str, Any],
        design: dict[str, Any],
        task_type: str,
    ) -> dict[str, Any]:
        requested = raw.get("requested_stages")
        if not isinstance(requested, list) or not requested:
            requested = list(_TASK_STAGES[task_type])
        requested = [str(item) for item in requested if str(item) in _STAGES]
        if not requested:
            raise DirectCADIRInputError("CAD-IR requested_stages does not contain a supported stage.")
        outputs = self._outputs(raw.get("outputs") or design.get("outputs"), task_type)
        if requested == ["export_files"] and not outputs:
            design["needs_confirmation"] = True
            design["confirmation_reason"] = "export_files requires at least one explicit output format."
            design.setdefault("unsupported_features", []).append({
                "type": "direct_cad_ir_output_missing",
                "required": True,
                "reason": design["confirmation_reason"],
            })
        return {
            "task_type": task_type,
            "requested_stages": requested,
            "forbidden_stages": [stage for stage in _STAGES if stage not in requested],
            "stop_after": str(raw.get("stop_after") or _STOP_AFTER[task_type]),
            "requested_outputs": outputs,
            "needs_confirmation": bool(design.get("needs_confirmation", False)),
            "confirmation_reason": str(design.get("confirmation_reason") or ""),
            "stage_mode": "direct_cad_ir",
        }

    @staticmethod
    def _outputs(value: Any, task_type: str) -> list[str]:
        entries = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        if not entries:
            entries = list(_DEFAULT_OUTPUTS[task_type])
        result: list[str] = []
        for entry in entries:
            token = str(entry).strip()
            output = _OUTPUT_NAMES.get(token.lower(), token)
            if output and output not in result:
                result.append(output)
        return result

    def _candidate_from_design(self, design: dict[str, Any]) -> dict[str, Any]:
        ir = design.get("cad_ir") or {}
        features = []
        for item in list(ir.get("features") or []):
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            target_reference = None
            if target.get("feature_ref") or target.get("face_role"):
                target_reference = {
                    "feature_id": target.get("feature_ref"),
                    "role": target.get("face_role"),
                }
            parameters = copy.deepcopy(item.get("options") or {})
            parameters.update(copy.deepcopy(item.get("parameters") or {}))
            features.append({
                "id": str(item.get("id") or item.get("name") or "feature"),
                "operation": str(item.get("operation") or ""),
                "depends_on": self._dependency_ids(item.get("dependencies")),
                "target_body": str(target.get("body_ref") or "primary_solid"),
                "target_reference": target_reference,
                "parameters": parameters,
                "evidence": ["Explicit direct CAD-IR GUI input."],
                "assumptions": [],
                "unresolved": [],
                "confidence": 1.0,
            })
        return {
            "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
            "source_prompt": "Direct CAD-IR GUI input",
            "part_type": str(design.get("part_family") or "cad_ir_part"),
            "task_type": str(design.get("task_type") or "model_3d"),
            "parameters": copy.deepcopy(design.get("parameters") or {"unit": "mm"}),
            "features": features,
            "outputs": list(design.get("outputs") or []),
            "assumptions": copy.deepcopy(design.get("assumptions") or []),
            "unresolved": copy.deepcopy(design.get("unresolved") or []),
            "confidence": 1.0,
        }

    def _write_artifacts(
        self,
        parsed_input: dict[str, Any],
        candidate: dict[str, Any],
        validation: dict[str, Any],
        design: dict[str, Any],
    ) -> Path:
        run_dir = self.output_root / f"direct_cad_ir_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "direct_cad_ir_input.json").write_text(
            json.dumps(parsed_input, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "candidate_plan.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "planner_validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "normalized_design.json").write_text(
            json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return run_dir

    @staticmethod
    def _first_error(validation: dict[str, Any]) -> str:
        errors = list(validation.get("errors") or [])
        if errors:
            return str(errors[0].get("message") or "CAD-IR validation failed.")
        return "CAD-IR validation failed."
