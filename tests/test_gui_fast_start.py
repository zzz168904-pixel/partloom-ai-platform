from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

import app as gui_app


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_main_window_does_not_probe_solidworks_before_first_paint(monkeypatch, tmp_path: Path) -> None:
    _application()

    def fail_if_called() -> dict:
        raise AssertionError("SolidWorks probe must be deferred until the Qt event loop starts")

    monkeypatch.setattr(gui_app, "probe_solidworks_status", fail_if_called)
    window = gui_app.MainWindow(tmp_path / "gui.log")
    try:
        assert window._status_probe_running is False
    finally:
        window.status_timer.stop()
        window.activity_timer.stop()
        window.close()


def test_status_result_updates_connection_without_running_com(tmp_path: Path) -> None:
    _application()
    window = gui_app.MainWindow(tmp_path / "gui.log")
    try:
        window._status_probe_running = True
        window._apply_solidworks_status(
            {
                "connected": True,
                "info": {"file_name": "fixture.SLDPRT"},
                "elapsed_s": 0.01,
            }
        )
        assert window._status_probe_running is False
        assert window.sw_status_value.text() == "已连接"
        assert window.model_value.text() == "fixture.SLDPRT"
    finally:
        window.status_timer.stop()
        window.activity_timer.stop()
        window.close()
