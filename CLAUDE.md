# MyThingsLab — workspace instructions

This directory holds the MyThingsLab tool fleet: `my-things-core/` (the SDK —
the five contracts plus build tooling) and one sibling repo per `My[X]` tool,
each scaffolded from `my-template/`. The root itself is the `fleet-dispatch`
repo, but its scripts now live in the sibling
[`my-fleet/`](https://github.com/MyThingsLab/my-fleet) repo:
`myfleet.fleet_dispatch` (pick-and-build workers) and `myfleet.fleet_cycle`
(the full autonomous loop), plus the cross-repo test gate, ASK-channel merge
routing, and usage/account monitoring — see `my-fleet/README.md` and
`my-fleet/CLAUDE.md`. This root repo keeps only `dev-ledger/` (its own build
provenance) and `.fleet-dispatch/` (the dispatch loop's runtime ledger dir,
still read/written from the fleet root by `my-fleet`'s scripts). `README.md`
here narrates how the fleet chains together; `TODO.md` is the curated org
backlog.

## Instruction hierarchy

When developing any tool, the most specific instruction wins:

1. That repo's `CLAUDE.md` (purpose, its one Engine call, invariants) and its
   vendored `HARNESS.md` (fixed build rules, drift-checked in CI).
2. This file — cross-cutting workspace facts.
3. `my-things-core/docs/CONVENTIONS.md` — fleet-wide conventions, the
   Rule→Gate enforcement table, and "Starting a new tool".

Canonical homes: the harness lives at `my-things-core/src/mythings/harness.md`
(every repo's `HARNESS.md` is a vendored copy); architecture and provenance are
`my-things-core/docs/ARCHITECTURE.md` and `docs/PROVENANCE.md`.

## Cross-cutting facts

- All repos live under the **`MyThingsLab`** GitHub org, **public**, and are
  kept **entirely isolated from other ventures** (org account, not personal).
- Shared `.venv` at this root has every repo's package installed editable.
- Provenance goes in each repo's `dev-ledger/` (`python -m mythings._devledger`);
  runtime activity goes to the shared `Ledger`.
- To start a new tool, follow "Starting a new tool" in
  `my-things-core/docs/CONVENTIONS.md`.
- `.claude/HANDOFF.md` is the fleet resume brief, regenerated automatically on
  session start — read it before re-deriving state from git/ledger history.

## Session rules (apply to every session and dispatched worker)

- **Branch before commit.** Never commit on a local `main`; check out a branch
  immediately after syncing `main`. Every change lands via PR — `main` is
  branch-protected (PR + green `test` check required) in every shipped repo.
- **Never merge a PR yourself.** Open it, get CI green, mark it ready; a human
  always merges.
- **Never persist secrets.** No tokens on disk, in git, or in the ledger; use
  `gh secret set`. Treat any secret pasted into chat as exposed.
- **Re-check live state before acting.** An external multi-worker dispatcher
  runs against these same repos; issues, PRs, and branches move between
  sessions. `git fetch` and check `gh` before trusting a local checkout.
- **Read a file before editing it, every session.** The Edit/Write tools
  require a prior Read in the *current* session — a file existing on disk
  from an earlier session or worker doesn't satisfy this. Fresh worker
  sessions and post-compaction context both need a fresh Read before any
  Edit/Write, even for files you already know the contents of.
