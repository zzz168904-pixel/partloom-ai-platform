from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .models import SkillResult
from .revolve_skill import RevolveSkill


class ParametricManagementSkill:
    """Manage explicit SolidWorks equations and Part configurations."""

    FEATURE_TYPES = {"configuration", "equation"}
    CONFIG_OPTION_BITS = {
        "use_alternate_name": 1,
        "exclude_from_bom": 2,
        "suppress_new_features": 4,
        "hide_new_components": 8,
        "minimize_feature_manager": 16,
        "inherit_properties": 32,
        "link_to_parent": 64,
        "dont_activate": 128,
        "dissolve_in_bom": 256,
        "use_description_in_bom": 512,
    }
    CONFIG_SCOPES = {"this": 1, "all": 2, "specified": 3}

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in self.FEATURE_TYPES:
            return SkillResult(False, f"Unsupported parametric feature type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"{feature_type}_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            validation = RevolveSkill._validate_active_part(model, require_body=True)
            if not validation.get("success"):
                return self._result(False, str(validation.get("message")), report_path, validation)

            operations: list[dict[str, Any]] = []
            for index, feature in enumerate(features):
                request = self.normalize_request(feature_type, feature.get("params", {}))
                if not request.get("success"):
                    return self._result(False, str(request.get("message")), report_path, {
                        "active_doc": validation.get("active_doc"),
                        "feature_type": feature_type,
                        "operations": operations,
                        "invalid_request": request,
                    })
                if feature_type == "configuration":
                    operation = self._create_configuration(model, feature, request, get_com_member)
                else:
                    operation = self._create_equation(model, feature, request, get_com_member)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(False, str(operation.get("message")), report_path, {
                        "active_doc": validation.get("active_doc"),
                        "feature_type": feature_type,
                        "operations": operations,
                    })

            model.ForceRebuild3(False)
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "feature_type": feature_type,
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "configuration_names": self._configuration_names(model, get_com_member),
                "equations": self._equation_snapshot(model, get_com_member),
                "side_effects": {
                    "modifies_active_doc": True,
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": [],
            }
            return self._result(True, f"SolidWorks {feature_type} created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"{feature_type} failed: {exc}", report_path, {
                "feature_type": feature_type,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, feature_type: str, params: dict[str, Any]) -> dict[str, Any]:
        if feature_type == "configuration":
            return cls._normalize_configuration(params)
        if feature_type == "equation":
            return cls._normalize_equation(params)
        return {"success": False, "message": f"Unsupported feature type: {feature_type}"}

    @classmethod
    def _normalize_configuration(cls, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or params.get("configuration_name") or "").strip()
        if not name or len(name) > 200 or any(char in name for char in '\\/:*?"<>|@\r\n'):
            return {"success": False, "message": "configuration requires a valid explicit name."}
        comment = str(params.get("comment") or "").strip()
        alternate_name = str(params.get("alternate_name") or "").strip()
        activate = bool(params.get("activate", False))
        option_values = params.get("options") if isinstance(params.get("options"), dict) else {}
        options = 0
        normalized_options: dict[str, bool] = {}
        for key, bit in cls.CONFIG_OPTION_BITS.items():
            enabled = bool(option_values.get(key, params.get(key, False)))
            if key == "use_alternate_name" and alternate_name:
                enabled = True
            if key == "dont_activate":
                enabled = not activate
            normalized_options[key] = enabled
            if enabled:
                options |= bit
        return {
            "success": True,
            "name": name,
            "comment": comment,
            "alternate_name": alternate_name,
            "activate": activate,
            "options": normalized_options,
            "options_mask": options,
        }

    @classmethod
    def _normalize_equation(cls, params: dict[str, Any]) -> dict[str, Any]:
        expression = str(params.get("expression") or "").strip()
        kind = str(params.get("kind") or "global_variable").strip().lower()
        if kind not in {"global_variable", "dimension"}:
            return {"success": False, "message": "equation kind must be global_variable or dimension."}
        if not expression:
            name = str(params.get("name") or params.get("target") or "").strip().strip('"')
            if not name or len(name) > 200 or any(char in name for char in '"=\r\n'):
                return {"success": False, "message": "equation requires a valid name or target."}
            if kind == "dimension" and "@" not in name:
                return {"success": False, "message": "dimension equation target must include a SolidWorks dimension path such as D1@Feature."}
            rhs_value = params.get("rhs")
            if rhs_value in (None, ""):
                rhs_value = params.get("value")
                if rhs_value in (None, ""):
                    return {"success": False, "message": "equation requires expression, rhs, or value."}
                try:
                    rhs = f"{float(rhs_value):g}"
                except (TypeError, ValueError):
                    return {"success": False, "message": "equation value must be numeric when rhs is omitted."}
                unit = str(params.get("unit") or "").strip().lower()
                if unit not in {"", "mm", "cm", "m", "in", "deg", "rad"}:
                    return {"success": False, "message": "equation unit must be mm, cm, m, in, deg, rad, or empty."}
                rhs += unit
            else:
                rhs = str(rhs_value).strip()
            expression = f'"{name}" = {rhs}'
        match = re.fullmatch(r'\s*"([^"\r\n=]+)"\s*=\s*(.+?)\s*', expression)
        if not match or len(expression) > 1000:
            return {"success": False, "message": "equation must use SolidWorks syntax: \"Name\" = expression."}
        lhs, rhs = match.group(1).strip(), match.group(2).strip()
        if not lhs or not rhs or any(char in rhs for char in "\r\n;"):
            return {"success": False, "message": "equation contains an empty or unsafe right-hand expression."}
        if kind == "dimension" and "@" not in lhs:
            return {"success": False, "message": "dimension equation target must include @Feature."}
        scope = str(params.get("scope") or "all").strip().lower()
        if scope not in cls.CONFIG_SCOPES:
            return {"success": False, "message": "equation scope must be this, all, or specified."}
        configurations = params.get("configurations") or params.get("configuration_names") or []
        if isinstance(configurations, str):
            configurations = [configurations]
        if not isinstance(configurations, list) or any(not str(item).strip() for item in configurations):
            return {"success": False, "message": "equation configurations must be a list of names."}
        configurations = [str(item).strip() for item in configurations]
        if scope == "specified" and not configurations:
            return {"success": False, "message": "specified equation scope requires configuration names."}
        return {
            "success": True,
            "kind": kind,
            "expression": f'"{lhs}" = {rhs}',
            "lhs": lhs,
            "rhs": rhs,
            "scope": scope,
            "scope_option": cls.CONFIG_SCOPES[scope],
            "configurations": configurations,
            "solve": bool(params.get("solve", True)),
        }

    @classmethod
    def _create_configuration(cls, model: Any, feature: dict[str, Any], request: dict[str, Any], get_member: Any) -> dict[str, Any]:
        before = cls._configuration_names(model, get_member)
        if request["name"] in before:
            return {"success": False, "message": f"Configuration already exists: {request['name']}"}
        created = model.AddConfiguration3(
            request["name"],
            request["comment"],
            request["alternate_name"],
            int(request["options_mask"]),
        )
        if created is None:
            return {"success": False, "message": "SolidWorks AddConfiguration3 returned no configuration."}
        if request["activate"] and not bool(model.ShowConfiguration2(request["name"])):
            return {"success": False, "message": f"Configuration was created but could not be activated: {request['name']}"}
        after = cls._configuration_names(model, get_member)
        if request["name"] not in after or len(after) != len(before) + 1:
            return {"success": False, "message": "Configuration list did not contain exactly one requested addition."}
        active = get_member(get_member(model, "ConfigurationManager"), "ActiveConfiguration")
        active_name = str(get_member(active, "Name") or "") if active is not None else ""
        return {
            "success": True,
            "name": str(feature.get("name") or request["name"]),
            "type": "configuration",
            "request": request,
            "configuration_name": request["name"],
            "configuration_names_before": before,
            "configuration_names_after": after,
            "active_configuration": active_name,
        }

    @classmethod
    def _create_equation(cls, model: Any, feature: dict[str, Any], request: dict[str, Any], get_member: Any) -> dict[str, Any]:
        manager = get_member(model, "GetEquationMgr")
        if manager is None:
            return {"success": False, "message": "SolidWorks did not provide EquationMgr."}
        configurations = cls._configuration_names(model, get_member)
        unknown = sorted(set(request["configurations"]) - set(configurations))
        if unknown:
            return {"success": False, "message": f"Equation references unknown configurations: {unknown}"}
        active_configuration = get_member(get_member(model, "ConfigurationManager"), "ActiveConfiguration")
        active_name = str(get_member(active_configuration, "Name") or "") if active_configuration is not None else ""
        if request["scope"] == "specified" and active_name not in request["configurations"]:
            return {"success": False, "message": "Specified equation scope must include the current configuration."}
        before_count = int(get_member(manager, "GetCount") or 0)
        if len(configurations) > 1:
            config_names = tuple(request["configurations"]) if request["scope"] == "specified" else ()
            index = manager.Add3(
                -1,
                request["expression"],
                bool(request["solve"]),
                int(request["scope_option"]),
                config_names,
            )
        else:
            if request["scope"] == "specified" and request["configurations"] != configurations:
                return {"success": False, "message": "Specified equation scope does not match the only configuration."}
            index = manager.Add2(-1, request["expression"], bool(request["solve"]))
        if int(index) < 0:
            return {"success": False, "message": "SolidWorks EquationMgr failed to add the equation."}
        evaluate_status = get_member(manager, "EvaluateAll")
        after_count = int(get_member(manager, "GetCount") or 0)
        stored = str(get_member(manager, "Equation", int(index)) or "")
        value = get_member(manager, "Value", int(index))
        is_global = bool(get_member(manager, "GlobalVariable", int(index)))
        try:
            status = int(get_member(manager, "Status"))
        except Exception:
            status = -1
        if after_count != before_count + 1 or request["lhs"] not in stored:
            return {"success": False, "message": "Equation was not persisted exactly once in EquationMgr."}
        if request["kind"] == "global_variable" and not is_global:
            return {"success": False, "message": "Equation was not recognized as a SolidWorks global variable."}
        return {
            "success": True,
            "name": str(feature.get("name") or request["lhs"]),
            "type": "equation",
            "request": request,
            "equation_index": int(index),
            "stored_expression": stored,
            "evaluated_value": value,
            "evaluate_status": evaluate_status,
            "equation_status": status,
            "is_global_variable": is_global,
            "equation_count_before": before_count,
            "equation_count_after": after_count,
        }

    @staticmethod
    def _configuration_names(model: Any, get_member: Any) -> list[str]:
        values = get_member(model, "GetConfigurationNames") or ()
        if not isinstance(values, (tuple, list)):
            values = (values,)
        return [str(item) for item in values if item is not None]

    @staticmethod
    def _equation_snapshot(model: Any, get_member: Any) -> list[dict[str, Any]]:
        manager = get_member(model, "GetEquationMgr")
        if manager is None:
            return []
        count = int(get_member(manager, "GetCount") or 0)
        return [
            {
                "index": index,
                "expression": str(get_member(manager, "Equation", index) or ""),
                "value": get_member(manager, "Value", index),
                "global_variable": bool(get_member(manager, "GlobalVariable", index)),
            }
            for index in range(count)
        ]

    def _ensure_imports(self) -> None:
        if not self.script_dir.exists():
            raise RuntimeError(f"SolidWorks automation scripts not found: {self.script_dir}")
        script_text = str(self.script_dir)
        if script_text not in sys.path:
            sys.path.insert(0, script_text)
        os.environ["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["CAD_AGENT_NO_INTERACTIVE"] = "1"

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        payload["files"] = list(dict.fromkeys(
            [str(item) for item in payload.get("files", [])] + [str(report_path)]
        ))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success,
            message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
