from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH).resolve().parent
hidden_imports = [
    "pythoncom",
    "pywintypes",
    "win32com.client",
    "comtypes",
    "comtypes.client",
]
hidden_imports += collect_submodules("cad_agent")
hidden_imports += collect_submodules(
    "agents",
    filter=lambda name: not name.startswith("agents.voice"),
)
analysis = Analysis(
    [str(root / "app.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "assets" / "weldment_profiles"), "assets/weldment_profiles"),
        (str(root / "examples" / "cad_ir"), "examples/cad_ir"),
        (
            str(root / "src" / "cad_agent" / "engineering_knowledge" / "*.json"),
            "cad_agent/engineering_knowledge",
        ),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PartLoomAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PartLoomAI",
)
