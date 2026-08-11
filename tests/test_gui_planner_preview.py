from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QLabel

from app import PlannerPreviewDialog


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _summary(status: str, confirmation_allowed: bool) -> dict:
    return {
        "task_type": "model_3d",
        "requested_stages": ["model_3d"],
        "stop_after": "model_3d",
        "part_type": "mounting_plate",
        "allow_execution": status == "approved",
        "confirmation_allowed": confirmation_allowed,
        "planning_validation": {
            "status": status,
            "model_confidence": 0.82,
            "assumptions": ["material requires confirmation"] if status == "needs_confirmation" else [],
            "unresolved": ["hole position missing"] if status == "blocked" else [],
            "unsupported_operations": [],
            "errors": ([{"code": "hole_position_unresolved", "message": "Hole position is missing."}] if status == "blocked" else []),
            "warnings": [],
        },
        "planning_preview": {
            "features": [{
                "id": "base_01",
                "operation": "base_plate",
                "parameters": {"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
                "depends_on": [],
                "target_body": "body_01",
                "target_reference": None,
                "evidence": ["100x60x10"],
                "assumptions": [],
                "unresolved": [],
            }],
        },
    }


def test_preview_displays_candidate_dimensions_dependencies_and_validation() -> None:
    _application()
    dialog = PlannerPreviewDialog(_summary("needs_confirmation", True))
    try:
        text = dialog.details.toPlainText()
        assert any("mounting_plate" in label.text() for label in dialog.findChildren(QLabel))
        assert "base_01 / base_plate" in text
        assert '"length_mm": 100' in text
        assert "依赖：[]" in text
        assert "material requires confirmation" in text
        assert dialog.confirm_button.isEnabled()
    finally:
        dialog.close()


def test_blocked_preview_disables_execution_button() -> None:
    _application()
    dialog = PlannerPreviewDialog(_summary("blocked", False))
    try:
        assert not dialog.confirm_button.isEnabled()
        assert "hole_position_unresolved" in dialog.details.toPlainText()
        assert dialog.edit_button.text() == "返回修改指令"
        assert dialog.confirm_button.text() == "确认并开始建模"
    finally:
        dialog.close()


def test_preview_uses_high_contrast_dark_local_theme() -> None:
    _application()
    dialog = PlannerPreviewDialog(_summary("approved", True))
    try:
        style = dialog.styleSheet()
        assert dialog.objectName() == "plannerPreviewDialog"
        assert dialog.font().family() == "Microsoft YaHei UI"
        assert dialog.details.objectName() == "plannerPreviewDetails"
        assert dialog.edit_button.objectName() == "plannerEditButton"
        assert dialog.confirm_button.objectName() == "plannerConfirmButton"
        assert "background-color: #151a1d" in style
        assert "background-color: #0d1113" in style
        assert "color: #dbe7e3" in style
        assert "QPushButton#plannerConfirmButton:disabled" in style
    finally:
        dialog.close()
