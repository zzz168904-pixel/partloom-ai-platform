from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import sw_connector
from sw_connector import SWConnector


def test_connector_balances_com_apartment_on_disconnect(monkeypatch) -> None:
    calls: list[str] = []
    app = object()
    monkeypatch.setattr(sw_connector.pythoncom, "CoInitialize", lambda: calls.append("init"))
    monkeypatch.setattr(sw_connector.pythoncom, "CoUninitialize", lambda: calls.append("uninit"))
    monkeypatch.setattr(sw_connector.pythoncom, "CoFreeUnusedLibraries", lambda: calls.append("free"))
    monkeypatch.setattr(sw_connector.win32com.client, "GetActiveObject", lambda _progid: app)
    connector = SWConnector()

    assert connector.connect() is app
    assert connector.connect() is app
    connector.disconnect()
    connector.disconnect()

    assert calls == ["init", "free", "uninit", "free"]
    assert connector.app is None


def test_failed_connection_releases_initialized_com_apartment(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sw_connector.pythoncom, "CoInitialize", lambda: calls.append("init"))
    monkeypatch.setattr(sw_connector.pythoncom, "CoUninitialize", lambda: calls.append("uninit"))
    monkeypatch.setattr(sw_connector.pythoncom, "CoFreeUnusedLibraries", lambda: calls.append("free"))

    def fail(_progid: str) -> object:
        raise RuntimeError("not running")

    monkeypatch.setattr(sw_connector.win32com.client, "GetActiveObject", fail)
    connector = SWConnector()

    assert connector.connect(log_failure=False) is None

    assert calls == ["init", "free", "uninit"]
    assert connector.app is None
    assert not connector._com_initialized


def test_typed_com_wrapper_open_and_activate_out_parameters_are_unpacked() -> None:
    model = object()

    class TypedApp:
        def OpenDoc6(self, path, doc_type, options, configuration, errors, warnings):
            assert errors == 0
            assert warnings == 0
            return model, 7, 11

        def ActivateDoc3(self, title, use_preferences, doc_type, errors):
            assert errors == 0
            return model, 13

    connector = SWConnector()
    connector.app = TypedApp()

    opened, open_errors, warnings = connector._open_doc6_compat(Path("part.SLDPRT"), 1)
    activated, activate_errors = connector._activate_doc3_compat("part.SLDPRT", 1)

    assert opened is model
    assert (open_errors, warnings) == (7, 11)
    assert activated is model
    assert activate_errors == 13


def test_dynamic_com_wrapper_falls_back_to_byref_variants(monkeypatch) -> None:
    model = object()

    class DynamicApp:
        def OpenDoc6(self, path, doc_type, options, configuration, errors, warnings):
            if isinstance(errors, int):
                raise TypeError("byref VARIANT required")
            errors.value = 3
            warnings.value = 5
            return model

        def ActivateDoc3(self, title, use_preferences, doc_type, errors):
            if isinstance(errors, int):
                raise TypeError("byref VARIANT required")
            errors.value = 9
            return model

    class FakeVariant:
        def __init__(self) -> None:
            self.value = 0

    connector = SWConnector()
    connector.app = DynamicApp()
    monkeypatch.setattr(connector, "_byref_i4", lambda: FakeVariant())

    opened, open_errors, warnings = connector._open_doc6_compat(Path("part.SLDPRT"), 1)
    activated, activate_errors = connector._activate_doc3_compat("part.SLDPRT", 1)

    assert opened is model
    assert (open_errors, warnings) == (3, 5)
    assert activated is model
    assert activate_errors == 9
