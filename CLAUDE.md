# MyThingsLab — workspace instructions

This directory holds the MyThingsLab tool line. Repos:

- `mythings-core/` — the SDK: the five contracts plus build tooling. **Canonical
  home** of the build harness (`src/mythings/harness.md`, `docs/CONVENTIONS.md`,
  `docs/ARCHITECTURE.md`, `docs/PROVENANCE.md`).
- `my-guard/` — the rule-engine tool (first My[X]).

When developing any tool, obey that tool's `CLAUDE.md` and its vendored
`HARNESS.md`. Cross-cutting facts that hold everywhere:

- All repos live under the **`MyThingsLab`** GitHub org, **public**, and are kept
  **entirely isolated from other ventures**.
- Shared `.venv` at this root has both packages installed editable.
- Provenance goes in each repo's `dev-ledger/` (`python -m mythings._devledger`).
- To start a new tool, follow "Starting a new tool" in
  `mythings-core/docs/CONVENTIONS.md`.
