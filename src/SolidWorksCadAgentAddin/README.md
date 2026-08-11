# AI CAD Agent for SOLIDWORKS 2025

This native 64-bit Task Pane is a thin client for the existing Python AI CAD
Agent Gateway. It does not duplicate SolidWorks, AutoCAD, PDF2CAD, planning, or
validation logic.

Build and load without administrator rights:

```powershell
powershell -ExecutionPolicy Bypass -File .\src\SolidWorksCadAgentAddin\Build-Addin.ps1
powershell -ExecutionPolicy Bypass -File .\src\SolidWorksCadAgentAddin\Start-SolidWorks-With-AI-CAD-Agent.ps1
```

To make the add-in appear permanently in `Tools > Add-Ins`, run the clearly
named script once from an administrator PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\src\SolidWorksCadAgentAddin\Register-SolidWorks-AI-CAD-Agent-Admin.ps1
```

The add-in starts the localhost Gateway from the project `.venv`, uses a
per-user token, and never binds to a network interface.
