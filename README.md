# MyThingsLab

A fleet of small, composable `My[X]` tools that develop GitHub repositories as
autonomously as possible — deterministic code for everything except the
handful of steps that genuinely need judgment, where exactly one `Engine` call
is made. Every repo lives under the `MyThingsLab` GitHub org and imports the
shared SDK, [`my-things-core`](my-things-core/).

## The fleet

| Repo | Role | Authority |
|---|---|---|
| [my-things-core](my-things-core/) | SDK: `ledger`, `policy`, `engine`, `github`, `isolation` contracts. | none — every other tool imports it |
| [my-guard](my-guard/) | Rule engine: evaluates an `Action` to allow/ask/deny. | policy for every `git`/`gh` side effect |
| [my-planner](my-planner/) | Priority-ordered, multi-item plan across the whole backlog. | recommends a sequence; never dispatches |
| [my-orchestrator](my-orchestrator/) | Picks the single next unit of work for the next available worker. | decides; never builds, never chains into another tool's CLI |
| *(worker)* | A headless `claude -p` session, dispatched by [`fleet_dispatch.py`](fleet_dispatch.py), closes the picked issue as a PR. | builds |
| [my-tester](my-tester/) | Finds one uncovered unit, opens a PR adding a test for it. | writes code (tests only) |
| [my-changelogger](my-changelogger/) | Folds new `dev-ledger` entries into `CHANGELOG.md`. | writes docs (changelog only) |
| [my-projector](my-projector/) | Reconciles the org Project board + tracking-issue checklist to live repo state. | bookkeeping, no priority judgment |
| [my-reporter](my-reporter/) | Digests the `Ledger` + every repo's `dev-ledger` into a report; can comment it on the tracking issue. | read-only |
| [my-telegram-bot](my-telegram-bot/) | Pushes ledger notifications to Telegram; turns a `Policy` `ASK` into a real human confirmation. | comms only, fail-closed |

The table lists the tools that drive the autonomous cycle; the fleet has more
(`my-docs`, `my-researcher`, `my-todo`, `my-server`, `my-typster`, …) — every
sibling `my-*/` directory is one tool, and the
[fleet docs site](https://mythingslab.github.io) carries a page per tool,
kept in sync by `my-docs`. Each tool's own `README.md`/`CLAUDE.md` is
authoritative for its internals. This page narrates how they chain into one
loop.

## The autonomous cycle

No tool calls another tool's CLI directly — each run is its own
`gh`-attributed, ledger-recorded action, per every tool's own invariants. Two
scripts at this root are the external drivers that chain them:

- **[`fleet_dispatch.py`](fleet_dispatch.py)** — the pick-and-build step:
  imports `Orchestrator` as a library to rank candidates, then fans them out
  across one or more `claude -p` accounts, each in its own git-worktree
  sandbox, with resume/recover across attempts (durable branches, cross-repo
  blocker protocol, `needs_human` after repeated failures).
- **[`fleet_cycle.py`](fleet_cycle.py)** — the full loop, in order:

  1. `myplanner plan` — refresh the recommended sequence (feeds
     `myorchestrator`'s ranking as one more urgency signal).
  2. `fleet_dispatch.py` — `myorchestrator` picks the next unit(s); workers
     close them as PRs.
  3. `mytester run` (per repo) — add coverage for one uncovered unit.
  4. `mychangelogger update` (per repo) — fold new ledger entries into
     `CHANGELOG.md`.
  5. `mydocs sync` — refresh the fleet docs site from each tool's
     `README.md`/`CLAUDE.md` (deterministic hash check; opens, never merges,
     one PR when pages are stale).
  6. `myprojector sync` — reconcile the org Project board + tracking-issue
     checklist.
  7. `myreporter post` — post a fleet-wide digest on the tracking issue.
  8. `mytelegrambot notify` — push everything since the last notify.

  The per-repo steps auto-discover every checkout with a `pyproject.toml`
  (except `my-template`), so a newly scaffolded tool joins the cycle without
  editing the script.

  `fleet_cycle.py --loop` keeps re-running that sequence instead of exiting
  after one pass — meant for an always-on host, not an interactive session.
  Each iteration re-derives the usable account pool
  (`account_usage.select_accounts`, polled on a cadence rather than every
  iteration) and backs off between iterations that dispatch nothing. It's
  meant to be launched as a long-lived process (e.g. a systemd user service)
  with `Restart=on-failure` handling crash recovery, not driven by this
  script's own `--max-duration-min`/`--max-cycle-budget-usd`, which exist for
  bounded manual runs instead.

Every mutating side effect along the way — `git push`, `gh pr create`,
tracking-issue edits — is wrapped as an `Action` routed through `Policy`
(`my-guard`'s `Guard`, or a tool's own default). An `ASK` collapses to `DENY`
unattended (in CI, or with no `my-telegram-bot` wired in); with
`TelegramPolicy` wrapping it, an `ASK` becomes a real Allow/Deny prompt sent
to Telegram and blocks for a reply instead.

## Issue → PR → draft → ready → green → merge

Every worker's PR follows the same shape (`fleet_dispatch.py`'s
`_finalize_pr`): open **draft**, promote to **ready for review** only once
the PR body's readiness checklist holds *and* CI is green, and never merge —
a human always does that last step.

This is now a GitHub-enforced invariant, not just tool discipline: every
shipped repo's `main` has branch protection requiring a PR (no direct or
force pushes) and a green `test` status check before the merge button
unlocks, with no required review count (workers can't approve their own
PRs anyway) and admin/owner bypass enabled for genesis bootstrapping (the
one empty `Initial commit` every new tool pushes straight to `main` before
its first PR exists). So even a manual push or a misbehaving tool can't
land on `main` without going through the same gate the fleet already
enforces on itself.

```bash
# One full cycle, dry-run (default): reports what each step would do, no
# mutating subcommands run and fleet_dispatch never spawns billed sessions.
python3 fleet_cycle.py --accounts ~/.claude-lorenzoliuzzo,~/.claude-mythingslab

# For real: mutating subcommands run, and fleet_dispatch spawns real sessions.
python3 fleet_cycle.py --accounts ~/.claude-lorenzoliuzzo,~/.claude-mythingslab \
  --execute --dispatch-execute
```

`--execute` and `--dispatch-execute` are separate flags on purpose:
`fleet_dispatch`'s sessions are billed API usage, while the rest of the cycle
(tester/changelogger/projector/reporter/telegram) is not — you can run the
bookkeeping half of the loop freely and opt into spawning workers separately.

## Kill switch

To stop `fleet_dispatch.py --execute` from launching anything — right now,
across every account, until you say otherwise:

```bash
python3 fleet_dispatch.py --abort        # arm it: no --accounts needed
python3 fleet_dispatch.py --clear-halt   # disarm it once it's safe to resume
```

`--abort` touches a marker file (`.fleet-dispatch/HALT`); every `--execute` run
checks for it before launching a single session and refuses outright if it's
there (a dry run still reports normally, just with a note). Since `fleet_cycle.py`
shells out to `fleet_dispatch.py` for its dispatch step, arming the marker halts
that path too. It doesn't reach into a session already running — those are
already bounded by `--max-budget-usd`/`--max-turns` and end on their own — it
stops the *next* one, so running `--abort` mid-flight leaves no half-written
state to clean up: whatever already pushed stays pushed, and nothing new starts.

## Provenance

Every tool records its own build history under its `dev-ledger/`
(`python -m mythings._devledger`), and its *runtime* activity (dispatches,
tests written, PRs opened, reports posted, notifications sent) to the shared
`Ledger` each contract reads and writes. `myreporter digest --handoff` renders
either into a resume-context brief for picking work back up in a fresh
session, without re-deriving state from raw git/ledger history.

## Install (development)

Each repo has its own `pyproject.toml`; a shared `.venv` at this root has
every package installed editable:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e "my-things-core[dev]"
for repo in my-*/; do
  [ "$repo" = "my-template/" ] || [ ! -f "$repo/pyproject.toml" ] || \
    pip install -e "${repo%/}[dev]"
done
```

## License

Each repo carries its own MIT license.
