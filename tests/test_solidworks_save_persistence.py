from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from src.cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from src.cad_agent.revolve_skill import RevolveSkill
from src.cad_agent.solidworks_automation_skill import SolidWorksAutomationSkill


class _FakeModel:
    def __init__(self, path: Path) -> None:
        self.path = path

    def GetType(self) -> int:
        return 1

    def GetPathName(self) -> str:
        return str(self.path)

    def ForceRebuild3(self, _top_only: bool) -> bool:
        return True


def _get_com_member(obj, name: str, *args):
    member = getattr(obj, name)
    return member(*args) if callable(member) else member


def _skill(tmp_path: Path, monkeypatch) -> SolidWorksAutomationSkill:
    skill = SolidWorksAutomationSkill(tmp_path)
    monkeypatch.setattr(skill, "_ensure_skill_imports", lambda: None)
    monkeypatch.setattr(skill, "_ensure_non_interactive_com_dependencies", lambda: None)
    return skill


def test_save_active_part_does_not_accept_a_stale_existing_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "motor.SLDPRT"
    target.write_bytes(b"stale")
    model = _FakeModel(target)
    fake_sw_connect = types.SimpleNamespace(
        connect_solidworks=lambda visible=True: (object(), model),
        get_com_member=_get_com_member,
        save_document=lambda _model, _path=None: False,
    )
    monkeypatch.setitem(sys.modules, "sw_connect", fake_sw_connect)
    monkeypatch.setattr(
        RevolveSkill,
        "_verify_reopen",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reopen must not run"))),
    )

    result = _skill(tmp_path, monkeypatch).save_active_part(tmp_path, str(target))

    assert result.success is False
    assert "Failed to save active Part" in result.message


def test_save_active_part_requires_close_reopen_geometry_validation(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "motor.SLDPRT"
    target.write_bytes(b"before")
    model = _FakeModel(target)

    def save_document(_model, _path=None) -> bool:
        target.write_bytes(b"persisted-solid")
        return True

    fake_sw_connect = types.SimpleNamespace(
        connect_solidworks=lambda visible=True: (object(), model),
        get_com_member=_get_com_member,
        save_document=save_document,
    )
    monkeypatch.setitem(sys.modules, "sw_connect", fake_sw_connect)
    monkeypatch.setattr(
        RevolveSkill,
        "_verify_reopen",
        staticmethod(
            lambda _sw, _model, path: {
                "success": True,
                "path": str(path),
                "active_path": str(path),
                "body_count": 1,
                "bbox_m": {"length": 0.295, "width": 0.15, "thickness": 0.138},
                "load_attempts": 1,
                "load_wait_s": 0.0,
                "load_timed_out": False,
                "model": model,
            }
        ),
    )

    result = _skill(tmp_path, monkeypatch).save_active_part(tmp_path, str(target))

    assert result.success is True
    assert result.data["rebuild_succeeded"] is True
    assert result.data["reopen_validation"]["body_count"] == 1
    assert target.read_bytes() == b"persisted-solid"


def test_reopen_body_poll_casts_strict_model_doc_to_part_doc(monkeypatch) -> None:
    class StrictModelDoc:
        _oleobj_ = object()

        def ForceRebuild3(self, _top_only: bool) -> bool:
            return True

    class Body:
        def GetBodyBox(self) -> list[float]:
            return [-0.1, -0.069, -0.069, 0.195, 0.081, 0.069]

        def GetVolume(self) -> float:
            return 0.0023

    class PartDoc:
        def GetBodies2(self, body_type: int, _visible_only: bool):
            return [Body()] if body_type == 0 else []

    import win32com.client

    monkeypatch.setattr(win32com.client, "CastTo", lambda model, interface: PartDoc())

    info = RevolveSkill._wait_for_reopened_body(StrictModelDoc())

    assert info["success"] is True
    assert info["body_count"] == 1
    assert info["load_timed_out"] is False
    assert info["bbox"]["length"] == pytest.approx(0.295)
