from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import BinaryIO


class CADExecutionLock:
    """Serialize SolidWorks/AutoCAD execution across desktop processes."""

    def __init__(self, timeout_s: float | None = None, poll_s: float = 0.2) -> None:
        configured_timeout = os.getenv("CAD_AGENT_EXECUTION_LOCK_TIMEOUT_S", "300")
        self.timeout_s = float(timeout_s if timeout_s is not None else configured_timeout)
        self.poll_s = max(float(poll_s), 0.05)
        local_root = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir())
        self.path = local_root / "PartLoomAI" / "cad_execution.lock"
        self.handle: BinaryIO | None = None
        self.waited_s = 0.0

    def __enter__(self) -> CADExecutionLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self.handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()

        started = time.monotonic()
        while True:
            try:
                self._lock_byte(handle)
                self.handle = handle
                self.waited_s = round(time.monotonic() - started, 3)
                return
            except OSError:
                elapsed = time.monotonic() - started
                if elapsed >= self.timeout_s:
                    handle.close()
                    raise TimeoutError(
                        f"Timed out after {self.timeout_s:.1f}s waiting for the CAD execution lock: {self.path}"
                    )
                time.sleep(self.poll_s)

    def release(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        self.handle = None
        try:
            handle.seek(0)
            self._unlock_byte(handle)
        finally:
            handle.close()

    @staticmethod
    def _lock_byte(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_byte(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
