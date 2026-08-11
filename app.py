from __future__ import annotations

import contextlib
import gc
import io
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QComboBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from sw_connector import SWConnector
from unified_skill_manager import SkillManager
from cad_agent import AgentsOrchestratorConfig, AgentsOrchestratorRunner, CADAgentSkillManager
from cad_agent.direct_cad_ir import DIRECT_CAD_IR_PROVIDER, DirectCADIRService
from cad_agent.provider_registry import ProviderRegistry
from cad_agent.planner_validator import PlannerValidator
from cad_agent.runtime_config import application_data_dir, output_root, redact_secrets
from cad_agent.stage_planner import stage_summary
from cad_agent.system_resources import cad_resource_preflight
from cad_agent.vibecad_skill import VibeCADSkill

LOG_DIR = application_data_dir() / "logs"
SINGLE_INSTANCE_NAME = "PartLoomAI.Gui.v1"


def setup_file_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"gui_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
    )
    return log_file


def notify_existing_instance(timeout_ms: int = 250) -> bool:
    """Ask an existing GUI process to restore its window."""
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_NAME)
    if not socket.waitForConnected(timeout_ms):
        socket.abort()
        return False
    socket.write(b"activate")
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


def create_single_instance_server(parent: QObject) -> QLocalServer | None:
    """Create the local activation endpoint after removing a stale endpoint."""
    QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
    server = QLocalServer(parent)
    if server.listen(SINGLE_INSTANCE_NAME):
        return server
    if notify_existing_instance():
        return None
    logging.warning("Could not create the GUI single-instance endpoint: %s", server.errorString())
    return server


DRAWING_COMMANDS = (
    "生成三视图",
    "创建工程图",
    "帮我给当前零件生成三视图工程图",
    "生成工程图并自动布局",
)
PDF_COMMANDS = ("导出PDF", "导出 PDF", "生成工程图并导出PDF", "生成工程图并导出 PDF")
DWG_COMMANDS = ("导出DWG", "导出 DWG")
LAYOUT_COMMANDS = ("自动布局",)
BATCH_COMMANDS = ("批量生成三视图", "批量画三视图", "批量生成工程图")
ANNOTATION_COMMANDS = ("自动标注", "自动尺寸标注", "添加尺寸", "标注尺寸")


class AnimatedBackground(QWidget):
    """Quiet CAD workspace background retained under the legacy class name."""

    def __init__(self) -> None:
        super().__init__()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101416"))
        painter.setPen(QPen(QColor(255, 255, 255, 9), 1))
        grid = 40
        for x in range(0, self.width(), grid):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), grid):
            painter.drawLine(0, y, self.width(), y)


class PlannerPreviewDialog(QDialog):
    """Read-only deterministic plan review shown before any Pipeline call."""

    def __init__(self, summary: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = dict(summary)
        self.setObjectName("plannerPreviewDialog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("CAD-IR 执行计划")
        self.setModal(True)
        self.setMinimumSize(760, 620)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet(
            """
            QDialog#plannerPreviewDialog {
                background-color: #151a1d;
                color: #e8eeeb;
            }
            QLabel#plannerPreviewTitle {
                color: #f4f7f5;
                font-size: 18px;
                font-weight: 900;
            }
            QLabel#plannerPreviewOverview {
                color: #cbd6d2;
                background-color: #1d2427;
                border: 1px solid #334044;
                border-radius: 6px;
                padding: 9px 11px;
            }
            QTextEdit#plannerPreviewDetails {
                color: #dbe7e3;
                background-color: #0d1113;
                border: 1px solid #354449;
                border-radius: 6px;
                padding: 12px;
                selection-color: #ffffff;
                selection-background-color: #287d6b;
                font-family: Consolas, "Microsoft YaHei UI", monospace;
                font-size: 12px;
            }
            QTextEdit#plannerPreviewDetails QScrollBar:vertical {
                width: 10px;
                margin: 1px;
                background: #111719;
                border: 0;
            }
            QTextEdit#plannerPreviewDetails QScrollBar::handle:vertical {
                min-height: 44px;
                border-radius: 4px;
                background: #46555a;
            }
            QTextEdit#plannerPreviewDetails QScrollBar::handle:vertical:hover {
                background: #5b6d72;
            }
            QTextEdit#plannerPreviewDetails QScrollBar::add-line:vertical,
            QTextEdit#plannerPreviewDetails QScrollBar::sub-line:vertical {
                height: 0;
            }
            QPushButton#plannerEditButton,
            QPushButton#plannerConfirmButton {
                min-height: 38px;
                padding: 0 16px;
                border-radius: 6px;
                font-weight: 750;
            }
            QPushButton#plannerEditButton {
                color: #d9e2df;
                background-color: #20282b;
                border: 1px solid #455257;
            }
            QPushButton#plannerEditButton:hover {
                color: #ffffff;
                background-color: #2a3438;
                border-color: #607178;
            }
            QPushButton#plannerConfirmButton {
                color: #081310;
                background-color: #52d1b5;
                border: 1px solid #52d1b5;
            }
            QPushButton#plannerConfirmButton:hover {
                background-color: #69ddc4;
                border-color: #69ddc4;
            }
            QPushButton#plannerConfirmButton:disabled {
                color: #7f8b88;
                background-color: #242b2e;
                border-color: #394246;
            }
            QToolTip {
                color: #edf3f1;
                background-color: #20282b;
                border: 1px solid #526167;
                padding: 5px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("执行前计划校验")
        title.setObjectName("plannerPreviewTitle")
        layout.addWidget(title)

        validation = self.summary.get("planning_validation") or {}
        status = str(validation.get("status") or "legacy_validated")
        confidence = validation.get("model_confidence")
        confidence_text = "N/A" if confidence is None else f"{float(confidence):.2f}"
        overview = QLabel(
            f"零件类型：{self.summary.get('part_type') or '未识别'}    "
            f"校验状态：{status}    模型置信度：{confidence_text}"
        )
        overview.setObjectName("plannerPreviewOverview")
        overview.setWordWrap(True)
        layout.addWidget(overview)

        self.details = QTextEdit()
        self.details.setObjectName("plannerPreviewDetails")
        self.details.setReadOnly(True)
        self.details.setPlainText(self.render_text(self.summary))
        layout.addWidget(self.details, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.edit_button = QPushButton("返回修改指令")
        self.confirm_button = QPushButton("确认并开始建模")
        self.edit_button.setObjectName("plannerEditButton")
        self.confirm_button.setObjectName("plannerConfirmButton")
        confirmation_allowed = bool(self.summary.get("confirmation_allowed", True))
        self.confirm_button.setEnabled(confirmation_allowed)
        if not confirmation_allowed:
            self.confirm_button.setToolTip("存在缺失尺寸、目标歧义或不支持特征，必须先修改指令。")
        self.edit_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

    @staticmethod
    def render_text(summary: dict) -> str:
        preview = summary.get("planning_preview") or {}
        validation = summary.get("planning_validation") or {}
        features = list(preview.get("features") or [])
        lines = [
            f"任务类型：{summary.get('task_type')}",
            f"执行阶段：{summary.get('requested_stages')}",
            f"停止阶段：{summary.get('stop_after')}",
            f"确定性校验：{validation.get('status', 'legacy_validated')}",
            f"允许执行：{'是' if summary.get('allow_execution') else '否'}",
            "",
            "Feature 执行顺序：",
        ]
        if features:
            for index, feature in enumerate(features, start=1):
                lines.extend([
                    f"{index}. {feature.get('id')} / {feature.get('operation')}",
                    f"   尺寸与参数：{json.dumps(feature.get('parameters') or {}, ensure_ascii=False)}",
                    f"   依赖：{feature.get('depends_on') or []}",
                    f"   目标实体：{feature.get('target_body') or '<缺失>'}",
                    f"   目标引用：{json.dumps(feature.get('target_reference'), ensure_ascii=False)}",
                    f"   证据：{feature.get('evidence') or []}",
                    f"   假设：{feature.get('assumptions') or []}",
                    f"   未解决：{feature.get('unresolved') or []}",
                ])
        else:
            lines.extend(f"- {item}" for item in summary.get("plan_lines", []))
        lines.extend([
            "",
            f"系统假设：{validation.get('assumptions') or []}",
            f"未解决信息：{validation.get('unresolved') or []}",
            f"不支持的 Feature：{validation.get('unsupported_operations') or []}",
            "校验错误：",
        ])
        errors = list(validation.get("errors") or [])
        lines.extend(
            f"- [{item.get('code')}] {item.get('message')}"
            for item in errors
        )
        if not errors:
            lines.append("- 无")
        warnings = list(validation.get("warnings") or [])
        lines.append("警告：")
        lines.extend(f"- [{item.get('code')}] {item.get('message')}" for item in warnings)
        if not warnings:
            lines.append("- 无")
        return "\n".join(lines)


class SignalWriter(io.TextIOBase):
    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit

    def write(self, text: str) -> int:
        if text.strip():
            self.emit(text.rstrip())
        return len(text)

    def flush(self) -> None:
        return None


class SolidWorksStatusBridge(QObject):
    ready = Signal(dict)


def probe_solidworks_status() -> dict:
    """Read SolidWorks status on a worker thread so COM cannot delay window paint."""
    started = time.perf_counter()
    connector = SWConnector()
    try:
        connected = connector.connect(log_failure=False) is not None
        info = connector.get_document_info() if connected else None
        return {
            "connected": connected,
            "info": info or {},
            "elapsed_s": time.perf_counter() - started,
        }
    except Exception:
        logging.exception("SolidWorks background status probe failed.")
        return {
            "connected": False,
            "info": {},
            "elapsed_s": time.perf_counter() - started,
        }
    finally:
        connector.disconnect()


def release_worker_resources(*owners: object) -> None:
    """Drop worker-owned COM references before its thread exits."""
    for owner in owners:
        disconnect = getattr(owner, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
                continue
            except Exception:
                logging.exception("Failed to disconnect a worker COM owner.")
        if hasattr(owner, "app"):
            try:
                owner.app = None
            except Exception:
                pass
    gc.collect()
    try:
        import pythoncom

        free_unused = getattr(pythoncom, "CoFreeUnusedLibraries", None)
        if callable(free_unused):
            free_unused()
    except Exception:
        pass


class SolidWorksWorker(QObject):
    log = Signal(str)
    status = Signal(str)
    skill = Signal(str)
    model = Signal(str)
    task = Signal(str)
    progress = Signal(int)
    result = Signal(bool, str)
    finished = Signal()

    def __init__(self, command: str, batch_paths: list[str] | None = None) -> None:
        super().__init__()
        self.command = command.strip()
        self.batch_paths = batch_paths or []
        self.connector = SWConnector()
        self.skill_manager = SkillManager(LOG_DIR / "skill_outputs")
        self.provider_registry = ProviderRegistry(VibeCADSkill(LOG_DIR / "provider_probe"))
        self.provider_statuses = self.provider_registry.statuses()

    def run(self) -> None:
        try:
            writer = SignalWriter(self.log.emit)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                self._run_command()
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.result.emit(False, f"执行失败：{exc}")
        finally:
            release_worker_resources(self.connector)
            self.finished.emit()

    def _run_command(self) -> None:
        actions = self._parse_actions(self.command)
        route = self.skill_manager.route(self.command, actions)
        if route is None:
            self.result.emit(False, "暂不支持该指令。")
            return

        self.task.emit(self.command)
        self.progress.emit(5)
        self.skill.emit(route.skill_name)
        self.log.emit(
            f"Skill 路由: {route.skill_name} "
            f"(confidence={route.confidence:.2f}, reason={route.reason})"
        )
        actions = route.actions

        if actions == ["skill_status"]:
            for line in self.skill_manager.status_lines():
                self.log.emit(line)
            self.progress.emit(100)
            self.result.emit(
                True,
                f"{route.skill_name} 已识别。当前版本已完成 Skill 注册、路由和环境检测；"
                "具体建模/识图执行器将继续接入。",
            )
            return

        if actions == ["skill_preflight"]:
            self.task.emit(f"{route.skill_name} 环境自检")
            self.progress.emit(20)
            result = self.skill_manager.run_preflight(route.skill_key)
            output = str(result.get("output", "")).strip()
            if output:
                self.log.emit(output)
            self.progress.emit(100 if result.get("success") else 0)
            self.result.emit(bool(result.get("success")), str(result.get("message", "自检完成")))
            return

        if actions == ["vibecad_plan"]:
            self.task.emit("VibeCAD 参数化设计规划")
            self.progress.emit(20)
            result = self.skill_manager.run_vibecad_plan(self.command)
            output = str(result.get("output", "")).strip()
            if output:
                self.log.emit(output)
            if result.get("path"):
                self.log.emit(f"设计计划: {result['path']}")
            if result.get("outline_path"):
                self.log.emit(f"执行摘要: {result['outline_path']}")
            self.progress.emit(100 if result.get("success") else 0)
            self.result.emit(bool(result.get("success")), str(result.get("message", "VibeCAD 规划完成")))
            return

        resource_status = cad_resource_preflight()
        snapshot = resource_status.get("snapshot") or {}
        self.log.emit(
            "CAD resource preflight: "
            f"ok={resource_status.get('ok')}, "
            f"commit={snapshot.get('commit_used_gb', 'N/A')}/"
            f"{snapshot.get('commit_limit_gb', 'N/A')} GB, "
            f"physical_free={snapshot.get('physical_available_gb', 'N/A')} GB"
        )
        if not resource_status.get("ok"):
            self.result.emit(False, str(resource_status.get("message") or "系统内存不足，CAD 任务已阻断。"))
            return

        if actions == ["threaded_hole_template"]:
            self.task.emit("螺纹孔模板建模")
            self.progress.emit(10)
            preflight = self.skill_manager.run_preflight(route.skill_key)
            self.log.emit(str(preflight.get("message", "")))
            if preflight.get("output"):
                self.log.emit(str(preflight["output"]))
            if not preflight.get("success"):
                self.progress.emit(0)
                self.result.emit(False, str(preflight.get("message", "SolidWorks 自检失败")))
                return
            self.progress.emit(30)
            result = self.skill_manager.run_threaded_hole_template(self.command)
            if result.get("output"):
                self.log.emit(str(result["output"]))
            if result.get("path"):
                self.log.emit(f"输出目录: {result['path']}")
            self.progress.emit(100 if result.get("success") else 0)
            self.result.emit(bool(result.get("success")), str(result.get("message", "螺纹孔模板完成")))
            return

        if actions == ["cnc_mount_template"]:
            self.task.emit("CNC 圆角倒角模板建模")
            self.progress.emit(10)
            preflight = self.skill_manager.run_preflight(route.skill_key)
            self.log.emit(str(preflight.get("message", "")))
            if preflight.get("output"):
                self.log.emit(str(preflight["output"]))
            if not preflight.get("success"):
                self.progress.emit(0)
                self.result.emit(False, str(preflight.get("message", "SolidWorks 自检失败")))
                return
            self.progress.emit(30)
            result = self.skill_manager.run_cnc_mount_template()
            if result.get("output"):
                self.log.emit(str(result["output"]))
            if result.get("path"):
                self.log.emit(f"输出目录: {result['path']}")
            self.progress.emit(100 if result.get("success") else 0)
            self.result.emit(bool(result.get("success")), str(result.get("message", "CNC 模板完成")))
            return

        self.log.emit("正在连接 SolidWorks")
        if self.connector.connect() is None:
            self.status.emit("未连接")
            self.result.emit(False, "请先打开 SolidWorks。")
            return

        self.status.emit("已连接")
        self.progress.emit(20)
        self.log.emit("已连接 SolidWorks")

        doc_info = self.connector.get_document_info()
        if doc_info:
            model_name = doc_info.get("file_name", "N/A")
            self.model.emit(model_name)
            self.log.emit(f"当前模型：{model_name}")

        ok = True
        message = "执行完成"
        for action in actions:
            result = self._run_action(action)
            ok = bool(result.get("success"))
            message = str(result.get("message", "未知结果"))
            self.log.emit(message)
            if not ok:
                self.progress.emit(0)
                self.result.emit(False, message)
                return
        self.result.emit(ok, message)

    def _run_action(self, action: str) -> dict:
        if action == "batch":
            return self._run_batch()
        if action == "drawing":
            self.task.emit("生成三视图工程图")
            self.progress.emit(30)
            self.log.emit("正在创建工程图")
            self.log.emit("正在生成三视图")
            result = self.connector.create_drawing_with_standard_views()
            if result.get("success"):
                self.progress.emit(60)
                self.log.emit("工程图创建完成")
            return result
        if action == "layout":
            self.task.emit("自动布局")
            self.progress.emit(80)
            self.log.emit("正在自动布局")
            return self.connector.auto_layout_current_drawing()
        if action == "annotate":
            self.task.emit("自动尺寸标注")
            self.progress.emit(85)
            self.log.emit("正在自动添加工程图尺寸标注")
            result = self.connector.annotate_current_drawing()
            if result.get("success"):
                self.progress.emit(100)
            return result
        if action == "pdf":
            self.task.emit("导出 PDF")
            self.progress.emit(95)
            self.log.emit("正在导出 PDF")
            result = self.connector.export_current_pdf()
            if result.get("success"):
                self.progress.emit(100)
            return result
        if action == "dwg":
            self.task.emit("导出 DWG")
            self.progress.emit(95)
            self.log.emit("正在导出 DWG")
            result = self.connector.export_current_dwg()
            if result.get("success"):
                self.progress.emit(100)
            return result
        return {"success": False, "message": "暂不支持该指令。"}

    def _run_batch(self) -> dict:
        if not self.batch_paths:
            return {"success": False, "message": "请先选择要批量处理的模型文件。"}
        total = len(self.batch_paths)
        ok_count = 0
        for index, path in enumerate(self.batch_paths, 1):
            self.task.emit(f"批量生成三视图 {index}/{total}")
            self.progress.emit(int((index - 1) / total * 100))
            self.log.emit(f"正在打开模型：{path}")
            opened = self.connector.open_model(path)
            if not opened.get("success"):
                self.log.emit(f"失败：{opened.get('message', '模型打开失败')}")
                continue
            self.model.emit(Path(path).name)
            self.log.emit("正在生成三视图")
            drawing = self.connector.create_drawing_with_standard_views()
            if not drawing.get("success"):
                self.log.emit(f"失败：{drawing.get('message', '工程图创建失败')}")
                continue
            self.log.emit("正在导出 PDF")
            pdf = self.connector.export_current_pdf()
            self.log.emit(pdf.get("message", "PDF 导出完成"))
            self.log.emit("正在导出 DWG")
            dwg = self.connector.export_current_dwg()
            self.log.emit(dwg.get("message", "DWG 导出完成"))
            if pdf.get("success") and dwg.get("success"):
                ok_count += 1
            self.progress.emit(int(index / total * 100))
        return {
            "success": ok_count > 0,
            "message": f"批量完成：PDF/DWG 成功 {ok_count}/{total}",
        }

    @staticmethod
    def _is_drawing_command(command: str) -> bool:
        return any(key in command for key in DRAWING_COMMANDS)

    @staticmethod
    def _parse_actions(command: str) -> list[str]:
        actions: list[str] = []
        if any(key in command for key in DRAWING_COMMANDS):
            actions.append("drawing")
        if any(key in command for key in LAYOUT_COMMANDS) and "layout" not in actions:
            actions.append("layout")
        if any(key in command for key in PDF_COMMANDS):
            if "drawing" not in actions and "生成" in command:
                actions.append("drawing")
            actions.append("pdf")
        if any(key in command for key in DWG_COMMANDS):
            actions.append("dwg")
        if any(key in command for key in ANNOTATION_COMMANDS):
            actions.append("annotate")
        if any(key in command for key in BATCH_COMMANDS):
            actions = ["batch"]
        return actions


class PipelineWorker(QObject):
    log = Signal(str)
    task = Signal(str)
    skill = Signal(str)
    progress = Signal(int)
    pipeline_event = Signal(dict)
    result = Signal(bool, str, dict)
    finished = Signal()

    PROGRESS_BY_EVENT = {
        "pipeline_started": 3,
        "brain_completed": 18,
        "step_started": 35,
        "lifecycle_stage": 50,
        "artifact_published": 88,
        "pipeline_finished": 100,
    }

    STEP_LABELS = {
        "base_plate": "SolidWorks base plate",
        "fillet": "SolidWorks fillet",
        "boss": "SolidWorks boss",
        "chamfer": "SolidWorks chamfer",
        "through_hole": "SolidWorks through hole",
        "pocket": "SolidWorks pocket",
        "slot": "SolidWorks slot",
        "linear_pattern": "SolidWorks linear pattern",
        "circular_pattern": "SolidWorks circular pattern",
        "mirror": "SolidWorks mirror",
        "save_sldprt": "Save SLDPRT",
        "solidworks_vibecad": "AI Brain / Design Planner",
        "solidworks_automation": "SolidWorks modeling",
        "solidworks_threaded_holes": "Thread Skill",
        "solidworks_cnc_fillet": "Fillet / Chamfer",
        "solidworks_drawing": "SolidWorks Drawing",
        "autocad_annotation": "AutoCAD Annotation",
        "step_export": "STEP Export",
        "pdf_export": "PDF Export",
    }

    def __init__(
        self,
        prompt: str,
        execute_real_skills: bool = True,
        stage_mode: str = "auto",
        provider_id: str = "auto",
        design_json: dict | None = None,
    ) -> None:
        super().__init__()
        self.prompt = prompt.strip()
        self.execute_real_skills = execute_real_skills
        self.stage_mode = stage_mode
        self.provider_id = provider_id
        self.design_json = dict(design_json) if design_json is not None else None
        self.direct_cad_ir = bool(
            self.design_json
            and self.design_json.get("planning_mode") == DIRECT_CAD_IR_PROVIDER
        )

    def run(self) -> None:
        try:
            output_root = LOG_DIR / "skill_outputs"
            manager = CADAgentSkillManager(output_root)
            direct_cad_ir = self.direct_cad_ir
            self.task.emit("CAD-IR Pipeline" if direct_cad_ir else "PartLoom AI Pipeline")
            self.skill.emit("CAD-IR Validator" if direct_cad_ir else "AI Brain")
            self.progress.emit(1)
            self.log.emit("Direct CAD-IR received; LLM planning is disabled." if direct_cad_ir else "AI Agent prompt received.")
            config = AgentsOrchestratorConfig.detect()
            provider_registry = ProviderRegistry(manager.vibecad)
            execution_provider_id = "local_fallback" if direct_cad_ir else self.provider_id
            _provider, provider_status = provider_registry.resolve(execution_provider_id)
            if direct_cad_ir:
                self.log.emit("Agents Orchestrator: ready, provider=direct_cad_ir, model=deterministic, llm=disabled")
                self.log.emit("CAD-IR Validation Started")
                self.log.emit("provider=direct_cad_ir")
            else:
                self.log.emit(
                    f"Agents Orchestrator: ready, provider={provider_status.id}, "
                    f"model={provider_status.model}, configured={str(provider_status.configured).lower()}"
                )
                self.log.emit("AI Brain Started")
                self.log.emit(f"provider={provider_status.id}")
                self.log.emit("Planner Started")
            agents_report = AgentsOrchestratorRunner(
                output_root,
                config=config,
                event_callback=self._on_pipeline_event,
                provider_id=execution_provider_id,
            ).run(
                self.prompt,
                execute_real_skills=self.execute_real_skills,
                run_pipeline=True,
                stage_mode=self.stage_mode,
                design_json=self.design_json,
            )
            planning_provider = agents_report.get("orchestrator", {}).get("planning_provider")
            if planning_provider:
                self.log.emit(f"planning_provider={planning_provider}")
            statuses = agents_report.get("agent_statuses", {})
            self.log.emit(f"Planner Finished: {statuses.get('planner', 'unknown')}")
            self.log.emit(f"Router Finished: {statuses.get('router', 'unknown')}")
            self.log.emit(f"Executor Finished: {statuses.get('executor', 'unknown')}")
            self.log.emit(f"Validator Finished: {statuses.get('validator', 'unknown')}")
            self.log.emit(f"Recovery Finished: {statuses.get('recovery', 'unknown')}")
            pipeline_data = agents_report.get("pipeline_result", {})
            artifacts = agents_report.get("artifacts", {})
            data = {
                "status": pipeline_data.get("status", "failed"),
                "report_path": pipeline_data.get("report_path") or artifacts.get("pipeline_report"),
                "run_dir": pipeline_data.get("run_dir"),
                "artifacts": pipeline_data.get("artifacts", {}),
                "task_type": agents_report.get("task_type"),
                "requested_stages": agents_report.get("requested_stages", []),
                "forbidden_stages": agents_report.get("forbidden_stages", []),
                "stop_after": agents_report.get("stop_after"),
                "agents_orchestrator": {
                    "provider": agents_report.get("orchestrator", {}).get("provider"),
                    "planning_provider": agents_report.get("orchestrator", {}).get("planning_provider"),
                    "model": agents_report.get("orchestrator", {}).get("model"),
                    "sdk_available": agents_report.get("orchestrator", {}).get("sdk_available"),
                    "agent_statuses": statuses,
                    "unsupported_features": agents_report.get("unsupported_features", []),
                    "validation": agents_report.get("validation", {}),
                    "recovery": agents_report.get("recovery", {}),
                    "report_path": artifacts.get("agents_orchestrator_report"),
                },
            }
            report = str(data.get("report_path") or "")
            status = str(data.get("status") or "failed")
            report_exists = bool(report and Path(report).exists())
            ok = status == "success" and report_exists
            if status == "success" and not report_exists:
                message = f"Pipeline failed: pipeline_report.json was not generated: {report}"
            else:
                message = f"Pipeline {status}. Report: {report}"
            self.progress.emit(100 if ok else 0)
            self.result.emit(ok, message, data)
        except Exception as exc:
            trace = redact_secrets(traceback.format_exc())
            self.log.emit(trace)
            self.result.emit(False, f"Pipeline failed: {redact_secrets(exc)}", {"error": redact_secrets(repr(exc))})
        finally:
            release_worker_resources()
            self.finished.emit()

    def _on_pipeline_event(self, event: dict) -> None:
        event_type = str(event.get("event", ""))
        step_key = str(event.get("skill_key", ""))
        label = self.STEP_LABELS.get(step_key, step_key or event_type)
        if event_type == "pipeline_started":
            self.log.emit("Pipeline Started")
            self.task.emit("CAD-IR Validation" if self.direct_cad_ir else "AI Brain")
            self.skill.emit("CAD-IR Validator" if self.direct_cad_ir else "AI Brain")
        elif event_type == "step_started":
            lifecycle_line = {
                "base_plate": "SolidWorks Started",
                "solidworks_automation": "SolidWorks Started",
                "solidworks_drawing": "Drawing Started",
                "autocad_annotation": "AutoCAD Started",
                "step_export": "STEP Started",
                "pdf_export": "PDF Started",
            }.get(step_key)
            if lifecycle_line:
                self.log.emit(lifecycle_line)
            if step_key == "solidworks_drawing":
                self.log.emit("DWG Started")
            self.task.emit(label)
            self.skill.emit(label)
            self.log.emit(f"Started: {label}")
        elif event_type == "lifecycle_stage":
            stage = event.get("stage", "")
            success = event.get("success", "")
            self.task.emit(f"{label}: {stage}")
            self.log.emit(f"{label} / {stage}: {success} - {event.get('message', '')}")
        elif event_type == "brain_completed":
            self.task.emit("Design JSON and Skill Pipeline ready")
            self.skill.emit("CAD-IR Validator" if self.direct_cad_ir else "AI Brain")
            if self.direct_cad_ir:
                self.log.emit("CAD-IR Validation Finished")
                self.log.emit(f"CAD-IR plan ready, steps={event.get('steps')}")
            else:
                self.log.emit("AI Brain Finished")
                self.log.emit(f"AI Brain completed, steps={event.get('steps')}")
        elif event_type == "artifact_published":
            self.log.emit(f"Published {event.get('artifact_key')}: {event.get('target')}")
        elif event_type in {"pipeline_exception", "step_exception"}:
            self.log.emit(str(event.get("traceback") or event.get("error") or "Pipeline exception"))
        elif event_type == "blocked_by_allowed_skills_guard":
            self.log.emit(f"blocked_by_allowed_skills_guard: {event.get('current_skill')}")
        elif event_type == "unexpected_output_generated":
            self.log.emit(f"unexpected_output_generated: {event.get('paths')}")
        elif event_type == "stop_after_reached":
            self.log.emit(f"stop_after={event.get('stop_after')} reached")
            self.log.emit("no further skills scheduled")
            self.log.emit("waiting_for_user_continue=true")
        elif event_type == "pipeline_finished":
            if event.get("status") == "success" and event.get("report_path"):
                self.log.emit("Pipeline Finished")
            self.log.emit(f"Pipeline finished: {event.get('status')}")
        progress = self.PROGRESS_BY_EVENT.get(event_type)
        if progress is not None:
            self.progress.emit(progress)
        self.pipeline_event.emit(event)


class PDF2CADWorker(QObject):
    log = Signal(str)
    task = Signal(str)
    skill = Signal(str)
    progress = Signal(int)
    result = Signal(bool, str, dict)
    finished = Signal()

    def __init__(
        self,
        pdf_path: str,
        mode: str = "2d",
        *,
        plan_only: bool = False,
        design_json: dict | None = None,
    ) -> None:
        super().__init__()
        self.pdf_path = pdf_path
        self.mode = mode
        self.plan_only = plan_only
        self.design_json = design_json

    def run(self) -> None:
        try:
            self.task.emit("PDF2CAD Pipeline")
            self.skill.emit("PDF Parse Skill")
            self.progress.emit(5)
            self.log.emit(f"PDF2CAD Started: {self.pdf_path}")
            output_root = LOG_DIR / "skill_outputs"
            manager = CADAgentSkillManager(output_root)
            self.progress.emit(15)
            if self.plan_only:
                data = manager.pdf2cad_runner.plan(self.pdf_path, mode=self.mode)
                data = {"status": "awaiting_confirmation", "success": True, **data}
            else:
                data = manager.run_pdf2cad_pipeline(self.pdf_path, mode=self.mode, design_json=self.design_json)
            self.progress.emit(100 if data.get("success") else 0)
            artifacts = data.get("artifacts", {}) if isinstance(data, dict) else {}
            for key in ("drawing_ir", "design_json", "dwg", "dxf", "pdf", "pdf2cad_report", "pipeline_report", "delivery_dir"):
                value = artifacts.get(key)
                if value:
                    self.log.emit(f"{key}: {value}")
            message = "PDF2CAD plan ready." if self.plan_only else str(data.get("message", "PDF2CAD finished"))
            self.result.emit(bool(data.get("success")), message, data)
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.result.emit(False, f"PDF2CAD failed: {exc}", {"error": repr(exc)})
        finally:
            release_worker_resources()
            self.finished.emit()


class CADFileWorker(QObject):
    log = Signal(str)
    task = Signal(str)
    skill = Signal(str)
    progress = Signal(int)
    result = Signal(bool, str, dict)
    finished = Signal()

    def __init__(self, source_path: str, requested_outputs: list[str] | None = None, prompt: str = "") -> None:
        super().__init__()
        self.source_path = source_path
        self.requested_outputs = requested_outputs
        self.prompt = prompt

    def run(self) -> None:
        try:
            self.task.emit("CAD File Pipeline")
            self.skill.emit("File2CAD Router")
            self.progress.emit(5)
            self.log.emit(f"File2CAD Started: {self.source_path}")
            manager = CADAgentSkillManager(LOG_DIR / "skill_outputs")
            self.progress.emit(15)
            data = manager.run_file2cad_pipeline(
                self.source_path,
                requested_outputs=self.requested_outputs,
                prompt=self.prompt,
            )
            status = str(data.get("status") or "failed")
            self.progress.emit(100 if status == "success" else 0)
            artifacts = data.get("artifacts", {}) if isinstance(data, dict) else {}
            for key in (
                "source_file",
                "sldprt",
                "sldasm",
                "slddrw",
                "step",
                "stl",
                "iges",
                "dwg",
                "dxf",
                "pdf",
                "annotated_dwg",
                "annotation_report",
                "pipeline_report",
                "delivery_dir",
            ):
                value = artifacts.get(key)
                if value:
                    self.log.emit(f"{key}: {value}")
            message = "File2CAD pipeline completed." if status == "success" else str(data.get("error") or "File2CAD failed.")
            self.result.emit(status == "success", message, data)
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.result.emit(False, f"File2CAD failed: {exc}", {"status": "failed", "error": repr(exc)})
        finally:
            release_worker_resources()
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self, log_file: Path) -> None:
        super().__init__()
        self.log_file = log_file
        self.thread: QThread | None = None
        self.worker: SolidWorksWorker | PipelineWorker | PDF2CADWorker | CADFileWorker | None = None
        self.batch_paths: list[str] = []
        self.last_pipeline_prompt = ""
        self.last_pipeline_kind = "agent"
        self.last_file_request: dict = {}
        self.last_pipeline_result: dict = {}
        self.pending_pdf_plan: dict = {}
        self.skill_manager = SkillManager(LOG_DIR / "skill_outputs")
        self.provider_registry = ProviderRegistry(VibeCADSkill(LOG_DIR / "provider_probe"))
        self.provider_statuses = self.provider_registry.statuses()
        self._status_probe_running = False
        self._status_bridge = SolidWorksStatusBridge(self)
        self._status_bridge.ready.connect(self._apply_solidworks_status)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_status)
        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.update_activity_animation)
        self.activity_tick = 0
        self.is_compact_layout: bool | None = None

        self.setWindowTitle("PartLoom AI Platform")
        self.resize(1440, 900)
        self.setMinimumSize(820, 620)
        self._build_ui()
        self._apply_style()
        self._apply_responsive_layout()
        QTimer.singleShot(250, self.refresh_status)
        self.status_timer.start(20000)
        self.activity_timer.start(450)

    def _build_ui(self) -> None:
        root = AnimatedBackground()
        root.setObjectName("root")
        self.outer = QHBoxLayout(root)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(0)

        self.sidebar = self._build_sidebar()
        self.main_scroll = self._wrap_scroll(self._build_main_area(), "mainScroll")
        self.status_scroll = self._wrap_scroll(self._build_status_panel(), "statusScroll")
        self.status_scroll.setMinimumWidth(260)
        self.status_scroll.setMaximumWidth(360)

        self.outer.addWidget(self.sidebar)
        self.outer.addWidget(self.main_scroll, 1)
        self.outer.addWidget(self.status_scroll)
        self.setCentralWidget(root)

    def _wrap_scroll(self, widget: QWidget, object_name: str) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return scroll

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        if not hasattr(self, "outer"):
            return

        compact = self.width() < 1100
        if compact == self.is_compact_layout:
            return
        self.is_compact_layout = compact

        self._remove_from_layout(self.sidebar)
        self._remove_from_layout(self.main_scroll)
        self._remove_from_layout(self.status_scroll)

        if compact:
            self.outer.setDirection(QBoxLayout.TopToBottom)
            self.sidebar_layout.setDirection(QBoxLayout.LeftToRight)
            self.sidebar_layout.setContentsMargins(18, 8, 18, 8)
            self.sidebar_layout.setSpacing(8)
            self.sidebar.setMaximumWidth(16777215)
            self.sidebar.setMinimumWidth(0)
            self.sidebar.setMinimumHeight(56)
            self.sidebar.setFixedHeight(56)
            self.status_scroll.setMaximumWidth(16777215)
            self.status_scroll.setMinimumWidth(0)
            self.status_scroll.setMinimumHeight(180)
            self.status_scroll.setMaximumHeight(270)
            self.sidebar_tag.setVisible(False)
            self.sidebar_footer.setVisible(False)
            for button in self.nav_buttons:
                button.setVisible(False)
            self.outer.addWidget(self.sidebar)
            self.outer.addWidget(self.main_scroll, 1)
            self.outer.addWidget(self.status_scroll)
        else:
            self.outer.setDirection(QBoxLayout.LeftToRight)
            self.sidebar_layout.setDirection(QBoxLayout.TopToBottom)
            self.sidebar_layout.setContentsMargins(18, 22, 18, 18)
            self.sidebar_layout.setSpacing(8)
            self.sidebar.setMinimumWidth(176)
            self.sidebar.setMaximumWidth(196)
            self.sidebar.setMinimumHeight(0)
            self.sidebar.setMaximumHeight(16777215)
            self.status_scroll.setMinimumWidth(292)
            self.status_scroll.setMaximumWidth(340)
            self.status_scroll.setMinimumHeight(0)
            self.status_scroll.setMaximumHeight(16777215)
            self.sidebar_tag.setVisible(True)
            self.sidebar_footer.setVisible(True)
            for button in self.nav_buttons:
                button.setVisible(True)
            self.outer.addWidget(self.sidebar)
            self.outer.addWidget(self.main_scroll, 1)
            self.outer.addWidget(self.status_scroll)

    def _remove_from_layout(self, widget: QWidget) -> None:
        index = self.outer.indexOf(widget)
        if index >= 0:
            item = self.outer.takeAt(index)
            if item:
                item.widget().setParent(None)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(176)
        sidebar.setMaximumWidth(196)
        sidebar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.sidebar_layout = QVBoxLayout(sidebar)
        self.sidebar_layout.setContentsMargins(18, 22, 18, 18)
        self.sidebar_layout.setSpacing(8)

        brand = QLabel("PARTLOOM")
        brand.setObjectName("brand")
        self.sidebar_layout.addWidget(brand)
        self.sidebar_tag = QLabel("ENGINEERING AGENT")
        self.sidebar_tag.setObjectName("sidebarTag")
        self.sidebar_layout.addWidget(self.sidebar_tag)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.HLine)
        self.sidebar_layout.addWidget(divider)

        self.nav_buttons: list[QPushButton] = []
        nav_items = [
            ("Agent 工作台", lambda: self.main_scroll.verticalScrollBar().setValue(0)),
            ("PDF 工程图", self.select_pdf2cad_file),
            ("CAD 文件转换", self.select_cad_file),
            ("打开输出目录", self.open_output_folder),
            ("查看运行日志", self.open_gui_log),
        ]
        for index, (item, callback) in enumerate(nav_items):
            button = QPushButton(item)
            button.setObjectName("navButton")
            button.setProperty("active", index == 0)
            button.setCursor(Qt.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(callback)
            self.nav_buttons.append(button)
            self.sidebar_layout.addWidget(button)
        self.sidebar_layout.addStretch()
        self.sidebar_footer = QLabel("LOCAL / COM AUTOMATION")
        self.sidebar_footer.setObjectName("sidebarFooter")
        self.sidebar_layout.addWidget(self.sidebar_footer)
        return sidebar

    def _build_main_area(self) -> QWidget:
        main = QFrame()
        main.setObjectName("mainArea")
        main.setMinimumWidth(0)
        main.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(main)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        eyebrow = QLabel("PARTLOOM AI / CAD WORKSPACE")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("零构 AI 工作台")
        title.setObjectName("title")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        header.addLayout(title_box)
        header.addStretch()
        self.agent_core_label = QLabel("CAD-IR Direct / LLM disabled")
        self.agent_core_label.setObjectName("corePill")
        self.agent_core_label.setProperty("fallback", False)
        self.agent_core_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        header.addWidget(self.agent_core_label)
        self.connection_label = QLabel("SolidWorks 未连接")
        self.connection_label.setObjectName("statusPill")
        self.connection_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        header.addWidget(self.connection_label)
        layout.addLayout(header)

        source_bar = QFrame()
        source_bar.setObjectName("sourceBar")
        source_layout = QHBoxLayout(source_bar)
        source_layout.setContentsMargins(10, 8, 10, 8)
        source_layout.setSpacing(8)
        source_label = QLabel("任务来源")
        source_label.setObjectName("sourceLabel")
        source_layout.addWidget(source_label)
        self.text_source_button = QPushButton("CAD-IR JSON")
        self.text_source_button.setObjectName("sourceButton")
        self.text_source_button.setProperty("active", True)
        self.text_source_button.clicked.connect(self.command_input_focus)
        self.pdf2cad_button = QPushButton("PDF 工程图")
        self.pdf2cad_button.setObjectName("sourceButton")
        self.pdf2cad_button.clicked.connect(self.select_pdf2cad_file)
        self.cadfile_button = QPushButton("CAD / 交换格式")
        self.cadfile_button.setObjectName("sourceButton")
        self.cadfile_button.clicked.connect(self.select_cad_file)
        self.pdf2cad_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.cadfile_button.setIcon(self.style().standardIcon(QStyle.SP_DriveHDIcon))
        source_layout.addWidget(self.text_source_button)
        source_layout.addWidget(self.pdf2cad_button)
        source_layout.addWidget(self.cadfile_button)
        source_layout.addStretch()
        layout.addWidget(source_bar)

        command_panel = QFrame()
        command_panel.setObjectName("agentPanel")
        command_layout = QVBoxLayout(command_panel)
        command_layout.setContentsMargins(18, 16, 18, 16)
        command_layout.setSpacing(10)
        command_header = QHBoxLayout()
        command_title = QLabel("设计任务")
        command_title.setObjectName("sectionTitle")
        command_header.addWidget(command_title)
        command_header.addStretch()
        provider_label = QLabel("规划入口")
        provider_label.setObjectName("fieldLabel")
        command_header.addWidget(provider_label)
        self.provider_combo = QComboBox()
        self.provider_combo.setObjectName("modeCombo")
        self.provider_combo.addItem("CAD-IR 直接输入（DeepSeek 已关闭）", DIRECT_CAD_IR_PROVIDER)
        self.provider_combo.setMinimumWidth(190)
        self.provider_combo.setEnabled(False)
        self.provider_combo.currentIndexChanged.connect(self._refresh_provider_label)
        command_header.addWidget(self.provider_combo)
        mode_label = QLabel("任务模式")
        mode_label.setObjectName("fieldLabel")
        command_header.addWidget(mode_label)
        self.agent_mode_combo = QComboBox()
        self.agent_mode_combo.setObjectName("modeCombo")
        self.agent_mode_combo.addItem("自动识别", "auto")
        self.agent_mode_combo.addItem("仅3D建模", "model_3d")
        self.agent_mode_combo.addItem("仅修改当前模型", "modify_3d")
        self.agent_mode_combo.addItem("仅生成工程图", "create_drawing")
        self.agent_mode_combo.addItem("仅自动标注", "annotate_drawing")
        self.agent_mode_combo.addItem("仅导出文件", "export_files")
        self.agent_mode_combo.addItem("完整流程", "full_pipeline")
        self.agent_mode_combo.setMinimumWidth(170)
        command_header.addWidget(self.agent_mode_combo)
        command_layout.addLayout(command_header)
        self.command_input = QTextEdit()
        self.command_input.setObjectName("commandInput")
        self.command_input.setPlaceholderText(
            "粘贴 cad.ir.v1 / vibecad.design.v1 JSON，或输入本地 .json 文件完整路径。"
            "当前不调用 DeepSeek，也不接受自然语言。"
        )
        self.command_input.setMinimumHeight(150)
        self.command_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        command_layout.addWidget(self.command_input)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.agent_button = QPushButton("校验 CAD-IR 并执行")
        self.agent_button.setObjectName("primaryButton")
        self.agent_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.agent_button.clicked.connect(self.execute_agent_pipeline)
        self.run_button = QPushButton("专家命令")
        self.run_button.setObjectName("secondaryButton")
        self.run_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowForward))
        self.run_button.clicked.connect(self.execute_command)
        clear_button = QPushButton("清空")
        clear_button.setObjectName("quietButton")
        clear_button.setIcon(self.style().standardIcon(QStyle.SP_DialogResetButton))
        clear_button.clicked.connect(self.command_input.clear)
        actions.addWidget(self.agent_button)
        actions.addWidget(self.run_button)
        actions.addStretch()
        actions.addWidget(clear_button)
        command_layout.addLayout(actions)
        layout.addWidget(command_panel)

        tools_tabs = QTabWidget()
        tools_tabs.setObjectName("toolsTabs")
        tools_tabs.setDocumentMode(True)

        expert_tab = QWidget()
        expert_layout = QVBoxLayout(expert_tab)
        expert_layout.setContentsMargins(12, 14, 12, 12)
        expert_layout.setSpacing(10)
        cards = QGridLayout()
        cards.setSpacing(8)
        quick_actions = [
            ("连接 SolidWorks", "连接 SolidWorks"),
            ("读取当前模型", "读取模型"),
            ("创建工程图", "创建工程图"),
            ("生成标准三视图", "生成三视图"),
            ("自动布局", "自动布局"),
            ("自动尺寸标注", "自动尺寸标注"),
            ("导出 PDF", "导出 PDF"),
            ("导出 DWG", "导出 DWG"),
        ]
        for index, (label, command) in enumerate(quick_actions):
            card = QPushButton(label)
            card.setObjectName("toolButton")
            card.setCursor(Qt.PointingHandCursor)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            card.clicked.connect(lambda checked=False, value=command: self.use_template(value))
            cards.addWidget(card, index // 4, index % 4)
        expert_layout.addLayout(cards)
        tools_tabs.addTab(expert_tab, "专家工具")

        batch_tab = QWidget()
        batch_layout = QVBoxLayout(batch_tab)
        batch_layout.setContentsMargins(12, 14, 12, 12)
        batch_layout.setSpacing(10)
        batch_controls = QHBoxLayout()
        select_batch = QPushButton("选择 SLDPRT / SLDASM")
        select_batch.setObjectName("secondaryButton")
        select_batch.setIcon(self.style().standardIcon(QStyle.SP_FileDialogContentsView))
        select_batch.clicked.connect(self.select_batch_files)
        batch_run = QPushButton("生成三视图并导出")
        batch_run.setObjectName("primaryButton")
        batch_run.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        batch_run.clicked.connect(lambda: self.use_template("批量生成三视图"))
        batch_controls.addWidget(select_batch)
        batch_controls.addWidget(batch_run)
        batch_controls.addStretch()
        self.batch_label = QLabel("未选择模型")
        self.batch_label.setObjectName("muted")
        batch_layout.addLayout(batch_controls)
        batch_layout.addWidget(self.batch_label)
        tools_tabs.addTab(batch_tab, "批量任务")

        file_tab = QWidget()
        file_layout = QGridLayout(file_tab)
        file_layout.setContentsMargins(12, 14, 12, 12)
        file_layout.setSpacing(8)
        pdf_entry = QPushButton("选择 PDF 工程图")
        pdf_entry.setObjectName("toolButton")
        pdf_entry.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        pdf_entry.clicked.connect(self.select_pdf2cad_file)
        cad_entry = QPushButton("选择 DWG / DXF / STEP / IGES / STL")
        cad_entry.setObjectName("toolButton")
        cad_entry.setIcon(self.style().standardIcon(QStyle.SP_DriveHDIcon))
        cad_entry.clicked.connect(self.select_cad_file)
        open_output_button = QPushButton("打开输出文件夹")
        open_output_button.setObjectName("secondaryButton")
        open_output_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        open_output_button.clicked.connect(self.open_output_folder)
        file_layout.addWidget(pdf_entry, 0, 0)
        file_layout.addWidget(cad_entry, 0, 1)
        file_layout.addWidget(open_output_button, 0, 2)
        file_layout.setColumnStretch(0, 1)
        file_layout.setColumnStretch(1, 2)
        file_layout.setColumnStretch(2, 1)
        tools_tabs.addTab(file_tab, "文件与输出")
        layout.addWidget(tools_tabs)

        log_header = QHBoxLayout()
        log_title = QLabel("实时日志")
        log_title.setObjectName("sectionTitle")
        open_log_button = QPushButton("打开日志")
        open_log_button.setObjectName("quietButton")
        open_log_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        open_log_button.clicked.connect(self.open_gui_log)
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(open_log_button)
        layout.addLayout(log_header)
        self.log_output = QTextEdit()
        self.log_output.setObjectName("logOutput")
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(190)
        layout.addWidget(self.log_output, 1)
        return main

    def _build_status_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("statusPanel")
        panel.setMinimumWidth(0)
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(10)

        heading_row = QHBoxLayout()
        heading = QLabel("Pipeline Monitor")
        heading.setObjectName("panelTitle")
        live = QLabel("LIVE")
        live.setObjectName("livePill")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        heading_row.addWidget(live)
        layout.addLayout(heading_row)

        self.result_value = self._add_status_row(layout, "执行状态", "等待执行")
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)

        current_label = QLabel("当前执行")
        current_label.setObjectName("statusSection")
        layout.addWidget(current_label)
        self.sw_status_value = self._add_status_row(layout, "SolidWorks", "未连接")
        self.model_value = self._add_status_row(layout, "活动文档", "N/A")
        self.task_value = self._add_status_row(layout, "当前任务", "待命")
        self.skill_value = self._add_status_row(layout, "当前 Skill", "未选择")
        self.pipeline_event_value = self._add_status_row(layout, "Pipeline Event", "idle")
        self.pipeline_step_value = self._add_status_row(layout, "当前步骤", "待命")
        self.activity_value = self._add_status_row(layout, "运行状态", "待命")

        output_label = QLabel("任务产物")
        output_label.setObjectName("statusSection")
        layout.addWidget(output_label)
        self.pipeline_report_value = self._add_status_row(layout, "Pipeline Report", "N/A")
        self.pipeline_output_value = self._add_status_row(layout, "输出目录", "N/A")
        self.pipeline_artifacts_value = self._add_status_row(layout, "输出文件", "N/A")

        monitor_actions = QGridLayout()
        monitor_actions.setSpacing(6)
        open_report = QPushButton("打开报告")
        open_report.setObjectName("secondaryButton")
        open_report.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        open_report.clicked.connect(self.open_pipeline_report)
        copy_path = QPushButton("复制路径")
        copy_path.setObjectName("secondaryButton")
        copy_path.setIcon(self.style().standardIcon(QStyle.SP_FileLinkIcon))
        copy_path.clicked.connect(self.copy_pipeline_output_path)
        rerun = QPushButton("重新执行")
        rerun.setObjectName("secondaryButton")
        rerun.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        rerun.clicked.connect(self.rerun_pipeline)
        monitor_actions.addWidget(open_report, 0, 0)
        monitor_actions.addWidget(copy_path, 0, 1)
        monitor_actions.addWidget(rerun, 1, 0, 1, 2)
        layout.addLayout(monitor_actions)

        system_label = QLabel("系统能力")
        system_label.setObjectName("statusSection")
        layout.addWidget(system_label)
        self.skill_registry_value = self._add_status_row(
            layout,
            "Registry",
            "\n".join(self.skill_manager.status_lines()),
        )
        self.skill_registry_value.setProperty("dense", True)
        layout.addStretch()
        return panel

    def _apply_style(self) -> None:
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet(
            """
            QMainWindow { background: #101416; color: #f1f5f3; }
            #root { background: #101416; }
            QWidget { color: #e8eeeb; }
            QScrollArea { background: transparent; border: 0; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { width: 9px; background: #111619; border: 0; }
            QScrollBar::handle:vertical { min-height: 48px; border-radius: 4px; background: #394448; }
            QScrollBar::handle:vertical:hover { background: #4b595d; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            #sidebar { background: #14191c; border-right: 1px solid #293136; }
            #brand { font-size: 24px; font-weight: 900; color: #f1f5f3; padding: 0; }
            #sidebarTag, #sidebarFooter, #eyebrow { color: #6f817d; font-size: 10px; font-weight: 800; }
            #divider { color: #2a3337; background: #2a3337; max-height: 1px; margin: 12px 0; }
            #navButton { text-align: left; min-height: 38px; padding: 0 10px; border: 1px solid transparent; border-radius: 6px;
                background: transparent; color: #9caaa7; font-size: 13px; font-weight: 700; }
            #navButton:hover { background: #1c2326; color: #f1f5f3; border-color: #303a3e; }
            #navButton[active="true"] { background: #20302d; color: #7ee2cc; border-color: #31524b; }
            #mainArea { background: #101416; }
            #title { font-size: 25px; font-weight: 900; color: #f4f7f5; }
            #muted, #fieldLabel, #sourceLabel { color: #8d9b98; font-size: 12px; }
            #sourceBar { background: #151a1d; border: 1px solid #293136; border-radius: 6px; }
            #sourceButton { min-height: 32px; padding: 0 12px; border: 1px solid transparent; border-radius: 5px;
                background: transparent; color: #a8b4b1; font-weight: 700; }
            #sourceButton:hover { color: #f2f5f3; background: #20272a; }
            #sourceButton[active="true"] { color: #74dcc5; background: #20302d; border-color: #31524b; }
            #agentPanel { background: #171d20; border: 1px solid #30393d; border-radius: 8px; }
            #statusPill, #corePill, #livePill { padding: 7px 10px; border-radius: 6px; font-size: 11px; font-weight: 800; }
            #statusPill { color: #f2b84b; background: #2b2619; border: 1px solid #574821; }
            #statusPill[connected="true"] { color: #77ddc6; background: #1d2a27; border-color: #315048; }
            #corePill { color: #77ddc6; background: #1d2a27; border: 1px solid #315048; }
            #corePill[fallback="true"] { color: #f2b84b; background: #2b2619; border-color: #574821; }
            #livePill { color: #77ddc6; background: #1d2a27; border: 1px solid #315048; padding: 4px 7px; }
            #commandInput { background: #111618; border: 1px solid #354044; border-radius: 6px;
                padding: 14px; color: #f1f5f3; font-size: 15px; selection-background-color: #287d6b; }
            #commandInput:focus { border: 1px solid #43bfa5; background: #13191b; }
            QComboBox { min-height: 34px; padding: 0 10px; border-radius: 5px; background: #111618;
                color: #e8eeeb; border: 1px solid #354044; }
            QComboBox:hover, QComboBox:focus { border-color: #43bfa5; }
            QComboBox QAbstractItemView { background: #171d20; color: #e8eeeb; border: 1px solid #354044; selection-background-color: #254d45; }
            #primaryButton { min-height: 40px; padding: 0 18px; border: 1px solid #45bfa6; border-radius: 6px;
                color: #081310; font-weight: 850; background: #52d1b5; }
            #primaryButton:hover { background: #69ddc4; border-color: #69ddc4; }
            #primaryButton:pressed { background: #3cad95; }
            #primaryButton:disabled { color: #67716f; background: #242b2e; border-color: #30383b; }
            #secondaryButton { min-height: 38px; padding: 0 14px; border-radius: 6px;
                background: #20272a; color: #d9e0dd; border: 1px solid #384347; font-weight: 700; }
            #secondaryButton:hover { background: #283135; border-color: #536267; color: #ffffff; }
            #quietButton { min-height: 34px; padding: 0 10px; border-radius: 5px; border: 1px solid transparent;
                background: transparent; color: #8e9b98; font-weight: 700; }
            #quietButton:hover { color: #e8eeeb; background: #20272a; border-color: #303a3e; }
            #toolButton { min-height: 54px; border-radius: 6px; background: #1b2225;
                border: 1px solid #30393d; color: #dfe5e2; font-size: 13px; font-weight: 750; padding: 0 12px; }
            #toolButton:hover { border-color: #3f7469; background: #202b2a; color: #85e1cd; }
            #sectionTitle, #panelTitle { font-size: 16px; font-weight: 900; color: #f1f5f3; }
            #toolsTabs::pane { border: 1px solid #2e373b; background: #151a1d; border-radius: 0 6px 6px 6px; }
            QTabBar::tab { min-width: 108px; min-height: 34px; padding: 0 12px; color: #8e9b98;
                background: #111618; border: 1px solid #2b3438; border-bottom: 0; }
            QTabBar::tab:first { border-top-left-radius: 6px; }
            QTabBar::tab:last { border-top-right-radius: 6px; }
            QTabBar::tab:selected { color: #7ee2cc; background: #19211f; border-color: #36564f; }
            QTabBar::tab:hover:!selected { color: #dfe5e2; background: #1a2023; }
            #statusPanel { background: #14191c; border-left: 1px solid #293136; }
            #statusSection { margin-top: 7px; color: #6f817d; font-size: 10px; font-weight: 900; }
            #statusItem { background: #181e21; border: 1px solid #293236; border-radius: 6px; }
            #statusItemTitle { color: #7f8d8a; font-size: 10px; font-weight: 700; }
            #statusValue { color: #e8eeeb; font-size: 12px; font-weight: 700; }
            #statusValue[dense="true"] { color: #9eaaa7; font-size: 10px; font-family: Consolas, monospace; }
            #logOutput { background: #0d1113; border: 1px solid #2c3539; border-radius: 6px;
                padding: 12px; color: #a9d9ce; font-family: Consolas, monospace; font-size: 11px; }
            #progressBar { min-height: 16px; max-height: 16px; border-radius: 4px; background: #20272a; border: 1px solid #30393d;
                color: #dfe5e2; text-align: center; font-size: 9px; }
            #progressBar::chunk { border-radius: 3px; background: #4fc8ad; }
            #statusValue[active="true"] { color: #7ee2cc; }
            QMessageBox { background: #171d20; }
            QMessageBox QLabel { color: #e8eeeb; }
            """
        )

    def _add_status_row(self, layout: QVBoxLayout, title: str, value: str) -> QLabel:
        item = QFrame()
        item.setObjectName("statusItem")
        item_layout = QVBoxLayout(item)
        item_layout.setContentsMargins(10, 7, 10, 8)
        item_layout.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("statusItemTitle")
        value_label = QLabel(value)
        value_label.setObjectName("statusValue")
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        item_layout.addWidget(label)
        item_layout.addWidget(value_label)
        layout.addWidget(item)
        return value_label

    def refresh_status(self) -> None:
        if os.environ.get("PARTLOOM_DISABLE_CAD_PROBE", "").strip().lower() in {"1", "true", "yes"}:
            self.connection_label.setText("SolidWorks 未检测")
            self.sw_status_value.setText("检测已禁用")
            return
        if self._status_probe_running or self._thread_is_running():
            return
        self._status_probe_running = True
        self.connection_label.setText("SolidWorks 检测中")
        self.sw_status_value.setText("检测中")

        def run_probe() -> None:
            self._status_bridge.ready.emit(probe_solidworks_status())

        threading.Thread(
            target=run_probe,
            name="SolidWorksStatusProbe",
            daemon=True,
        ).start()

    def _apply_solidworks_status(self, status: dict) -> None:
        self._status_probe_running = False
        elapsed_s = float(status.get("elapsed_s") or 0.0)
        if elapsed_s >= 1.0:
            logging.info("SolidWorks background status probe completed in %.3fs", elapsed_s)
        if not status.get("connected"):
            self.connection_label.setText("SolidWorks 未连接")
            self.connection_label.setProperty("connected", False)
            self.connection_label.style().unpolish(self.connection_label)
            self.connection_label.style().polish(self.connection_label)
            self.sw_status_value.setText("未连接")
            self.model_value.setText("N/A")
            return
        self.connection_label.setText("SolidWorks 已连接")
        self.connection_label.setProperty("connected", True)
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self.sw_status_value.setText("已连接")
        info = status.get("info") or {}
        self.model_value.setText(info.get("file_name", "N/A") if info else "N/A")

    def use_template(self, text: str) -> None:
        if self._thread_is_running():
            self.append_log("当前任务正在执行，请稍后再试。")
            return
        self.command_input.setPlainText(text)
        self.execute_command()

    def command_input_focus(self) -> None:
        self._set_source_active("text")
        self.command_input.setFocus()

    def _set_source_active(self, source: str) -> None:
        for key, button in (
            ("text", self.text_source_button),
            ("pdf", self.pdf2cad_button),
            ("cad", self.cadfile_button),
        ):
            button.setProperty("active", key == source)
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_task_controls_enabled(self, enabled: bool) -> None:
        for button in (self.run_button, self.agent_button, self.pdf2cad_button, self.cadfile_button):
            button.setEnabled(enabled)
        if hasattr(self, "provider_combo"):
            self.provider_combo.setEnabled(False)
        if hasattr(self, "agent_mode_combo"):
            self.agent_mode_combo.setEnabled(enabled)
        if enabled:
            if not self.status_timer.isActive():
                self.status_timer.start(20000)
            QTimer.singleShot(250, self.refresh_status)
        else:
            self.status_timer.stop()

    def _thread_is_running(self) -> bool:
        thread = self.thread
        if thread is None:
            return False
        try:
            return bool(thread.isRunning())
        except RuntimeError:
            self._release_thread_reference(thread)
            return False

    def _release_thread_reference(self, thread: QThread) -> None:
        if self.thread is thread:
            self.thread = None

    def _refresh_provider_label(self, *_args) -> None:
        provider_id = str(self.provider_combo.currentData() or "auto")
        if provider_id == DIRECT_CAD_IR_PROVIDER:
            self.agent_core_label.setText("CAD-IR Direct / LLM disabled")
            self.agent_core_label.setProperty("fallback", False)
            self.agent_core_label.style().unpolish(self.agent_core_label)
            self.agent_core_label.style().polish(self.agent_core_label)
            return
        try:
            _provider, status = self.provider_registry.resolve(provider_id)
            self.agent_core_label.setText(f"Provider {status.id} / {status.model}")
            self.agent_core_label.setProperty("fallback", status.id == "local_fallback")
            self.agent_core_label.style().unpolish(self.agent_core_label)
            self.agent_core_label.style().polish(self.agent_core_label)
        except Exception as exc:
            self.agent_core_label.setText(f"Provider unavailable: {provider_id}")
            self.agent_core_label.setProperty("fallback", True)
            self.append_log(f"Provider selection failed: {redact_secrets(exc)}")

    def open_gui_log(self) -> None:
        try:
            os.startfile(str(self.log_file))
        except Exception as exc:
            self.append_log(f"打开日志失败：{exc}")

    def select_batch_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 SolidWorks 模型",
            str(Path.home() / "Desktop"),
            "SolidWorks Models (*.sldprt *.sldasm *.SLDPRT *.SLDASM)",
        )
        if not files:
            return
        self.batch_paths = files
        self.batch_label.setText(f"已选择 {len(files)} 个模型")
        self.append_log(f"已选择批量模型：{len(files)} 个")

    def select_pdf2cad_file(self) -> None:
        if self._thread_is_running():
            self.append_log("当前任务正在执行，请稍后再试。")
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 PDF 工程图",
            str(Path.home() / "Desktop"),
            "PDF Files (*.pdf *.PDF)",
        )
        if not file_path:
            return
        self._set_source_active("pdf")
        self.execute_pdf2cad(file_path)

    def select_cad_file(self) -> None:
        if self._thread_is_running():
            self.append_log("当前任务正在执行，请稍后再试。")
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 CAD 或交换格式文件",
            str(Path.home() / "Desktop"),
            (
                "CAD and Exchange Files (*.dwg *.dxf *.dwt *.sldprt *.sldasm *.slddrw "
                "*.step *.stp *.iges *.igs *.stl *.x_t *.x_b);;"
                "AutoCAD (*.dwg *.dxf *.dwt);;"
                "SolidWorks and Exchange (*.sldprt *.sldasm *.slddrw *.step *.stp *.iges *.igs *.stl *.x_t *.x_b)"
            ),
        )
        if not file_path:
            return
        self._set_source_active("cad")
        prompt = self.command_input.toPlainText().strip()
        outputs = self._requested_cad_outputs(prompt)
        try:
            manager = CADAgentSkillManager(LOG_DIR / "skill_outputs")
            planned = manager.file2cad_runner.plan(file_path, requested_outputs=outputs, prompt=prompt)
            summary = planned.get("summary", {})
        except Exception as exc:
            self.append_log(traceback.format_exc())
            QMessageBox.critical(self, "CAD 文件计划失败", str(exc))
            return
        if summary.get("needs_confirmation"):
            reason = str(summary.get("confirmation_reason") or "当前格式转换不受支持。")
            self.append_log(reason)
            QMessageBox.warning(self, "需要确认", reason)
            return
        lines = [
            f"源文件：{Path(file_path).name}",
            f"输出：{', '.join(summary.get('outputs', []))}",
            "",
            "计划执行：",
            *[f"- {item}" for item in summary.get("plan_lines", [])],
        ]
        if QMessageBox.question(self, "确认 CAD 文件任务", "\n".join(lines)) != QMessageBox.Yes:
            self.append_log("File2CAD plan was not executed.")
            return
        self.execute_file2cad(file_path, outputs, prompt)

    @staticmethod
    def _requested_cad_outputs(prompt: str) -> list[str] | None:
        text = prompt.upper()
        mappings = (
            ("ANNOTATED DWG", ("ANNOTATED DWG", "标注DWG", "标注 DWG")),
            ("SLDPRT", ("SLDPRT",)),
            ("SLDASM", ("SLDASM",)),
            ("SLDDRW", ("SLDDRW",)),
            ("STEP", ("STEP", "STP")),
            ("IGES", ("IGES", "IGS")),
            ("STL", ("STL",)),
            ("DWG", ("DWG",)),
            ("DXF", ("DXF",)),
            ("PDF", ("PDF",)),
        )
        outputs = [name for name, tokens in mappings if any(token in text for token in tokens)]
        return outputs or None

    def execute_file2cad(self, source_path: str, requested_outputs: list[str] | None, prompt: str) -> None:
        self.last_pipeline_kind = "cad_file"
        self.last_file_request = {
            "source_path": source_path,
            "requested_outputs": requested_outputs,
            "prompt": prompt,
        }
        self.last_pipeline_result = {}
        self._set_task_controls_enabled(False)
        self.progress_bar.setValue(0)
        self.result_value.setText("File2CAD running")
        self.pipeline_event_value.setText("file2cad_started")
        self.pipeline_step_value.setText("CAD File Router")
        self.pipeline_report_value.setText("N/A")
        self.pipeline_output_value.setText("N/A")
        self.pipeline_artifacts_value.setText("N/A")
        self.task_value.setText("CAD File Conversion")
        self.skill_value.setText("File2CAD Router")
        self.append_log(f"> File2CAD: {source_path}")

        self.thread = QThread(self)
        self.worker = CADFileWorker(source_path, requested_outputs=requested_outputs, prompt=prompt)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.skill.connect(self.skill_value.setText)
        self.worker.task.connect(self.task_value.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.result.connect(self.on_pipeline_result)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(lambda thread=self.thread: self._release_thread_reference(thread))
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self._set_task_controls_enabled(True))
        self.thread.start()

    def execute_pdf2cad(self, pdf_path: str) -> None:
        self.last_pipeline_kind = "pdf"
        self.last_file_request = {"source_path": pdf_path, "mode": "2d"}
        self.last_pipeline_prompt = f"PDF2CAD: {pdf_path}"
        self.last_pipeline_result = {}
        self.pending_pdf_plan = {}
        self._set_task_controls_enabled(False)
        self.progress_bar.setValue(0)
        self.result_value.setText("PDF2CAD running")
        self.pipeline_event_value.setText("pdf2cad_started")
        self.pipeline_step_value.setText("PDF Parse")
        self.pipeline_report_value.setText("N/A")
        self.pipeline_output_value.setText("N/A")
        self.pipeline_artifacts_value.setText("N/A")
        self.task_value.setText("PDF2CAD Pipeline")
        self.skill_value.setText("PDF Parse Skill")
        self.append_log(f"> PDF2CAD: {pdf_path}")

        self.thread = QThread(self)
        self.worker = PDF2CADWorker(pdf_path, mode="2d", plan_only=True)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.skill.connect(self.skill_value.setText)
        self.worker.task.connect(self.task_value.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.result.connect(self.on_pdf_plan_ready)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(lambda thread=self.thread: self._release_thread_reference(thread))
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.present_pdf_plan)
        self.thread.start()

    def on_pdf_plan_ready(self, ok: bool, message: str, data: dict) -> None:
        self.pending_pdf_plan = {"ok": ok, "message": message, "data": data}
        self.append_log(message)
        design = data.get("design_json", {}) if isinstance(data, dict) else {}
        params = design.get("parameters", {}) if isinstance(design, dict) else {}
        title = design.get("title_block", {}) if isinstance(design, dict) else {}
        lines = [
            f"parser: {data.get('parser')}",
            f"size: {params.get('length', 'N/A')} x {params.get('width', 'N/A')} mm",
            f"grid: {params.get('grid_pitch_x', 'N/A')} x {params.get('grid_pitch_y', 'N/A')} mm",
            f"wire_diameter: {params.get('wire_diameter', 'N/A')} mm",
            f"material_candidates: {design.get('material_candidates', [])}",
            f"drawing_no: {title.get('drawing_no', 'N/A')}",
            f"name: {title.get('name', 'N/A')}",
        ]
        self.pipeline_artifacts_value.setText("\n".join(lines))
        self.result_value.setText("等待确认" if ok else "解析失败")

    def present_pdf_plan(self) -> None:
        pending = dict(self.pending_pdf_plan)
        self.pending_pdf_plan = {}
        if not pending.get("ok"):
            self._set_task_controls_enabled(True)
            QMessageBox.critical(self, "PDF 解析失败", str(pending.get("message") or "PDF planning failed."))
            return
        data = pending.get("data", {})
        design = dict(data.get("design_json", {}))
        summary = dict(data.get("summary", {}))
        if summary.get("needs_confirmation"):
            unresolved = list(summary.get("unexecutable_required_features", []))
            non_material = [item for item in unresolved if item.get("type") != "drawing_conflict"]
            material_risks = [item for item in design.get("risks", []) if item.get("type") == "material_conflict"]
            candidates = list(design.get("material_candidates", []))
            if non_material or not material_risks or len(candidates) < 2:
                self._set_task_controls_enabled(True)
                reason = str(summary.get("confirmation_reason") or "识别参数不完整，请检查 Design JSON。")
                QMessageBox.warning(self, "PDF 图纸需要补充参数", reason)
                return
            box = QMessageBox(self)
            box.setWindowTitle("确认 PDF 图纸材料")
            box.setIcon(QMessageBox.Warning)
            params = design.get("parameters", {})
            box.setText(
                "图纸材料字段存在冲突。\n\n"
                f"识别尺寸：{params.get('length')} x {params.get('width')} mm\n"
                f"网格：{params.get('grid_pitch_x')} x {params.get('grid_pitch_y')} mm\n"
                f"线径：{params.get('wire_diameter')} mm\n\n"
                "请选择本次重建采用的材料："
            )
            material_buttons = {box.addButton(str(candidate), QMessageBox.AcceptRole): str(candidate) for candidate in candidates}
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            selected = material_buttons.get(box.clickedButton())
            if not selected:
                self._set_task_controls_enabled(True)
                self.append_log("PDF2CAD material conflict was not confirmed.")
                return
            design.setdefault("parameters", {})["material"] = selected
            design["risks"] = [item for item in design.get("risks", []) if item.get("type") != "material_conflict"]
            design["unsupported_features"] = [
                item for item in design.get("unsupported_features", []) if item.get("type") != "drawing_conflict"
            ]
            design["needs_confirmation"] = bool(design["unsupported_features"])
            design["confirmation_reason"] = "" if not design["needs_confirmation"] else design.get("confirmation_reason", "")
            self.append_log(f"PDF2CAD material confirmed: {selected}")
        else:
            params = design.get("parameters", {})
            question = (
                f"识别尺寸：{params.get('length')} x {params.get('width')} mm\n"
                f"输出：{', '.join(summary.get('outputs', []))}\n\n确认生成二维 CAD？"
            )
            if QMessageBox.question(self, "确认 PDF2CAD 计划", question) != QMessageBox.Yes:
                self._set_task_controls_enabled(True)
                self.append_log("PDF2CAD plan was not executed.")
                return
        if design.get("needs_confirmation"):
            self._set_task_controls_enabled(True)
            QMessageBox.warning(self, "仍需确认", str(design.get("confirmation_reason") or "计划仍包含未解决问题。"))
            return
        self._execute_confirmed_pdf(str(self.last_file_request.get("source_path") or ""), design)

    def _execute_confirmed_pdf(self, pdf_path: str, design_json: dict) -> None:
        self._set_task_controls_enabled(False)
        self.progress_bar.setValue(15)
        self.result_value.setText("PDF2CAD running")
        self.pipeline_event_value.setText("pdf2cad_confirmed")
        self.pipeline_step_value.setText("CAD Reconstruction")
        self.task_value.setText("PDF2CAD Pipeline")
        self.skill_value.setText("AutoCAD Reconstruction")

        self.thread = QThread(self)
        self.worker = PDF2CADWorker(pdf_path, mode="2d", design_json=design_json)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.skill.connect(self.skill_value.setText)
        self.worker.task.connect(self.task_value.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.result.connect(self.on_pipeline_result)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(lambda thread=self.thread: self._release_thread_reference(thread))
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self._set_task_controls_enabled(True))
        self.thread.start()

    def open_output_folder(self) -> None:
        output_dir = output_root()
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(output_dir))
            self.append_log(f"已打开输出文件夹：{output_dir}")
        except Exception as exc:
            self.append_log(f"打开输出文件夹失败：{exc}")

    def execute_agent_pipeline(self) -> None:
        cad_ir_text = self.command_input.toPlainText().strip()
        if not cad_ir_text:
            self.append_log("请输入 CAD-IR JSON。")
            return
        if self._thread_is_running():
            self.append_log("当前任务正在执行，请稍后再试。")
            return
        stage_mode = str(self.agent_mode_combo.currentData() or "auto")
        provider_id = "local_fallback"
        try:
            self.append_log("Planning provider=direct_cad_ir, model=deterministic, llm=disabled")
            planned = DirectCADIRService(LOG_DIR / "skill_outputs").plan(
                cad_ir_text,
                stage_mode=stage_mode,
            )
            self.append_log("planning_provider=direct_cad_ir")
            self.append_log(
                f"cad_ir_input_format={planned.get('direct_cad_ir_input_format', 'json')}"
            )
            summary = stage_summary(planned)
        except Exception as exc:
            self.append_log(redact_secrets(traceback.format_exc()))
            self.append_log(f"Planning failed before execution: {redact_secrets(exc)}")
            return
        self._show_stage_plan(summary)
        unsupported = summary.get("unexecutable_required_features", [])
        if unsupported:
            self.append_log(f"required_features_not_executable={unsupported}")
        if not self._confirm_agent_plan(summary):
            self.append_log("CAD-IR plan was not authorized; Pipeline and SolidWorks were not started.")
            self.command_input.setFocus()
            return
        if planned.get("planning_contract"):
            try:
                planned = PlannerValidator.authorize_after_confirmation(planned)
                summary = stage_summary(planned)
                self.append_log("Planner Validator authorization recorded after GUI confirmation.")
            except Exception as exc:
                self.append_log(redact_secrets(traceback.format_exc()))
                self.append_log(f"Planner authorization failed: {redact_secrets(exc)}")
                self.command_input.setFocus()
                return
        self.last_pipeline_kind = "agent"
        self.last_file_request = {}
        self.last_pipeline_prompt = "Direct CAD-IR GUI input"
        self.last_pipeline_result = {}
        self._set_task_controls_enabled(False)
        self.progress_bar.setValue(0)
        self.result_value.setText("Pipeline running")
        self.pipeline_event_value.setText("pipeline_started")
        self.pipeline_step_value.setText(str(summary.get("task_type") or "CAD-IR Validator"))
        self.pipeline_report_value.setText("N/A")
        self.pipeline_output_value.setText("N/A")
        self.pipeline_artifacts_value.setText("N/A")
        self.task_value.setText(str(summary.get("task_type") or "CAD-IR Pipeline"))
        self.skill_value.setText("CAD-IR Validator")
        self.append_log(f"> Direct CAD-IR: {len(cad_ir_text)} characters")
        self.append_log(f"task_type={summary.get('task_type')}")
        self.append_log(f"requested_stages={summary.get('requested_stages')}")
        self.append_log(f"stop_after={summary.get('stop_after')}")
        self.append_log(f"planner_validation_status={summary.get('planner_status')}")
        self.append_log(f"planner_allow_execution={summary.get('allow_execution')}")

        self.thread = QThread(self)
        self.worker = PipelineWorker(
            "Direct CAD-IR GUI input",
            execute_real_skills=True,
            stage_mode=stage_mode,
            provider_id=provider_id,
            design_json=planned,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.skill.connect(self.skill_value.setText)
        self.worker.task.connect(self.task_value.setText)
        self.worker.task.connect(self.pipeline_step_value.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.pipeline_event.connect(self.on_pipeline_event)
        self.worker.result.connect(self.on_pipeline_result)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(lambda thread=self.thread: self._release_thread_reference(thread))
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self._set_task_controls_enabled(True))
        self.thread.start()

    def _show_stage_plan(self, summary: dict) -> None:
        lines = [
            f"任务类型: {summary.get('task_type')}",
            f"requested_stages: {summary.get('requested_stages')}",
            "计划执行:",
        ]
        lines.extend(f"- {line}" for line in summary.get("plan_lines", []))
        unsupported = summary.get("unexecutable_required_features", [])
        if unsupported:
            lines.append("Required features are not executable:")
            lines.extend(f"- {item.get('name') or item.get('type')}: {item.get('reason')}" for item in unsupported)
        lines.append("本次不会执行:")
        skipped = summary.get("skipped_lines", [])
        if skipped:
            lines.extend(f"- {line}" for line in skipped)
        else:
            lines.append("- 无")
        self.pipeline_artifacts_value.setText("\n".join(lines[:14]))
        self.append_log("\n".join(lines))
        validation = summary.get("planning_validation") or {}
        if validation:
            self.append_log(
                "Planner Validator: "
                f"status={validation.get('status')}, "
                f"confidence={validation.get('model_confidence')}, "
                f"allow_pipeline={validation.get('allow_pipeline')}"
            )

    def _confirm_agent_plan(self, summary: dict) -> bool:
        dialog = PlannerPreviewDialog(summary, self)
        return dialog.exec() == QDialog.Accepted

    def execute_command(self) -> None:
        command = self.command_input.toPlainText().strip()
        if not command:
            self.append_log("请输入指令。")
            return
        self._set_task_controls_enabled(False)
        self.progress_bar.setValue(0)
        self.result_value.setText("执行中")
        self.task_value.setText(command)
        self.append_log(f"> {command}")

        self.thread = QThread(self)
        self.worker = SolidWorksWorker(command, self.batch_paths)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.status.connect(self.sw_status_value.setText)
        self.worker.skill.connect(self.skill_value.setText)
        self.worker.model.connect(self.model_value.setText)
        self.worker.task.connect(self.task_value.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.result.connect(self.on_task_result)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(lambda thread=self.thread: self._release_thread_reference(thread))
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self._set_task_controls_enabled(True))
        self.thread.start()

    def on_task_result(self, ok: bool, message: str) -> None:
        self.result_value.setText("成功" if ok else "失败")
        self.append_log(message)

    def on_pipeline_event(self, event: dict) -> None:
        event_name = str(event.get("event", ""))
        self.pipeline_event_value.setText(event_name)
        step_key = str(event.get("skill_key", ""))
        stage = str(event.get("stage", ""))
        if step_key:
            self.pipeline_step_value.setText(f"{step_key} {stage}".strip())
        if event_name == "pipeline_finished":
            self.result_value.setText(str(event.get("status", "")))

    def on_pipeline_result(self, ok: bool, message: str, data: dict) -> None:
        self.last_pipeline_result = data
        self.result_value.setText("成功" if ok else "失败")
        self.append_log(message)
        if data.get("task_type"):
            self.task_value.setText(str(data.get("task_type")))
            self.append_log(f"completed_task_type={data.get('task_type')}")
            self.append_log(f"completed_requested_stages={data.get('requested_stages')}")
            self.append_log(f"not_executed_stages={data.get('forbidden_stages')}")
            self.append_log("waiting_for_user_continue=true")
        report_path = str(data.get("report_path") or "")
        report_exists = bool(report_path and Path(report_path).exists())
        status = str(data.get("status") or ("success" if data.get("success") else "failed"))
        ok = bool(ok and status == "success" and report_exists)
        self.result_value.setText("完成" if ok else "失败")
        artifacts = data.get("artifacts", {}) if isinstance(data, dict) else {}
        if report_path:
            self.pipeline_report_value.setText(report_path)
        delivery_dir = str(artifacts.get("delivery_dir") or "")
        if delivery_dir:
            self.pipeline_output_value.setText(delivery_dir)
        artifact_lines = []
        if data.get("task_type"):
            artifact_lines.extend(
                [
                    f"task_type: {data.get('task_type')}",
                    f"requested_stages: {data.get('requested_stages')}",
                    f"not_executed: {data.get('forbidden_stages')}",
                    "waiting_for_user_continue: true",
                ]
            )
        for key in (
            "source_file",
            "sldprt",
            "sldasm",
            "slddrw",
            "solidworks_model",
            "step",
            "stl",
            "iges",
            "dwg",
            "dxf",
            "pdf",
            "annotated_dwg",
            "drawing_ir",
            "pdf2cad_report",
            "autocad_annotation_report",
            "delivery_pipeline_report",
        ):
            value = artifacts.get(key)
            if value:
                artifact_lines.append(f"{key}: {value}")
        self.pipeline_artifacts_value.setText("\n".join(artifact_lines[:8]) if artifact_lines else "N/A")

    def open_pipeline_report(self) -> None:
        report_path = self.last_pipeline_result.get("report_path") or self.pipeline_report_value.text()
        path = Path(str(report_path))
        if not path.exists():
            self.append_log("Pipeline report does not exist yet.")
            return
        os.startfile(str(path))

    def copy_pipeline_output_path(self) -> None:
        artifacts = self.last_pipeline_result.get("artifacts", {}) if self.last_pipeline_result else {}
        path = str(artifacts.get("delivery_dir") or artifacts.get("annotated_dwg") or self.pipeline_output_value.text())
        QApplication.clipboard().setText(path)
        self.append_log(f"Copied path: {path}")

    def rerun_pipeline(self) -> None:
        if self.last_pipeline_kind == "pdf" and self.last_file_request.get("source_path"):
            self.execute_pdf2cad(str(self.last_file_request["source_path"]))
            return
        if self.last_pipeline_kind == "cad_file" and self.last_file_request.get("source_path"):
            self.execute_file2cad(
                str(self.last_file_request["source_path"]),
                self.last_file_request.get("requested_outputs"),
                str(self.last_file_request.get("prompt") or ""),
            )
            return
        if not self.last_pipeline_prompt:
            self.append_log("No previous AI Agent prompt to rerun.")
            return
        self.command_input.setPlainText(self.last_pipeline_prompt)
        self.execute_agent_pipeline()

    def update_activity_animation(self) -> None:
        running = self._thread_is_running()
        if not running:
            self.activity_value.setProperty("active", False)
            self.activity_value.setText("待命")
            self.activity_value.style().unpolish(self.activity_value)
            self.activity_value.style().polish(self.activity_value)
            return
        self.activity_tick = (self.activity_tick + 1) % 4
        self.activity_value.setProperty("active", True)
        self.activity_value.setText("运行中" + "." * self.activity_tick)
        self.activity_value.style().unpolish(self.activity_value)
        self.activity_value.style().polish(self.activity_value)

    def append_log(self, text: str) -> None:
        self.log_output.append(text)
        logging.info(text)


def main() -> int:
    app = QApplication(sys.argv)
    if notify_existing_instance():
        return 0

    log_file = setup_file_logging()

    def log_uncaught(exc_type, exc, tb) -> None:
        logging.error("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    logging.info("PartLoom AI Platform starting.")

    server = create_single_instance_server(app)
    if server is None:
        return 0

    window = MainWindow(log_file)

    def activate_window() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                connection.readAll()
                connection.disconnectFromServer()
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(activate_window)
    app.aboutToQuit.connect(server.close)
    app._single_instance_server = server
    window.append_log(f"日志文件：{log_file}")
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
