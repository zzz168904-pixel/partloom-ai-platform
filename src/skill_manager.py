from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SOLIDWORKS_SKILL_DIR = Path.home() / ".codex" / "skills" / "solidworks-automation"
AUTOCAD_SKILL_DIR = SOLIDWORKS_SKILL_DIR / "subskills" / "autocad-automation"
VIBECAD_SKILL_DIR = SOLIDWORKS_SKILL_DIR / "subskills" / "solidworks-vibecad"
THREADED_HOLES_SKILL_DIR = SOLIDWORKS_SKILL_DIR / "subskills" / "solidworks-threaded-holes"
CNC_FILLET_SKILL_DIR = SOLIDWORKS_SKILL_DIR / "subskills" / "solidworks-fillet-chamfer-cnc"


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


class SkillManager:
    def __init__(self, output_root: Path | None = None) -> None:
        self.output_root = output_root or (Path.cwd() / "logs" / "skill_outputs")
        self.skills = {
            "solidworks": SkillDefinition(
                key="solidworks",
                name="SolidWorks 自动化",
                description="零件建模、装配、工程图、导出、审查",
                root=SOLIDWORKS_SKILL_DIR,
                preflight_script=SOLIDWORKS_SKILL_DIR / "scripts" / "sw_preflight.py",
                keywords=(
                    "solidworks",
                    "sw",
                    "零件",
                    "装配",
                    "建模",
                    "三视图",
                    "工程图",
                    "step",
                    "stl",
                    "sldprt",
                    "sldasm",
                ),
            ),
            "solidworks_vibecad": SkillDefinition(
                key="solidworks_vibecad",
                name="SolidWorks VibeCAD",
                description="Natural language to parametric design plan",
                root=VIBECAD_SKILL_DIR,
                preflight_script=SOLIDWORKS_SKILL_DIR / "scripts" / "sw_preflight.py",
                entry_script=VIBECAD_SKILL_DIR / "scripts" / "plan_from_brief.py",
                keywords=(
                    "vibecad",
                    "text-to-cad",
                    "参数化",
                    "设计计划",
                    "建模计划",
                    "自然语言建模",
                    "规划",
                    "支架",
                    "安装座",
                    "连接块",
                ),
            ),
            "solidworks_threaded_holes": SkillDefinition(
                key="solidworks_threaded_holes",
                name="SolidWorks Threaded Holes",
                description="Tapped holes, pilot holes, chamfers, thread metadata",
                root=THREADED_HOLES_SKILL_DIR,
                preflight_script=SOLIDWORKS_SKILL_DIR / "scripts" / "sw_preflight.py",
                entry_script=THREADED_HOLES_SKILL_DIR / "scripts" / "create_threaded_hole_template.py",
                keywords=(
                    "threaded",
                    "thread",
                    "螺纹",
                    "螺丝孔",
                    "攻丝",
                    "攻牙",
                    "底孔",
                    "m3",
                    "m4",
                    "m5",
                    "m6",
                    "m8",
                    "m10",
                    "m12",
                ),
            ),
            "solidworks_cnc_fillet": SkillDefinition(
                key="solidworks_cnc_fillet",
                name="SolidWorks CNC Fillet/Chamfer",
                description="CNC blocks, mounts, fillets, chamfers, pockets",
                root=CNC_FILLET_SKILL_DIR,
                preflight_script=SOLIDWORKS_SKILL_DIR / "scripts" / "sw_preflight.py",
                entry_script=CNC_FILLET_SKILL_DIR / "scripts" / "create_cnc_mount_template.py",
                keywords=(
                    "cnc",
                    "fillet",
                    "chamfer",
                    "圆角",
                    "倒角",
                    "机加工",
                    "减重口袋",
                    "安装座",
                    "连接块",
                    "支架",
                ),
            ),
            "autocad": SkillDefinition(
                key="autocad",
                name="AutoCAD / DWG 自动化",
                description="DWG/DXF 导入解析、二维绘图、批量改图、CAD 预览",
                root=AUTOCAD_SKILL_DIR,
                preflight_script=AUTOCAD_SKILL_DIR / "scripts" / "acad_preflight.py",
                keywords=(
                    "autocad",
                    "cad",
                    "dwg",
                    "dxf",
                    "二维",
                    "图层",
                    "线稿",
                    "导入",
                    "解析",
                ),
            ),
        }

    def route(self, command: str, actions: list[str]) -> SkillRoute | None:
        text = command.lower()
        if self._contains_any(text, ("环境检测", "自检", "preflight", "检查 skill", "检查skill")):
            if self._contains_any(text, self.skills["autocad"].keywords):
                return self._route("autocad", ["skill_preflight"], 0.95, "命令要求 AutoCAD skill 自检")
            return self._route("solidworks", ["skill_preflight"], 0.9, "命令要求 SolidWorks skill 自检")

        if actions:
            return self._route("solidworks", actions, 0.98, "命中现有 SolidWorks 工程图流程")

        if self._contains_any(text, self.skills["autocad"].keywords):
            return self._route("autocad", ["skill_status"], 0.72, "命中 DWG/DXF/CAD 相关关键词")

        if self._contains_any(text, self.skills["solidworks"].keywords):
            return self._route("solidworks", ["skill_status"], 0.68, "命中 SolidWorks/建模相关关键词")

        if self._contains_any(text, ("图片", "图像", "识别", "pdf")):
            return self._route("solidworks", ["skill_status"], 0.55, "识别/建模类任务默认进入 SolidWorks skill 队列")

        return None

    def status_lines(self) -> list[str]:
        lines: list[str] = []
        for skill in self.skills.values():
            installed = "已安装" if skill.root.exists() else "未找到"
            preflight = "自检脚本可用" if skill.preflight_script.exists() else "缺少自检脚本"
            lines.append(f"{skill.name}: {installed}, {preflight}")
        lines.append(f"Python COM: {self._module_status('pythoncom')}, win32com: {self._module_status('win32com')}")
        return lines

    def run_preflight(self, skill_key: str, timeout_s: int = 20) -> dict[str, object]:
        skill = self.skills.get(skill_key)
        if skill is None:
            return {"success": False, "message": f"未知 Skill: {skill_key}"}
        if not skill.preflight_script.exists():
            return {"success": False, "message": f"{skill.name} 缺少自检脚本: {skill.preflight_script}"}

        command = [sys.executable, str(skill.preflight_script)]
        if skill.preflight_script.name == "sw_preflight.py":
            command.append("--no-install")
        process = subprocess.run(
            command,
            cwd=str(skill.root),
            env=self._non_interactive_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
        )
        output = "\n".join(part for part in (process.stdout.strip(), process.stderr.strip()) if part)
        output = output.replace("\ufffd", "?")
        return {
            "success": process.returncode == 0,
            "message": f"{skill.name} 自检完成，退出码 {process.returncode}",
            "output": output,
        }

    def _route(self, skill_key: str, actions: list[str], confidence: float, reason: str) -> SkillRoute:
        skill = self.skills[skill_key]
        return SkillRoute(skill.key, skill.name, actions, confidence, reason)

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    @staticmethod
    def _module_status(name: str) -> str:
        return "可用" if importlib.util.find_spec(name) else "缺失"

    @staticmethod
    def _non_interactive_env() -> dict[str, str]:
        env = os.environ.copy()
        env["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["CAD_AGENT_NO_INTERACTIVE"] = "1"
        return env
