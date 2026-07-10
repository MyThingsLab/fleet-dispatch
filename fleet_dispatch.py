#!/usr/bin/env python3
"""Fan out ready fleet work across multiple Claude Code accounts.

Reuses myorchestrator's ranking (myorchestrator next --count N) to pick one
distinct candidate per available worker, then runs each in its own git
worktree (mythings.isolation.Workspace) with a headless `claude -p` session
under a different CLAUDE_CONFIG_DIR — so two subscriptions can work the fleet
concurrently without touching each other's files.

Only "issue" candidates are dispatchable today; "scaffold" candidates (a
not-yet-built tool) need MyScaffolder, which doesn't exist yet, so they're
reported and skipped.

Attempts never restart from scratch. Every terminal outcome is durable (the
branch is pushed even on failure, the transcript + a per-issue ledger entry
persist), so when an issue comes back around a later attempt reads what the
prior one did, resumes its branch, and moves forward. If an issue is blocked by
a missing capability in another tool's repo the worker files that as an issue
there and the issue is paused (not failed) until the blocker closes; after
MAX_ATTEMPTS unresolved tries it's handed to a human.

Each run ends at "draft PR opened", promoted to ready-for-review only once its
checklist body and CI both check out — never pushes to main, never merges.
Defaults to --dry-run; pass --execute to actually spawn the headless sessions.

Kill switch: `--abort` touches a HALT marker (.fleet-dispatch/HALT) and exits;
every subsequent --execute run refuses to launch anything until `--clear-halt`
removes it. See README.md's "Kill switch" section for the one-line runbook.

Every headless session is bounded three ways: --max-budget-usd (spend),
--max-turns (turn count), and --session-timeout-s (wall-clock time, in case a
single turn stalls rather than exhausting its turn/budget cap). A timeout is
recorded as a "deferred" outcome -- resumable, not counted toward MAX_ATTEMPTS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mythings.isolation import Workspace
from mythings.ledger import Ledger

from fleet_usage import SAFE_FAMILY_PATTERNS, UsageReport, family_for, parse_transcript
from myorchestrator.candidates import Candidate
from myorchestrator.manifest import default_manifest_path
from myorchestrator.orchestrator import Orchestrator, Recommendation

WORKSPACE_ROOT = Path(__file__).resolve().parent
DISPATCH_LEDGER = WORKSPACE_ROOT / ".fleet-dispatch" / "ledger.jsonl"
ALLOWED_TOOLS_PATH = WORKSPACE_ROOT / ".fleet-dispatch" / "allowed_tools.json"
TRANSCRIPTS_DIR = WORKSPACE_ROOT / ".fleet-dispatch" / "transcripts"
# The kill switch: a marker file, not a signal or a flag a running process has
# to poll mid-loop. `--execute` checks for it before launching anything and
# refuses outright if it's there, so arming it (`--abort`) always beats a run
# that starts after it -- no race between "halt" and "launch". It doesn't
# reach into an already-running headless session (those are already bounded by
# --max-budget-usd/--max-turns and end on their own); it stops the *next* one.
HALT_MARKER = WORKSPACE_ROOT / ".fleet-dispatch" / "HALT"

# Guards the read-modify-write of allowed_tools.json and its commit in
# WORKSPACE_ROOT: concurrent dispatches now run in parallel threads, and two
# threads self-widening the allowlist at once would race on the file and on
# `git commit` (a second commit while one is mid-flight fails on index.lock).
_ALLOWLIST_LOCK = threading.Lock()

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash(git *)",
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(python3 -m pytest*)",
    "Bash(ruff*)",
    "Bash(python -m ruff*)",
    "Bash(python3 -m ruff*)",
    # Read-only inspection: workers reach for these to look around even though
    # they have native Read/Glob/Grep tools; allowing the non-mutating ones up
    # front stops a run from dead-ending on a denied `ls`/`grep` (see the
    # SAFE_FAMILY_PATTERNS note in fleet_usage.py). `rm`/`pip`/`python -c` are
    # intentionally absent — those can mutate or run code and stay friction.
    # `find` also stays off this list even bare/unprefixed: allowedTools is a
    # command-prefix match, and there is no prefix of `find . -name X -delete`
    # (or `-exec ...`) that both matches real read-only usage and excludes the
    # mutating one, so it stays friction like `rm`.
    "Bash(ls*)",
    "Bash(cat*)",
    "Bash(head*)",
    "Bash(tail*)",
    "Bash(wc*)",
    "Bash(grep*)",
    "Bash(pwd*)",
    "Bash(printenv*)",
    "Bash(env)",
    # Setup a worker routinely needs before it can run a repo's own tests: a
    # local venv. `pip install` still isn't on this list, so the worker has to
    # rely on the repo's checked-in dependencies once the venv exists.
    "Bash(python3 -m venv*)",
    "Bash(gh issue view*)",
    "Bash(gh pr create*)",
    # Filing a blocker issue in another tool's repo when this one can't proceed
    # is a first-class move in the resume/recover loop (see _prompt_for's blocker
    # protocol), so the worker needs to create issues, not just view them.
    "Bash(gh issue create*)",
]

# Passed to every worker as `--disallowedTools` so a headless session never
# burns tokens reading (or wanders into) generated / vendored / provenance dirs
# that are irrelevant to closing a code issue. This is the real, supported
# stand-in for a ".claudeignore": Claude Code has no such file, but a Read()
# deny glob is exactly the "don't read what's useless" lever. The worker is
# already filesystem-isolated to one repo's worktree (see Workspace below), so
# these globs only need to hide noise *within* that repo. Deny Edit too: none of
# these are files a worker should be rewriting to close an issue.
DEFAULT_DENY_READS = [
    "Read(**/.venv/**)",
    "Read(**/__pycache__/**)",
    "Read(**/*.pyc)",
    "Read(**/.ruff_cache/**)",
    "Read(**/.pytest_cache/**)",
    "Read(**/.git/**)",
    "Read(**/node_modules/**)",
    "Read(**/dev-ledger/**)",
    "Edit(**/.venv/**)",
    "Edit(**/dev-ledger/**)",
]


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _today_spend_usd(ledger: Ledger, *, today: str | None = None) -> float:
    # UTC calendar day, matching _utc_ts/_utc_now elsewhere in this file — a
    # daily cap that drifted with the host's local timezone would reset at an
    # unpredictable wall-clock hour.
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return sum(
        float(e.data.get("cost_usd", 0.0))
        for e in ledger.read(tool="fleet_dispatch", kind="usage")
        if e.ts.startswith(day)
    )


def _load_allowed_tools() -> list[str]:
    if ALLOWED_TOOLS_PATH.exists():
        return json.loads(ALLOWED_TOOLS_PATH.read_text())
    ALLOWED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_TOOLS_PATH.write_text(json.dumps(DEFAULT_ALLOWED_TOOLS, indent=2))
    return list(DEFAULT_ALLOWED_TOOLS)


def _with_rtk_allowlist(tools: list[str]) -> list[str]:
    # rtk's hook rewrites `git status` -> `rtk git status` (it prepends `rtk `).
    # Verified against rtk 0.43.0: its PreToolUse hook returns `updatedInput`
    # only -- NO `permissionDecision: allow` -- so the rewritten command is NOT
    # self-allowed and must independently satisfy the worker's --allowedTools, or
    # a headless worker stalls on a denied command. The denial auto-widen in
    # _record_usage can't recover it either (it would re-add `Bash(git *)`, not
    # the `rtk`-prefixed form). Mirror each Bash(X) entry with Bash(rtk X) so the
    # compact form is allowed exactly where the original was, never broader.
    mirrored = list(tools)
    for t in tools:
        if t.startswith("Bash(") and t.endswith(")"):
            inner = t[len("Bash(") : -1]
            mirrored.append(f"Bash(rtk {inner})")
    return mirrored


def _save_allowed_tools(tools: list[str], *, commit_message: str) -> None:
    ALLOWED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_TOOLS_PATH.write_text(json.dumps(tools, indent=2))
    # The commit itself *is* the audit trail for a self-edit -- git history
    # replaces the pre-git backup-copy approach, and `git revert` is the way
    # back out if a widened pattern turns out to be wrong. The ledger entry
    # that explains *why* rides along in the same commit.
    #
    # Commit with an explicit pathspec, NOT a bare `git commit`: WORKSPACE_ROOT
    # is a live checkout that may have unrelated staged changes, and a bare
    # commit would sweep them into this self-edit. The pathspec form commits a
    # snapshot of exactly these two files and leaves anything else staged alone.
    subprocess.run(
        ["git", "-C", str(WORKSPACE_ROOT), "add", str(ALLOWED_TOOLS_PATH), str(DISPATCH_LEDGER)],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(WORKSPACE_ROOT), "commit", "-m", commit_message,
            "--", str(ALLOWED_TOOLS_PATH), str(DISPATCH_LEDGER),
        ],
        check=True,
    )


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path


def _parse_accounts(raw: str) -> list[Account]:
    accounts = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        accounts.append(Account(name=f"account{i + 1}", config_dir=Path(entry).expanduser()))
    return accounts


def _config_dir_has_rtk_hook(config_dir: Path) -> bool:
    # rtk installs itself with `rtk init -g` into a CLAUDE_CONFIG_DIR: it writes
    # a PreToolUse hook to settings.json that rewrites commands to their compact
    # `rtk` equivalents. We never write that hook ourselves — rtk owns it, and
    # its schema is versioned — we only read settings.json to confirm a worker
    # spawned under this dir will actually inherit the compression. The hook is
    # self-guarding (exits 0 if rtk/jq is missing), so this check is about
    # "compression is wired", not safety.
    settings = config_dir / "settings.json"
    if not settings.is_file():
        return False
    try:
        data = json.loads(settings.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    hooks = data.get("hooks", {}).get("PreToolUse", [])
    return "rtk" in json.dumps(hooks)


def _preflight_rtk(accounts: list[Account]) -> list[str]:
    # Read-only. Returns human-readable problems; an empty list means rtk
    # compression is correctly wired for every account. Refusing to --execute
    # on a non-empty result is the point: a paid run must never silently skip
    # the compression you asked for.
    problems = []
    if shutil.which("rtk") is None:
        problems.append("`rtk` is not on PATH — install it and run `rtk init -g --hook-only`")
    for account in accounts:
        if not _config_dir_has_rtk_hook(account.config_dir):
            problems.append(
                f"{account.name} ({account.config_dir}) has no rtk PreToolUse hook — "
                f"run `CLAUDE_CONFIG_DIR={account.config_dir} rtk init -g --hook-only`"
            )
    return problems


def _account_uuid(config_dir: Path) -> str | None:
    # The account a config dir is logged into is recorded by `claude auth login`
    # in .claude.json under oauthAccount. Read-only; no token is touched.
    try:
        data = json.loads((config_dir / ".claude.json").read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return (data.get("oauthAccount") or {}).get("accountUuid") or None


def _preflight_distinct_accounts(accounts: list[Account]) -> list[str]:
    # The whole premise is that each config dir is a *different* Claude account,
    # so the workers don't share one session window / usage quota. Two dirs that
    # resolve to the same accountUuid silently void that -- both drain the one
    # account, "concurrency" buys nothing, and one hits its limit twice as fast.
    # This is invisible without checking (they're separate directories with
    # separate tokens), so verify identity before spending anything.
    problems: list[str] = []
    seen: dict[str, str] = {}  # accountUuid -> the first account name that had it
    for account in accounts:
        uuid = _account_uuid(account.config_dir)
        if uuid is None:
            problems.append(
                f"{account.name} ({account.config_dir}): can't read an account identity "
                f"from .claude.json — is it `claude auth login`'d?"
            )
            continue
        if uuid in seen:
            problems.append(
                f"{account.name} ({account.config_dir}) is the SAME Claude account as "
                f"{seen[uuid]} (accountUuid {uuid[:8]}…) — they would share one session "
                f"and quota. Re-auth one to a different account: "
                f"`CLAUDE_CONFIG_DIR={account.config_dir} claude auth login`."
            )
        else:
            seen[uuid] = account.name
    return problems


def _prompt_for(
    candidate: Candidate, prior: Attempt | None = None, *, has_branch: bool = False
) -> str:
    repo, number = candidate.id.split("#")

    resume_block = ""
    if prior is not None:
        # Whether the prior attempt left committed work is decided by an actual
        # pushed branch, not by its outcome name -- a "failed" run that stalled
        # before its first commit (e.g. a session/rate limit) leaves nothing, so
        # promising a branch that isn't there would just confuse the worker.
        resume_block = (
            f"THIS IS A RESUMED ATTEMPT (#{prior.attempt_number + 1}). A previous "
            f"attempt on this issue ended '{prior.outcome}'"
            + (
                ", and you are already checked out on the branch it left behind.\n"
                if has_branch
                else " without leaving any committed work; you are starting from main.\n"
            )
            + (
                f"Its parting message was: {prior.final_message[:400]!r}\n"
                if prior.final_message
                else ""
            )
            + "Do NOT start over. First run `git log --oneline main..HEAD` and "
            "`git diff main...HEAD` to see exactly what the prior attempt already "
            "did and where it got stuck, then continue from there — fix what broke "
            "and finish the issue.\n\n"
        )

    blocker_block = (
        "If this issue turns out to be blocked by a missing capability in ANOTHER "
        "MyThingsLab repo (a contract, helper, or fix that repo must land first), "
        "do not thrash against it. Use `gh issue create --repo MyThingsLab/<repo>` "
        "to file a precise issue describing exactly what that repo must add and "
        "why, then END your run by printing one final line, exactly:\n"
        "  FLEET-DISPATCH-BLOCKED: MyThingsLab/<repo>#<number>\n"
        "naming the issue you just filed. That records the dependency so this issue "
        "is paused, not failed, until the blocker is resolved.\n\n"
    )

    return (
        resume_block
        + f"Work issue #{number} in the {repo} repo (`gh issue view {number} --repo "
        f"MyThingsLab/{repo}` for the full description; title: {candidate.title!r}).\n\n"
        f"You are running fully non-interactively, as a headless `claude -p` "
        f"session: no human is watching and no one can approve a permission "
        f"prompt. If a command is denied, do NOT ask for approval or wait for it — "
        f"it will never come. Work only with the tools you already have, and prefer "
        f"your Read, Edit, Write, Glob and Grep tools over shelling out to `ls`, "
        f"`cat`, `find` or `grep` to inspect the repo.\n\n"
        + blocker_block
        + "Follow this repo's own CLAUDE.md and HARNESS.md exactly. Make the smallest "
        "change that closes the issue, with tests. Do not read or edit generated / "
        "vendored / provenance paths — .venv, __pycache__, .ruff_cache, "
        ".pytest_cache, node_modules, dev-ledger — reads there are blocked and add "
        "nothing. Stay inside this repo; do not touch any other repo in the "
        "workspace.\n\n"
        "Run the repo's full test suite AND its linter, and confirm both pass, "
        "before you finish. Commit your work, then open the pull request as a DRAFT "
        "with `gh pr create --draft`. The PR body MUST contain, verbatim, a line "
        f"`Closes #{number}` and this readiness checklist with every box you have "
        "actually satisfied checked:\n"
        "- [ ] pytest passes\n"
        "- [ ] ruff clean\n"
        "- [ ] change scoped to this repo only\n"
        "Leave the PR as a draft — do NOT mark it ready for review, do NOT push to "
        "main, and do NOT merge it yourself. A separate gate promotes it to ready "
        "once CI is green."
    )


def _dispatch_outcome(n_commits: int, pr_number: int | None) -> tuple[str, str]:
    # Translates what actually landed into an honest ledger outcome. A headless
    # worker exiting 0 is NOT proof it did the work -- it may have given up (e.g.
    # asked for a permission approval no one was there to grant). "success"
    # requires a real commit AND an open PR; anything less says so plainly.
    if n_commits == 0:
        return "no_changes", "worker committed nothing; branch left unpushed"
    if pr_number is None:
        return "needs_review", "committed but no PR was opened; branch pushed for review"
    return "success", f"opened PR #{pr_number}"


def _open_pr_number(org: str, repo: str, branch: str) -> int | None:
    result = subprocess.run(
        [
            "gh", "pr", "list", "--repo", f"{org}/{repo}", "--head", branch,
            "--state", "open", "--json", "number", "--jq", ".[0].number // empty",
        ],
        capture_output=True,
        text=True,
    )
    out = result.stdout.strip()
    return int(out) if out.isdigit() else None


def _branch_name(candidate: Candidate) -> str:
    return f"fleet-dispatch/{candidate.id.replace('#', '-')}"


# --- PR merge-readiness gate -----------------------------------------------
#
# A pushed draft PR is promoted to "ready for review" only when it honours the
# checklist contract from _prompt_for AND its CI actually goes green. Everything
# short of that stays a draft and reports "needs_review" (a resumable outcome),
# so "success" always means a human can pick the PR up to merge. Never merges --
# the human always does that.


def _pr_body(org: str, repo: str, number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", f"{org}/{repo}", "--json", "body", "--jq", ".body"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _pr_body_ok(body: str, issue_number: str) -> tuple[bool, str]:
    # Enforced-checklist half of readiness: the PR must reference the issue it
    # closes and carry a checklist with at least one box the worker actually
    # ticked. A body that skips it means the worker didn't follow the contract.
    low = body.lower()
    if f"closes #{issue_number}" not in low:
        return False, f"body is missing 'Closes #{issue_number}'"
    if "- [x]" not in low:
        return False, "body is missing a checked readiness checklist"
    return True, ""


def _checks_state(org: str, repo: str, number: int) -> str:
    # Collapses gh's per-check buckets into one verdict:
    #   'none'    -> no CI checks are configured/reported (can't verify green)
    #   'fail'    -> at least one check failed or was cancelled
    #   'pending' -> nothing failed yet but something is still running/queued
    #   'pass'    -> every check settled successfully (or was skipped)
    result = subprocess.run(
        ["gh", "pr", "checks", str(number), "--repo", f"{org}/{repo}", "--json", "bucket", "--jq", ".[].bucket"],
        capture_output=True, text=True,
    )
    buckets = [b for b in result.stdout.split() if b]
    if not buckets:
        return "none"
    if any(b in ("fail", "cancel") for b in buckets):
        return "fail"
    if any(b == "pending" for b in buckets):
        return "pending"
    return "pass"


def _wait_for_checks(
    org: str, repo: str, number: int, *, timeout: float, interval: float = 15.0
) -> str:
    # Polls until CI settles or `timeout` seconds elapse. Returns the terminal
    # state ('pass'/'fail'/'none'), or 'pending' if it timed out still running.
    # timeout=0 degenerates to a single check -- the shape unit tests exercise.
    deadline = time.monotonic() + timeout
    while True:
        state = _checks_state(org, repo, number)
        if state != "pending":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "pending"
        time.sleep(min(interval, remaining))


def _promote_pr(org: str, repo: str, number: int) -> None:
    subprocess.run(["gh", "pr", "ready", str(number), "--repo", f"{org}/{repo}"], check=True)


def _finalize_pr(
    org: str, repo: str, issue_number: str, pr_number: int, *, ready_timeout: float
) -> tuple[str, str]:
    # Maps a freshly-pushed draft PR onto the existing outcome vocabulary so the
    # resume/recover router still understands it: "success" ONLY when the body
    # holds AND CI goes green (then it's promoted out of draft); otherwise
    # "needs_review", which is resumable and leaves the draft for a human.
    body_ok, why = _pr_body_ok(_pr_body(org, repo, pr_number), issue_number)
    if not body_ok:
        return "needs_review", f"PR #{pr_number} left draft (not merge-ready): {why}"
    state = _wait_for_checks(org, repo, pr_number, timeout=ready_timeout)
    if state == "pass":
        _promote_pr(org, repo, pr_number)
        return "success", f"PR #{pr_number} promoted to ready for review (CI green)"
    if state == "none":
        return "needs_review", f"PR #{pr_number} left draft: no CI checks to verify green"
    if state == "pending":
        return "needs_review", f"PR #{pr_number} left draft: CI still running after {ready_timeout:.0f}s"
    return "needs_review", f"PR #{pr_number} left draft: CI failing"


# --- resume / recover loop -------------------------------------------------
#
# An attempt on an issue never has to start from scratch. Every terminal
# dispatch outcome is durable (the branch is pushed even on failure, the
# transcript + a per-issue ledger entry persist), so a later attempt can read
# what the prior one did, continue its branch, and move forward -- or, if the
# issue is blocked by a missing capability in another tool's repo, file that
# blocker as an issue there and pause this one instead of thrashing.

MAX_ATTEMPTS = 3
# The worker signals "I filed a blocker issue in another repo; pause this issue
# rather than count it a failure" by printing a final line of exactly this form.
# Agent-owned judgment, machine-readable handoff.
_BLOCKED_SENTINEL = "FLEET-DISPATCH-BLOCKED:"
_TERMINAL_OUTCOMES = frozenset(
    {"success", "needs_review", "no_changes", "failed", "blocked", "needs_human", "deferred"}
)
_RESUMABLE_OUTCOMES = frozenset({"needs_review", "no_changes", "failed", "deferred"})
# Outcomes that count toward MAX_ATTEMPTS. "deferred" (a transient
# infrastructure failure -- session/rate limit, network) is excluded: the issue
# is fine, the fleet just couldn't run right then, so retrying it must not burn
# the budget that escalates a genuinely-stuck issue to a human.
_COUNTED_OUTCOMES = _TERMINAL_OUTCOMES - {"deferred"}
# Substrings that mark a failure as transient/infrastructure rather than a real
# problem with the issue. Matched case-insensitively against the worker's final
# message. Kept deliberately narrow -- only unambiguous capacity/transport
# signals, so a real error is never silently retried forever as "deferred".
_TRANSIENT_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "overloaded",
    "service unavailable",
)


def _is_transient_failure(final_message: str) -> bool:
    low = final_message.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class Attempt:
    candidate_id: str
    outcome: str
    branch: str
    attempt_number: int  # count of terminal attempts so far, this one included
    final_message: str = ""
    blocker: str | None = None  # "<org>/<repo>#<n>" when outcome == "blocked"


def _parse_blocker(final_message: str) -> str | None:
    for line in final_message.splitlines():
        line = line.strip()
        if line.startswith(_BLOCKED_SENTINEL):
            ref = line[len(_BLOCKED_SENTINEL) :].strip()
            return ref or None
    return None


def _last_attempt(ledger: Ledger, candidate_id: str) -> Attempt | None:
    entries = [
        e
        for e in ledger
        if e.tool == "fleet_dispatch"
        and e.kind == "dispatch"
        and e.outcome in _TERMINAL_OUTCOMES
        and e.data.get("candidate") == candidate_id
    ]
    if not entries:
        return None
    last = entries[-1]
    # attempt_number is the count that gates the human-escalation cap, so it
    # excludes transient runs -- a string of rate limits mustn't push a healthy
    # issue to needs_human. That means "deferred" outcomes and, defensively, any
    # "failed" entry whose message reads as transient (e.g. ones recorded before
    # transient classification existed, or by an older build).
    counted = sum(
        1
        for e in entries
        if e.outcome in _COUNTED_OUTCOMES
        and not (
            e.outcome == "failed"
            and _is_transient_failure(e.data.get("final_message", ""))
        )
    )
    return Attempt(
        candidate_id=candidate_id,
        outcome=last.outcome,
        branch=last.data.get("branch", ""),
        attempt_number=counted,
        final_message=last.data.get("final_message", ""),
        blocker=last.data.get("blocker"),
    )


def _dispatch_decision(
    attempt: Attempt | None, blocker_open: bool, max_attempts: int = MAX_ATTEMPTS
) -> str:
    # Pure routing rule: what should happen to an issue given its last attempt?
    # Returns one of: "fresh", "resume", "skip:done", "skip:blocked",
    # "skip:needs_human".
    if attempt is None:
        return "fresh"
    if attempt.outcome == "success":
        return "skip:done"
    if attempt.outcome == "needs_human":
        return "skip:needs_human"
    if attempt.outcome == "blocked":
        return "skip:blocked" if blocker_open else "resume"
    # needs_review / no_changes / failed / deferred -- resumable. The cap uses
    # attempt_number, which already excludes deferred (transient) runs, so those
    # resume indefinitely without ever escalating a healthy issue to a human.
    if attempt.attempt_number >= max_attempts:
        return "skip:needs_human"
    return "resume"


def _issue_is_open(ref: str) -> bool:
    # ref is "<org>/<repo>#<number>", e.g. "MyThingsLab/mythings-core#25".
    if "#" not in ref:
        return False
    repo, number = ref.rsplit("#", 1)
    if not number.isdigit():
        return False
    result = subprocess.run(
        ["gh", "issue", "view", number, "--repo", repo, "--json", "state", "--jq", ".state"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().upper() == "OPEN"


def _remote_branch_exists(repo_path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _record_usage(
    report: UsageReport, *, account: Account, candidate: Candidate, transcript_path: Path,
    ledger: Ledger, rtk: bool = False,
) -> None:
    ledger.record(
        tool="fleet_dispatch",
        kind="usage",
        outcome="success",
        detail=f"{account.name} -> {candidate.id}: ${report.cost_usd:.4f}, "
        f"{report.num_turns} turns, {len(report.denials)} denials",
        candidate=candidate.id,
        account=account.name,
        cost_usd=report.cost_usd,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_creation_input_tokens=report.cache_creation_input_tokens,
        cache_read_input_tokens=report.cache_read_input_tokens,
        num_turns=report.num_turns,
        wasted_output_tokens=report.wasted_output_tokens,
        denials_count=len(report.denials),
        transcript_path=str(transcript_path),
        # Marks whether rtk output compression was active for this run, so
        # rtk-on vs rtk-off `kind=usage` entries can be diffed after the fact --
        # the "measure it, don't assume it" half of the rtk integration.
        rtk=rtk,
    )
    if report.denials:
        print(
            f"  [{account.name}] {len(report.denials)} permission denial(s), "
            f"~{report.wasted_output_tokens} output tokens wasted"
        )

    with _ALLOWLIST_LOCK:
        tools = _load_allowed_tools()
        all_added: list[str] = []
        for d in report.denials:
            family = family_for(d.command) if d.tool_name == "Bash" else None
            if family is None:
                ledger.record(
                    tool="fleet_dispatch",
                    kind="friction",
                    outcome="needs_review",
                    detail=f"unrecognized denied command, no auto-widen: {d.command!r}",
                    candidate=candidate.id,
                    turn=d.turn,
                    preceding_reasoning=d.preceding_reasoning,
                )
                print(f"  [{account.name}] friction (needs human review): {d.command!r}")
                continue
            missing = [p for p in SAFE_FAMILY_PATTERNS[family] if p not in tools]
            if missing:
                tools.extend(missing)
                all_added.extend(missing)
                ledger.record(
                    tool="fleet_dispatch",
                    kind="self_edit",
                    outcome="widened_allowlist",
                    detail=f"auto-widened '{family}' family after a denial: added {missing}",
                    candidate=candidate.id,
                    added=missing,
                    triggering_command=d.command,
                    turn=d.turn,
                    preceding_reasoning=d.preceding_reasoning,
                )
                print(f"  [{account.name}] self-widened allowlist ({family}): +{missing}")
        if all_added:
            _save_allowed_tools(
                tools,
                commit_message=(
                    f"fleet_dispatch: auto-widen allowlist after {candidate.id} denials\n\n"
                    f"Added: {all_added}\n"
                    f"Triggered by {len(report.denials)} permission denial(s) dispatching "
                    f"{account.name} -> {candidate.id}. See .fleet-dispatch/ledger.jsonl "
                    f"(kind=self_edit) for the reasoning behind each addition."
                ),
            )


def _dispatch_one(
    account: Account,
    candidate: Candidate,
    *,
    execute: bool,
    max_budget_usd: float,
    max_turns: int,
    ledger: Ledger,
    org: str,
    prior: Attempt | None = None,
    rtk: bool = False,
    ready_timeout: float = 0.0,
    session_timeout_s: float = 1800.0,
) -> None:
    repo, number = candidate.id.split("#")
    repo_path = WORKSPACE_ROOT / repo
    branch = _branch_name(candidate)
    attempt_number = (prior.attempt_number + 1) if prior is not None else 1

    # Resume from the prior attempt's pushed branch when one exists, so the
    # worker continues that work instead of restarting from main. If the prior
    # attempt left nothing durable (no_changes/transient failure never pushed),
    # fall back to main but still carry its context in the prompt.
    resuming_branch = prior is not None and _remote_branch_exists(repo_path, branch)
    base_ref = f"origin/{branch}" if resuming_branch else "main"
    prompt = _prompt_for(candidate, prior, has_branch=resuming_branch)

    mode = "fresh" if prior is None else f"resume#{attempt_number} from {prior.outcome}"
    # One print call, not several: with dispatches now running concurrently in
    # separate threads, individual print()s from different accounts could
    # otherwise interleave mid-block and produce unreadable output.
    print(
        f"\n=== {account.name} -> {candidate.id} ({repo}) [{mode}] ===\n"
        f"  branch: {branch} (base {base_ref})\n"
        f"  config: {account.config_dir}\n"
        f"  budget cap: ${max_budget_usd}, turn cap: {max_turns}, "
        f"session timeout: {session_timeout_s:.0f}s\n"
        f"  prompt: {prompt}"
    )

    if not execute:
        print("  [dry-run] not launched")
        return

    ledger.record(
        tool="fleet_dispatch",
        kind="dispatch",
        outcome="started",
        detail=f"{account.name} -> {candidate.id} ({mode})",
        candidate=candidate.id,
        account=account.name,
        branch=branch,
        attempt=attempt_number,
    )

    allowed_tools = _load_allowed_tools()
    if rtk:
        allowed_tools = _with_rtk_allowlist(allowed_tools)

    if resuming_branch:
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin", branch], check=True
        )

    with Workspace(repo_path, base_ref=base_ref) as tree:
        # -B, not -b: reset the local branch ref to this worktree's HEAD (the
        # prior branch tip when resuming, else main). A leftover local ref from a
        # prior run's now-removed worktree would otherwise make `checkout -b`
        # crash; any branch with an open PR was already skipped in main().
        subprocess.run(["git", "-C", str(tree), "checkout", "-B", branch], check=True)
        # Snapshot the branch point now, so "did the worker commit anything?" is
        # measured against where it started -- not the `main` ref, which another
        # concurrent dispatch could advance underneath us.
        base_sha = subprocess.run(
            ["git", "-C", str(tree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(account.config_dir)}
        # --max-budget-usd/--max-turns bound the session's spend and turn count,
        # but neither bounds wall-clock time: a single stalled turn (network
        # hang, or the exact "no one can approve a denied command" stall the
        # prompt warns the worker about) would otherwise block this thread
        # forever with no backstop. `timeout=` is that backstop; a timeout is an
        # infrastructure hiccup, not evidence the issue itself is broken, so it
        # is routed to "deferred" below rather than counted as a real failure.
        timed_out = False
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    prompt,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--max-budget-usd",
                    str(max_budget_usd),
                    "--max-turns",
                    str(max_turns),
                    "--disallowedTools",
                    *DEFAULT_DENY_READS,
                    "--allowedTools",
                    *allowed_tools,
                ],
                cwd=tree,
                env=env,
                capture_output=True,
                text=True,
                timeout=session_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            # subprocess.run's TimeoutExpired carries whatever output the
            # session had produced before it was killed -- keep it for the
            # transcript/ledger instead of discarding it, same forensic value
            # as a completed run's output.
            result = subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout=exc.stdout or "", stderr=exc.stderr or "",
            )

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path = TRANSCRIPTS_DIR / f"{branch.replace('/', '_')}-{_utc_ts()}.jsonl"
        transcript_path.write_text(result.stdout)
        report = parse_transcript(result.stdout.splitlines())
        _record_usage(
            report, account=account, candidate=candidate, transcript_path=transcript_path,
            ledger=ledger, rtk=rtk,
        )

        # Count the branch's own commits via merge-base (robust to main
        # advancing under a concurrent dispatch) to judge whether real work
        # exists on the branch at all; count this run's additions separately so
        # a resume that made no progress is visible.
        merge_base = subprocess.run(
            ["git", "-C", str(tree), "merge-base", "main", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        total_commits = int(
            subprocess.run(
                ["git", "-C", str(tree), "rev-list", "--count", f"{merge_base}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            or "0"
        )
        new_commits = int(
            subprocess.run(
                ["git", "-C", str(tree), "rev-list", "--count", f"{base_sha}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            or "0"
        )

        blocker = _parse_blocker(report.final_message)

        # Durability is the whole point of the resume loop: push any real commits
        # so a later attempt can pick them up, even when this run failed or only
        # got partway. (No commits -> nothing to push, and no dead branch left.)
        pushed = False
        pr_number: int | None = None
        if total_commits > 0:
            push = subprocess.run(
                ["git", "-C", str(tree), "push", "-u", "origin", branch],
                capture_output=True, text=True,
            )
            pushed = push.returncode == 0
            if not pushed:
                print(f"  [{account.name}] push failed: {push.stderr.strip()}")

        # An explicit blocker signal wins over everything else: the worker chose
        # to pause on a cross-repo dependency, which is a distinct outcome from
        # failing. Then a non-zero exit, then "committed nothing", then the
        # commit+PR discrimination (success vs needs_review).
        if blocker is not None:
            outcome, msg = "blocked", f"paused on cross-repo blocker {blocker}"
        elif timed_out:
            outcome, msg = (
                "deferred",
                f"deferred (transient): session exceeded {session_timeout_s:.0f}s wall-clock timeout",
            )
        elif result.returncode != 0 and _is_transient_failure(report.final_message):
            # Not the issue's fault -- a session/rate limit or transport blip. Keep
            # it resumable but don't count it toward the human-escalation cap.
            outcome, msg = "deferred", f"deferred (transient): claude exited {result.returncode}"
        elif result.returncode != 0:
            outcome, msg = "failed", f"claude exited {result.returncode}"
        elif total_commits == 0:
            outcome, msg = "no_changes", "worker committed nothing"
        elif not pushed:
            outcome, msg = "failed", "commits present but push failed"
        else:
            pr_number = _open_pr_number(org, repo, branch)
            if pr_number is None:
                outcome, msg = _dispatch_outcome(total_commits, None)
            else:
                outcome, msg = _finalize_pr(
                    org, repo, number, pr_number, ready_timeout=ready_timeout
                )

        note = (
            f" (worker's last words: {report.final_message[:160]!r})"
            if report.final_message and outcome in {"no_changes", "failed", "blocked", "deferred"}
            else ""
        )
        print(
            f"  [{account.name}] {mode}: {outcome} — {msg} "
            f"[{total_commits} commit(s) on branch, +{new_commits} this run]{note}"
        )
        ledger.record(
            tool="fleet_dispatch",
            kind="dispatch",
            outcome=outcome,
            detail=f"{account.name} -> {candidate.id}: {msg}",
            candidate=candidate.id,
            account=account.name,
            branch=branch,
            attempt=attempt_number,
            commits=total_commits,
            new_commits=new_commits,
            pushed=pushed,
            pr_number=pr_number,
            blocker=blocker,
            final_message=report.final_message[:500],
            denials_count=len(report.denials),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        help="comma-separated CLAUDE_CONFIG_DIR paths, one per available worker "
        "(each must already be `claude auth login`'d). Not required with "
        "--abort/--clear-halt.",
    )
    parser.add_argument("--execute", action="store_true", help="actually launch headless sessions")
    halt_group = parser.add_mutually_exclusive_group()
    halt_group.add_argument(
        "--abort",
        action="store_true",
        help="kill switch: touch the HALT marker and exit immediately (no "
        "--accounts needed). Every subsequent --execute run refuses to launch "
        "anything until --clear-halt runs.",
    )
    halt_group.add_argument(
        "--clear-halt",
        action="store_true",
        help="remove the HALT marker and exit immediately, restoring normal "
        "--execute operation.",
    )
    parser.add_argument(
        "--rtk",
        action="store_true",
        help="enable rtk output compression: preflight-verify the rtk hook is "
        "installed in every account's config dir (never installs it — rtk's own "
        "`rtk init -g` owns that), and mirror each Bash(X) allowlist entry with "
        "Bash(rtk X) so the hook's rewritten `rtk <cmd>` commands still pass the "
        "headless worker's --allowedTools",
    )
    parser.add_argument("--org", default="MyThingsLab")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=3.0,
        help="dollar cap passed to each headless claude session (default: $3)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=40,
        help="turn cap passed to each headless claude session (default: 40)",
    )
    parser.add_argument(
        "--max-daily-usd",
        type=float,
        default=20.0,
        help="hard ceiling on total fleet_dispatch spend (all accounts, kind=usage "
        "ledger entries) per UTC calendar day; a run that would push projected "
        "spend over this refuses to launch anything, before spend (default: $20)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for a pushed PR's CI to go green before promoting "
        "it from draft to ready-for-review; on timeout the PR is left a draft "
        "(default: 600). 0 checks once and does not wait.",
    )
    parser.add_argument(
        "--session-timeout-s",
        type=float,
        default=1800.0,
        help="wall-clock seconds to allow a single headless claude session to "
        "run before killing it; complements --max-budget-usd/--max-turns, which "
        "bound spend and turn count but not a stalled/hung session (default: "
        "1800 = 30 min). A timeout is recorded as 'deferred' (transient, "
        "resumable) rather than counted as a real failure.",
    )
    args = parser.parse_args(argv)

    if args.abort:
        HALT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        HALT_MARKER.write_text(f"halted at {_utc_ts()}\n")
        print(
            f"HALT marker armed at {HALT_MARKER} — every subsequent --execute run "
            f"refuses to launch until `python3 fleet_dispatch.py --clear-halt` runs"
        )
        return 0

    if args.clear_halt:
        if HALT_MARKER.exists():
            HALT_MARKER.unlink()
            print(f"HALT marker cleared: {HALT_MARKER}")
        else:
            print("no HALT marker was set")
        return 0

    if not args.accounts:
        parser.error("--accounts must list at least one CLAUDE_CONFIG_DIR")

    accounts = _parse_accounts(args.accounts)
    if not accounts:
        parser.error("--accounts must list at least one CLAUDE_CONFIG_DIR")

    if HALT_MARKER.exists():
        if args.execute:
            print(
                f"refusing to launch: HALT marker present at {HALT_MARKER} (fleet "
                f"kill switch armed). Run `python3 fleet_dispatch.py --clear-halt` "
                f"once it's safe to resume."
            )
            return 1
        print(
            f"note: HALT marker present at {HALT_MARKER} — this dry run still "
            f"reports normally, but --execute would refuse until --clear-halt"
        )

    # A fleet of accounts that are secretly the same account is not a fleet.
    # Always gate on distinct identities -- cheap, local, and it prevents silently
    # draining one account twice (which is exactly what happened once).
    account_problems = _preflight_distinct_accounts(accounts)
    if account_problems:
        print("account preflight failed — the configured accounts are not distinct:")
        for p in account_problems:
            print(f"  - {p}")
        return 1

    if args.rtk:
        problems = _preflight_rtk(accounts)
        if problems:
            print("rtk compression requested (--rtk) but not wired:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("rtk output-compression hook verified for every account")

    orch = Orchestrator(
        org=args.org,
        manifest_path=default_manifest_path(),
        repo_root=WORKSPACE_ROOT,
        ledger=Ledger(WORKSPACE_ROOT / "my-orchestrator" / ".mythings" / "ledger.jsonl"),
    )
    # Overfetch the ranked pool so a worker slot falls through to the next
    # dispatchable candidate instead of sitting idle behind an undispatchable
    # scaffold proposal.
    pool: list[Recommendation] = orch.next_n(max(len(accounts) * 5, 20))
    dispatchable = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind == "issue"]
    skipped = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind != "issue"]

    if skipped:
        names = ", ".join(c.id for c in skipped)
        print(f"skipping (need MyScaffolder, not built yet): {names}")

    # Don't re-dispatch an issue that already has an open fleet-dispatch PR in
    # flight: the orchestrator ranks open issues without knowing one is already
    # being handled, and re-running it just burns an account to open a second,
    # duplicate PR for the same issue.
    in_flight = [
        c for c in dispatchable
        if _open_pr_number(args.org, c.repo, _branch_name(c)) is not None
    ]
    if in_flight:
        ids = {c.id for c in in_flight}
        names = ", ".join(sorted(ids))
        print(f"skipping (already has an open fleet-dispatch PR): {names}")
        dispatchable = [c for c in dispatchable if c.id not in ids]

    dispatch_ledger = Ledger(DISPATCH_LEDGER)

    # Resume/recover routing: read each issue's last attempt and decide whether
    # to start fresh, resume the prior branch, or skip it -- still blocked on a
    # cross-repo dependency, or given up on after MAX_ATTEMPTS tries.
    plan: list[tuple[Candidate, Attempt | None]] = []
    for c in dispatchable:
        prior = _last_attempt(dispatch_ledger, c.id)
        blocker_open = (
            _issue_is_open(prior.blocker)
            if prior is not None and prior.outcome == "blocked" and prior.blocker
            else False
        )
        decision = _dispatch_decision(prior, blocker_open)
        if decision == "skip:done":
            continue
        if decision == "skip:blocked":
            print(f"skipping (blocked on {prior.blocker}, still open): {c.id}")
            continue
        if decision == "skip:needs_human":
            print(f"skipping (needs a human after {prior.attempt_number} attempts): {c.id}")
            # Record it once, so it stays skipped instead of being re-evaluated
            # (and re-counted) every run.
            if prior.outcome != "needs_human":
                dispatch_ledger.record(
                    tool="fleet_dispatch",
                    kind="dispatch",
                    outcome="needs_human",
                    detail=f"{c.id}: gave up after {prior.attempt_number} attempts "
                    f"(last outcome: {prior.outcome})",
                    candidate=c.id,
                    account="-",
                    branch=_branch_name(c),
                    attempt=prior.attempt_number,
                    final_message=prior.final_message[:500],
                )
            continue
        plan.append((c, prior if decision == "resume" else None))

    pairs = list(zip(accounts, plan))

    # Enforced before spend, not after: sum today's actual usage-ledger cost
    # plus the worst case for every session this run is about to launch, and
    # refuse the whole run if that would cross the daily ceiling. A dry run
    # spends nothing, so it's exempt.
    if args.execute and pairs:
        spent_today = _today_spend_usd(dispatch_ledger)
        projected = spent_today + len(pairs) * args.max_budget_usd
        if projected > args.max_daily_usd:
            print(
                f"refusing to launch: today's fleet_dispatch spend is already "
                f"${spent_today:.2f}, and {len(pairs)} more session(s) at up to "
                f"${args.max_budget_usd:.2f} each could reach ${projected:.2f}, "
                f"over the ${args.max_daily_usd:.2f}/day cap (--max-daily-usd). "
                f"Raise --max-daily-usd, lower --max-budget-usd, or wait for the "
                f"UTC day to roll over."
            )
            return 1

    failures: list[tuple[Account, Candidate, BaseException]] = []
    if pairs:
        # One worker thread per account: each already runs in its own git
        # worktree under its own CLAUDE_CONFIG_DIR (mythings.isolation.Workspace),
        # so nothing about running them at the same time needs new isolation --
        # only the shared allowlist self-edit does (see _ALLOWLIST_LOCK).
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            futures = {
                pool.submit(
                    _dispatch_one,
                    account,
                    candidate,
                    execute=args.execute,
                    max_budget_usd=args.max_budget_usd,
                    max_turns=args.max_turns,
                    ledger=dispatch_ledger,
                    org=args.org,
                    prior=prior,
                    rtk=args.rtk,
                    ready_timeout=args.ready_timeout,
                    session_timeout_s=args.session_timeout_s,
                ): (account, candidate)
                for account, (candidate, prior) in pairs
            }
            # future.exception() blocks until that future is done but, unlike
            # future.result(), never raises -- so one account's crash can't
            # stop us from also collecting every other account's outcome.
            for future, (account, candidate) in futures.items():
                exc = future.exception()
                if exc is not None:
                    failures.append((account, candidate, exc))
    for account, candidate, exc in failures:
        print(f"  [{account.name}] {candidate.id} crashed: {exc!r}")
    for account in accounts[len(plan) :]:
        print(f"\n=== {account.name}: no ready issue candidate ===")

    if not args.execute:
        print("\n(dry run — pass --execute to actually launch these sessions)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
