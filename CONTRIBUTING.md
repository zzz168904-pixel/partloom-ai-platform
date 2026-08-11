# Contributing

Contributions should preserve the deterministic execution boundary:

```text
input -> candidate plan -> CAD-IR -> validator -> confirmation
      -> Pipeline -> CAD connector -> geometry verification -> save gate
```

Requirements:

- Do not add customer files, API keys, logs, or generated CAD outputs.
- Add or change operations through the canonical CAD-IR schema.
- Keep CAD API mutations behind the Pipeline lifecycle.
- Fail closed on missing parameters or ambiguous geometry.
- Add focused tests for routing, validation, and side effects.
- Use synthetic geometry and synthetic CAD-IR in public fixtures.
- Do not bundle vendor binaries or claim vendor certification.
