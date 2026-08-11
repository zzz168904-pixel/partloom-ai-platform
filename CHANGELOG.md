# Changelog

## 0.1.0 Beta.1

- Expanded the public documentation with an honest capability matrix,
  architecture guide, CAD-IR guide, source-first installation instructions,
  troubleshooting, and contribution requirements.
- Clarified that the current GitHub release contains source archives and does
  not yet provide a signed prebuilt Windows installer.
- Open-sourced the complete platform core: CAD-IR, validation, planning,
  registries, geometry resolution, Pipeline, GUI, gateway, CAD connectors,
  production executor source, Add-in source, tests, and packaging scripts.
- Added a privacy release gate and replaced historical customer identifiers
  with synthetic fixtures.
- Added 417 passing public regression tests; five hardware/model-dependent
  persistent-reference tests skip in a clean checkout without proprietary CAD
  assets.
- Added GitHub CI, dependency updates, contribution templates, and release
  documentation.

## 0.1.0 Beta

- Introduced the PartLoom AI Platform product identity.
- Added deterministic CAD-IR planning and validation gates.
- Included SolidWorks, AutoCAD, PDF2CAD, and file-conversion connectors.
- Added stage-scoped execution, output guards, reports, and recovery hooks.
- Moved logs and outputs to per-user PartLoom directories.
- Removed private data, hard-coded personal paths, embedded keys, and
  customer-specific PDF fallbacks from the public distribution.
- Added a Windows standalone build and installer workflow.
