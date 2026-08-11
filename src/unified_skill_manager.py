from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cad_agent import CADAgentSkillManager


SOLIDWORKS_SKILL_DIR = Path.home() / ".codex" / "skills" / "solidworks-automation"
SUBSKILL_DIR = SOLIDWORKS_SKILL_DIR / "subskills"
AUTOCAD_SKILL_DIR = SUBSKILL_DIR / "autocad-automation"
VIBECAD_SKILL_DIR = SUBSKILL_DIR / "solidworks-vibecad"
THREADED_HOLES_SKILL_DIR = SUBSKILL_DIR / "solidworks-threaded-holes"
CNC_FILLET_SKILL_DIR = SUBSKILL_DIR / "solidworks-fillet-chamfer-cnc"


@dataclass(frozen=True)
class SkillDefinition:
    key: str
    name: str
    description: str
    root: Path
    preflight_script: Path
    keywords: tuple[str, ...]
    entry_script: Path | None = None


@dataclass(frozen=True)
class SkillRoute:
    skill_key: str
    skill_name: str
    actions: list[str]
    confidence: float
    reason: str


class UnifiedSkillManager:
    def __init__(self, output_root: Path | None = None) -> None:
        self.output_root = output_root or (Path.cwd() / "logs" / "skill_outputs")
        self.agent = CADAgentSkillManager(self.output_root)
        sw_preflight = SOLIDWORKS_SKILL_DIR / "scripts" / "sw_preflight.py"
        self.skills = {
            "solidworks": SkillDefinition(
                "solidworks",
                "SolidWorks Automation",
                "Parts, assemblies, drawings, export, review",
                SOLIDWORKS_SKILL_DIR,
                sw_preflight,
                ("solidworks", "sw", "零件", "装配", "建模", "三视图", "工程图", "step", "stl", "sldprt", "sldasm"),
            ),
            "solidworks_vibecad": SkillDefinition(
                "solidworks_vibecad",
                "SolidWorks VibeCAD",
                "Natural language to parametric design plan",
                VIBECAD_SKILL_DIR,
                sw_preflight,
                ("vibecad", "text-to-cad", "参数化", "设计计划", "建模计划", "自然语言建模", "规划"),
                VIBECAD_SKILL_DIR / "scripts" / "plan_from_brief.py",
            ),
            "solidworks_threaded_holes": SkillDefinition(
                "solidworks_threaded_holes",
                "SolidWorks Threaded Holes",
                "Tapped holes, pilot holes, chamfers, thread metadata",
                THREADED_HOLES_SKILL_DIR,
                sw_preflight,
                ("threaded", "thread", "螺纹", "螺丝孔", "攻丝", "攻牙", "底孔", "m3", "m4", "m5", "m6", "m8", "m10", "m12"),
                THREADED_HOLES_SKILL_DIR / "scripts" / "create_threaded_hole_template.py",
            ),
            "solidworks_cnc_fillet": SkillDefinition(
                "solidworks_cnc_fillet",
                "SolidWorks CNC Fillet/Chamfer",
                "CNC blocks, mounts, fillets, chamfers, pockets",
                CNC_FILLET_SKILL_DIR,
                sw_preflight,
                ("cnc", "fillet", "chamfer", "圆角", "倒角", "机加工", "减重口袋", "安装座", "连接块", "支架"),
                CNC_FILLET_SKILL_DIR / "scripts" / "create_cnc_mount_template.py",
            ),
            "autocad": SkillDefinition(
                "autocad",
                "AutoCAD / DWG Automation",
                "DWG/DXF import, 2D drawing, layer operations, CAD review",
                AUTOCAD_SKILL_DIR,
                AUTOCAD_SKILL_DIR / "scripts" / "acad_preflight.py",
                ("autocad", "cad", "dwg", "dxf", "二维", "图层", "线稿", "导入", "解析"),
            ),
        }

    def route(self, command: str, actions: list[str]) -> SkillRoute | None:
        text = command.lower()
        agent_route = self.agent.route(command, actions)
        if agent_route is not None:
            mapped_actions = ["vibecad_plan"] if agent_route.actions == ["vibecad_agent_plan"] else agent_route.actions
            return self._route(agent_route.skill_key, mapped_actions, agent_route.confidence, agent_route.reason)
        if self._contains_any(text, ("环境检测", "自检", "preflight", "检查 skill", "检查skill")):
            for key in ("solidworks_threaded_holes", "solidworks_cnc_fillet", "solidworks_vibecad", "autocad"):
                if self._contains_any(text, self.skills[key].keywords):
                    return self._route(key, ["skill_preflight"], 0.95, "requested skill preflight")
            return self._route("solidworks", ["skill_preflight"], 0.9, "requested SolidWorks preflight")

        if actions:
            return self._route("solidworks", actions, 0.98, "matched existing SolidWorks drawing workflow")
        if self._contains_any(text, self.skills["solidworks_vibecad"].keywords):
            return self._route("solidworks_vibecad", ["vibecad_plan"], 0.82, "matched VibeCAD planning keywords")
        if self._contains_any(text, self.skills["solidworks_threaded_holes"].keywords):
            return self._route("solidworks_threaded_holes", ["threaded_hole_template"], 0.88, "matched threaded-hole keywords")
        if self._contains_any(text, self.skills["solidworks_cnc_fillet"].keywords):
            return self._route("solidworks_cnc_fillet", ["cnc_mount_template"], 0.84, "matched CNC fillet/chamfer keywords")
        if self._contains_any(text, self.skills["autocad"].keywords):
            return self._route("autocad", ["skill_status"], 0.72, "matched DWG/DXF/CAD keywords")
        if self._contains_any(text, self.skills["solidworks"].keywords):
            return self._route("solidworks", ["skill_status"], 0.68, "matched SolidWorks keywords")
        if self._contains_any(text, ("图片", "图像", "识别", "pdf")):
            return self._route("solidworks_vibecad", ["vibecad_plan"], 0.55, "defaulted recognition/modeling request to VibeCAD planning")
        return None

    def status_lines(self) -> list[str]:
        lines = self.agent.status_lines()
        lines.append("--- legacy skill registry ---")
        for skill in self.skills.values():
            installed = "installed" if skill.root.exists() else "missing"
            preflight = "preflight ok" if skill.preflight_script.exists() else "preflight missing"
            entry = ""
            if skill.entry_script is not None:
                entry = ", entry ok" if skill.entry_script.exists() else ", entry missing"
            lines.append(f"{skill.name}: {installed}, {preflight}{entry}")
        lines.append(f"Python COM: {self._module_status('pythoncom')}, win32com: {self._module_status('win32com')}")
        return lines

    def run_preflight(self, skill_key: str, timeout_s: int = 20) -> dict[str, object]:
        skill = self.skills.get(skill_key)
        if skill is None:
            return {"success": False, "message": f"Unknown skill: {skill_key}"}
        if not skill.preflight_script.exists():
            return {"success": False, "message": f"{skill.name} missing preflight script: {skill.preflight_script}"}
        command = [sys.executable, str(skill.preflight_script)]
        if skill.preflight_script.name == "sw_preflight.py":
            command.append("--no-install")
        process = self._run_process(command, cwd=skill.root, timeout_s=timeout_s)
        return {
            "success": process["returncode"] == 0,
            "message": f"{skill.name} preflight finished with exit code {process['returncode']}",
            "output": process["output"],
        }

    def run_vibecad_plan(self, brief: str) -> dict[str, object]:
        result = self.agent.run_vibecad_plan(brief)
        return {
            "success": result.success,
            "message": result.message,
            "output": result.output,
            "path": result.path,
            "data": result.data,
        }

    def run_threaded_hole_template(
        self,
        command: str,
        timeout_s: int = 120,
        *,
        allow_test_template: bool = False,
    ) -> dict[str, object]:
        if not allow_test_template:
            return {
                "success": False,
                "message": "blocked_by_cad_ir_gate: legacy threaded-hole template is test-only",
                "error": "legacy_test_entry_blocked",
            }
        skill = self.skills["solidworks_threaded_holes"]
        if skill.entry_script is None or not skill.entry_script.exists():
            return {"success": False, "message": f"{skill.name} entry script missing"}
        run_dir = self._new_run_dir("threaded_hole")
        thread = self._detect_thread(command) or "M6"
        process = self._run_process(
            [sys.executable, str(skill.entry_script), "--thread", thread, "--output-dir", str(run_dir)],
            cwd=skill.root,
            timeout_s=timeout_s,
        )
        return {
            "success": process["returncode"] == 0,
            "message": f"Threaded-hole template finished with exit code {process['returncode']}: {run_dir}",
            "output": process["output"],
            "path": str(run_dir),
        }

    def run_cnc_mount_template(
        self,
        timeout_s: int = 120,
        *,
        allow_test_template: bool = False,
    ) -> dict[str, object]:
        if not allow_test_template:
            return {
                "success": False,
                "message": "blocked_by_cad_ir_gate: legacy CNC template is test-only",
                "error": "legacy_test_entry_blocked",
            }
        skill = self.skills["solidworks_cnc_fillet"]
        if skill.entry_script is None or not skill.entry_script.exists():
            return {"success": False, "message": f"{skill.name} entry script missing"}
        run_dir = self._new_run_dir("cnc_fillet_chamfer")
        process = self._run_process([sys.executable, str(skill.entry_script)], cwd=run_dir, timeout_s=timeout_s)
        return {
            "success": process["returncode"] == 0,
            "message": f"CNC fillet/chamfer template finished with exit code {process['returncode']}: {run_dir}",
            "output": process["output"],
            "path": str(run_dir),
        }

    def _route(self, skill_key: str, actions: list[str], confidence: float, reason: str) -> SkillRoute:
        skill = self.skills[skill_key]
        return SkillRoute(skill.key, skill.name, actions, confidence, reason)

    def _new_run_dir(self, prefix: str) -> Path:
        run_dir = self.output_root / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _run_process(command: list[str], cwd: Path, timeout_s: int) -> dict[str, object]:
        process = subprocess.run(
            command,
            cwd=str(cwd),
            env=UnifiedSkillManager._non_interactive_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
        )
        output = "\n".join(part for part in (process.stdout.strip(), process.stderr.strip()) if part)
        return {"returncode": process.returncode, "output": output.replace("\ufffd", "?")}

    @staticmethod
    def _detect_thread(text: str) -> str | None:
        match = re.search(r"\b(M(?:3|4|5|6|8|10|12))(?:x\d+(?:\.\d+)?)?\b", text, re.IGNORECASE)
        return match.group(0).upper() if match else None

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    @staticmethod
    def _module_status(name: str) -> str:
        return "available" if importlib.util.find_spec(name) else "missing"

    @staticmethod
    def _non_interactive_env() -> dict[str, str]:
        env = os.environ.copy()
        env["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["CAD_AGENT_NO_INTERACTIVE"] = "1"
        return env


SkillManager = UnifiedSkillManager
