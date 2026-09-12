# CAD handoff — 2026-09-12

Resume brief for continuing the Continuous Autonomous Development work on
another machine. Written mid-task; the "in flight" section is the part that
does not survive the move.

## The one-paragraph version

The fleet had two independent ways to mark untested worker code verified, and
both are now fixed and on `main`. The merge rule changed from "never merge a PR
yourself" to a four-condition gate, `myfleet.accept` implements it, and the
first real worker corpus ran end-to-end. What remains is mechanical: finish
applying a two-line pytest fix to 24 product repos, and land four open PRs.

## State of play

### Merged today

- `my-things-core#152`, `my-fleet#33`, `#34`, `my-coder#25`, `#26`
- `fleet-dispatch#68` — **the rule change**. `CLAUDE.md` now says "merging is
  gated, not forbidden"
- `my-fleet#35/#36/#38/#39` — `pythonpath`, prose cleanup, `myfleet.accept`,
  the `merge_ready_prs` fix
- 8 kernel fan-out PRs (`my-coder#27`, `my-orchestrator#27`, `my-guard#18`,
  `my-tester#24`, `my-director#11`, `my-reporter#19`, `my-template#22`)
- `my-things-core#153/#154/#155` — the first real worker output, plus the
  Anthropic-credential redaction

### Open, needs a human

| PR | Why it cannot self-merge |
|---|---|
| [my-coder#30](https://github.com/MyThingsLab/my-coder/pull/30) | fixes `--guarded` with no ask channel; carve-out (touches `coder.py`) |
| [my-fleet#42](https://github.com/MyThingsLab/my-fleet/pull/42) | per-repo carve-outs; carve-out (touches `accept.py`) |

Merge `my-fleet#42` **before** relying on the gate again — until it lands, the
gate does not recognise gating code outside my-fleet.

## In flight — does NOT survive the machine move

15 of 24 product repos have the `pythonpath` fix prepared in
`/tmp/.../scratchpad/fan2/`, on branch `chore/test-this-checkouts-source`,
**committed nowhere and pushed nowhere**. That directory is disposable. Do not
hunt for it on the new machine; regenerate.

The script is preserved at `scripts-fanout-pythonpath.py` in this worktree.

```
.venv/bin/python scripts-fanout-pythonpath.py /tmp/fanout <comma,separated,repos>
```

**Done (prepared, unpushed):** my-archivist, my-bibliography, my-glossary,
my-cartographer, my-changelogger, my-dashboard, my-data-analysist, my-embedder,
my-equations, my-flashcards, my-grader, my-idea, my-pipeline, my-planner,
my-professor

**Not started:** my-projector, my-researcher, my-searcher, my-server, my-site,
my-syllabus, my-tables, my-telegram-bot, my-todo

The script only *prepares* — it clones, edits, verifies, and stops. Committing,
pushing, opening the PR and filing the issue are still manual. Each PR needs a
`Closes #N` or the gate returns `needs_human` (see below).

## The four things worth not rediscovering

**1. Do not use my-coder for the fan-out.** It looks like the ideal first
workload and is not: my-coder's verification step is the thing being fixed, so a
worker would certify its own fix with the bug still active.

**2. Every fan-out PR must be proven load-bearing.** With `-o pythonpath=` the
new test has to FAIL. The script enforces this; if you hand-apply, check it.

**3. The bug is invisible to CI by construction.** Actions checks a repo out
fresh and installs it from itself, so there is no competing main checkout to
shadow the worktree. Green CI was never evidence against this. Only a local or
worker run surfaces it.

**4. Run local suites with `PYTHONPATH=$PWD/src`** in any repo whose fix has not
merged yet, or you are reproducing the bug while trying to fix it. This happened
during `my-fleet#36` — three tests failed against a stale main checkout.

## The recurring failure shape

Five instances this week of one pattern: **a failure that renders as a
legitimate negative outcome.**

- `my-fleet#27` — a broken ASK channel is indistinguishable from Deny
- `my-fleet#32` — a skipped required check was indistinguishable from a pass
- `my-fleet#31` — a dead worker session is indistinguishable from an idle fleet
- `my-coder#29` — an unarmed ask channel is indistinguishable from a refusal
- `my-fleet#37` — `merge_ready_prs` had a second, weaker definition of "green"

Rule of thumb that would have caught most: **check the channel, not just the
daemon.** `daemon_is_running()` returned True throughout #29.

## Gate semantics, so you do not re-litigate them

`myfleet.accept` returns ACCEPTED / REJECTED / NEEDS_HUMAN.

- `REJECTED` is reserved for **facts** — a draft is rejected because the author
  said it is not finished.
- An **oversized diff is NEEDS_HUMAN, never REJECTED**. A `size:` label is an
  estimate made before the work existed, so exceeding it is as likely to mean
  the estimate was wrong. The first corpus run rejected two good PRs this way.
- A PR with **no `Closes #N` is NEEDS_HUMAN** — nothing states what it was meant
  to do, so scope is unanswerable. This is why each fan-out PR needs an issue.
- `ledger_entry` and `coverage_delta` are deliberately **not** implemented:
  the first would have failed 30/30 real PRs, the second has no per-PR source.

## Corpus run

Runs on the Pi: `tailscale ssh lollinuxpi@lollinuxpi-server`, checkout
`~/repos/MyThingsLab`, accounts `~/.claude-personal` and `~/.claude-work`.

**Do not pass `--guarded` until `my-coder#30` merges** — it will run the session,
spend the money, and block every PR. That cost ~$3 today.

Verify auth by round-tripping a real prompt, not by checking a config directory
exists:

```
CLAUDE_CONFIG_DIR=~/.claude-work claude -p "reply with exactly: ALIVE"
```

## Next steps, in order

1. Merge `my-fleet#42`, then `my-coder#30`
2. Finish the product fan-out (15 prepared, 9 untouched) — file an issue per
   repo, `Closes #N` in each PR, then merge through the gate
3. Build the `my-fleet#31` auth preflight — an expired session must not be able
   to present as an idle fleet
4. `my-coder#28` — fold session stderr into the outcome detail; `claude exited 1`
   is not a diagnosis
5. Re-run a corpus with `--guarded` working, and let the gate merge it

## Standing constraints

- Never persist secrets; use `gh secret set`. A token pasted into chat is
  exposed.
- Branch before commit; never commit on a local `main`.
- Re-check live state before acting — an external dispatcher moves these repos.
- Read a file before editing it, every session.
