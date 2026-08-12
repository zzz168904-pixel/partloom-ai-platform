# Third-Party Notices

PartLoom uses third-party Python packages listed in `pyproject.toml` and
`requirements.txt`. Their licenses remain with their respective copyright holders.

Direct runtime dependencies currently include:

- `pywin32`
- `PySide6`
- `openai-agents`
- `fastapi`
- `uvicorn`
- `httpx`
- `python-dotenv`
- `comtypes`
- `pypdf`
- `pdfplumber`

This file is an inventory, not a replacement for upstream license texts. Release
review must re-check upstream terms when dependencies or redistribution change.

The release does not redistribute:

- SOLIDWORKS or SOLIDWORKS interop assemblies;
- AutoCAD or Autodesk components;
- MinerU model weights;
- customer CAD files or drawings;
- cloud-model API credentials.

Optional CAD applications and OCR/model runtimes must be installed separately
under their own licenses.
