from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import MainWindow


class _DeletedThreadWrapper:
    def isRunning(self) -> bool:
        raise RuntimeError("libshiboken: Internal C++ object already deleted")


class _WindowState:
    _thread_is_running = MainWindow._thread_is_running
    _release_thread_reference = MainWindow._release_thread_reference


def test_deleted_qthread_wrapper_is_released_without_gui_exception() -> None:
    window = _WindowState()
    window.thread = _DeletedThreadWrapper()

    assert window._thread_is_running() is False
    assert window.thread is None
