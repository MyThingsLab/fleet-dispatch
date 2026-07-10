from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from mythings.ledger import Ledger, LedgerEntry

import fleet_dispatch as fd
from fleet_usage import UsageReport, family_for


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "seed").write_text("seed")
    subprocess.run(["git", "-C", str(path), "add", "seed"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True)


def _account(config_dir: Path, settings: dict | None) -> fd.Account:
    config_dir.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (config_dir / "settings.json").write_text(json.dumps(settings))
    return fd.Account(name="account1", config_dir=config_dir)


_RTK_HOOK = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.claude/hooks/rtk-rewrite.sh"}]}
        ]
    }
}


def test_config_dir_has_rtk_hook_true_when_registered(tmp_path: Path) -> None:
    account = _account(tmp_path, _RTK_HOOK)
    assert fd._config_dir_has_rtk_hook(account.config_dir) is True


def test_config_dir_has_rtk_hook_false_without_hook(tmp_path: Path) -> None:
    account = _account(tmp_path, {"model": "sonnet"})
    assert fd._config_dir_has_rtk_hook(account.config_dir) is False


def test_config_dir_has_rtk_hook_false_when_no_settings_file(tmp_path: Path) -> None:
    assert fd._config_dir_has_rtk_hook(tmp_path) is False


def test_config_dir_has_rtk_hook_false_on_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json")
    assert fd._config_dir_has_rtk_hook(tmp_path) is False


def test_preflight_reports_missing_hook_per_account(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd.shutil, "which", lambda _: "/usr/bin/rtk")
    good = _account(tmp_path / "a", _RTK_HOOK)
    bad = _account(tmp_path / "b", {"model": "sonnet"})

    problems = fd._preflight_rtk([good, bad])

    assert len(problems) == 1
    assert str(bad.config_dir) in problems[0]


def test_preflight_reports_rtk_not_on_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd.shutil, "which", lambda _: None)
    good = _account(tmp_path, _RTK_HOOK)

    problems = fd._preflight_rtk([good])

    assert any("not on PATH" in p for p in problems)


def test_preflight_clean_when_all_wired(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd.shutil, "which", lambda _: "/usr/bin/rtk")
    account = _account(tmp_path, _RTK_HOOK)

    assert fd._preflight_rtk([account]) == []


def _account_with_uuid(config_dir: Path, uuid: str | None) -> fd.Account:
    config_dir.mkdir(parents=True, exist_ok=True)
    if uuid is not None:
        (config_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": "x@y"}})
        )
    return fd.Account(name=config_dir.name, config_dir=config_dir)


def test_account_uuid_reads_or_none(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-123")
    assert fd._account_uuid(a.config_dir) == "uuid-123"
    assert fd._account_uuid(tmp_path / "missing") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / ".claude.json").write_text("{not json")
    assert fd._account_uuid(tmp_path / "bad") is None


def test_preflight_distinct_accounts_flags_same_account(tmp_path: Path) -> None:
    # The exact footgun: two config dirs logged into the same account.
    a = _account_with_uuid(tmp_path / "a", "same-uuid")
    b = _account_with_uuid(tmp_path / "b", "same-uuid")
    problems = fd._preflight_distinct_accounts([a, b])
    assert len(problems) == 1
    assert "SAME Claude account" in problems[0]


def test_preflight_distinct_accounts_clean_when_different(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-a")
    b = _account_with_uuid(tmp_path / "b", "uuid-b")
    assert fd._preflight_distinct_accounts([a, b]) == []


def test_preflight_distinct_accounts_flags_unreadable_identity(tmp_path: Path) -> None:
    a = _account_with_uuid(tmp_path / "a", "uuid-a")
    b = _account_with_uuid(tmp_path / "b", None)  # no .claude.json
    problems = fd._preflight_distinct_accounts([a, b])
    assert len(problems) == 1
    assert "can't read an account identity" in problems[0]


def test_with_rtk_allowlist_mirrors_bash_entries_only() -> None:
    tools = ["Read", "Edit", "Bash(git *)", "Bash(pytest*)"]

    mirrored = fd._with_rtk_allowlist(tools)

    # Original entries preserved, non-Bash entries not mirrored.
    assert mirrored[: len(tools)] == tools
    assert "Bash(rtk git *)" in mirrored
    assert "Bash(rtk pytest*)" in mirrored
    assert "Bash(rtk Read)" not in mirrored
    assert "rtk Edit" not in " ".join(mirrored)


def test_with_rtk_allowlist_rewritten_command_would_match() -> None:
    # `git status` -> rtk rewrites to `rtk git status`; the mirrored pattern
    # Bash(rtk git *) is what makes that pass the allowlist.
    mirrored = fd._with_rtk_allowlist(["Bash(git *)"])
    assert "Bash(rtk git *)" in mirrored


@pytest.mark.parametrize("rtk", [True, False])
def test_record_usage_marks_whether_rtk_was_active(tmp_path: Path, rtk: bool) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    report = UsageReport(cost_usd=0.01, input_tokens=100, output_tokens=20, num_turns=1)
    account = fd.Account(name="account1", config_dir=tmp_path)
    candidate = fd.Candidate(
        id="myrepo#1", repo="myrepo", tool="", title="t", kind="issue", created_at=""
    )

    fd._record_usage(
        report,
        account=account,
        candidate=candidate,
        transcript_path=tmp_path / "t.jsonl",
        ledger=ledger,
        rtk=rtk,
    )

    (entry,) = [e for e in ledger.read() if e.kind == "usage"]
    assert entry.data["rtk"] is rtk
    assert entry.data["input_tokens"] == 100


def test_main_dispatches_accounts_concurrently(tmp_path: Path, monkeypatch) -> None:
    # Regression test for the bug this fix closes: main()'s dispatch loop used
    # to call _dispatch_one sequentially, so two accounts' work never
    # overlapped in time. Stub _dispatch_one to block for a bit and record its
    # own [start, end) window; if the loop is truly concurrent the two
    # accounts' windows overlap, if it's sequential they can't.
    calls: list[tuple[str, float, float]] = []
    calls_lock = threading.Lock()

    def fake_dispatch_one(
        account, candidate, *, execute, max_budget_usd, max_turns, ledger, org, prior=None, rtk=False, ready_timeout=0.0
    ):
        start = time.monotonic()
        time.sleep(0.2)
        end = time.monotonic()
        with calls_lock:
            calls.append((account.name, start, end))

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"),
        fd.Candidate(id="repo#2", repo="repo", tool="", title="t2", kind="issue", created_at="2020-01-02"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    assert rc == 0
    assert len(calls) == 2
    (_name1, start1, end1), (_name2, start2, end2) = calls
    assert start1 < end2 and start2 < end1


def test_main_surfaces_every_account_failure_not_just_first(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Regression test: future.result() in a loop raises on the first failing
    # future and unwinds before the loop reaches the second, silently
    # dropping any other account's crash. Both accounts fail here on purpose;
    # both should still be reported.
    def fake_dispatch_one(
        account, candidate, *, execute, max_budget_usd, max_turns, ledger, org, prior=None, rtk=False, ready_timeout=0.0
    ):
        raise RuntimeError(f"boom-{account.name}")

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"),
        fd.Candidate(id="repo#2", repo="repo", tool="", title="t2", kind="issue", created_at="2020-01-02"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "boom-account1" in out
    assert "boom-account2" in out


def test_main_skips_issue_with_open_pr_in_flight(tmp_path: Path, monkeypatch) -> None:
    # An issue that already has an open fleet-dispatch PR must not be handed to
    # an account again -- otherwise a second, duplicate PR gets opened for it.
    dispatched: list[str] = []

    def fake_dispatch_one(
        account, candidate, *, execute, max_budget_usd, max_turns, ledger, org, prior=None, rtk=False, ready_timeout=0.0
    ):
        dispatched.append(candidate.id)

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="done", kind="issue", created_at="2020-01-01"),
        fd.Candidate(id="repo#2", repo="repo", tool="", title="todo", kind="issue", created_at="2020-01-02"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    # repo#1's branch already has an open PR (#99); repo#2's does not.
    monkeypatch.setattr(
        fd,
        "_open_pr_number",
        lambda org, repo, branch: 99 if branch == fd._branch_name(candidates[0]) else None,
    )

    rc = fd.main(["--accounts", f"{tmp_path / 'a'}"])

    assert rc == 0
    # The single account should get repo#2 (todo), never the in-flight repo#1.
    assert dispatched == ["repo#2"]


# --- A: honest success detection -------------------------------------------


def test_dispatch_outcome_no_commits_is_not_success() -> None:
    outcome, msg = fd._dispatch_outcome(0, None)
    assert outcome == "no_changes"
    assert "committed nothing" in msg


def test_dispatch_outcome_commits_without_pr_needs_review() -> None:
    outcome, msg = fd._dispatch_outcome(2, None)
    assert outcome == "needs_review"
    assert "no PR" in msg


def test_dispatch_outcome_commits_and_pr_is_success() -> None:
    outcome, msg = fd._dispatch_outcome(3, 22)
    assert outcome == "success"
    assert "#22" in msg


# --- B: read-only shell recognised, mutation stays friction ----------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la docs/tools/", "ls"),
        ("rtk ls docs/tools/", "ls"),
        ("cat README.md", "cat"),
        ("grep -rn foo .", "grep"),
        ("rtk grep -rl bar .", "grep"),
        ("head -20 f.py", "head"),
        ("git status", "git"),
        ("rtk git status", "git"),  # rtk-prefixed git now classifies correctly
        ("gh pr view 1", "gh"),
        ("python -m pytest -q", "pytest"),
        # Mutating / code-running commands must NOT be recognised -> friction.
        ("rm conftest.py", None),
        ("pip install -e .", None),
        ("python -c 'import mythings'", None),
        ("find . -delete", None),
    ],
)
def test_family_for_readonly_vs_mutation(command: str, expected: str | None) -> None:
    assert family_for(command) == expected


def test_default_allowed_tools_has_readonly_not_mutation() -> None:
    assert "Bash(ls*)" in fd.DEFAULT_ALLOWED_TOOLS
    assert "Bash(grep*)" in fd.DEFAULT_ALLOWED_TOOLS
    # Never proactively allow mutation/code-execution.
    assert "Bash(rm*)" not in fd.DEFAULT_ALLOWED_TOOLS
    assert "Bash(find*)" not in fd.DEFAULT_ALLOWED_TOOLS


# --- C: prompt tells the worker it is non-interactive ----------------------


def test_prompt_is_noninteractive_and_prefers_native_tools() -> None:
    candidate = fd.Candidate(
        id="myrepo#7", repo="myrepo", tool="", title="t", kind="issue", created_at=""
    )
    prompt = fd._prompt_for(candidate)
    assert "non-interactively" in prompt
    assert "will never come" in prompt
    assert "Read" in prompt


def test_save_allowed_tools_commit_ignores_unrelated_staged_changes(
    tmp_path: Path, monkeypatch
) -> None:
    # The self-edit commit runs in a live checkout that may have other staged
    # changes; it must commit ONLY allowed_tools.json + ledger, never sweep an
    # unrelated staged file into the auto-widen commit.
    _init_git_repo(tmp_path)
    monkeypatch.setattr(fd, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(fd, "ALLOWED_TOOLS_PATH", tmp_path / ".fleet-dispatch" / "allowed_tools.json")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / ".fleet-dispatch" / "ledger.jsonl")
    fd.DISPATCH_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    fd.DISPATCH_LEDGER.write_text("{}\n")

    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "unrelated.py"], check=True)

    fd._save_allowed_tools(["Read", "Bash(ls*)"], commit_message="widen")

    committed = subprocess.run(
        ["git", "-C", str(tmp_path), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert ".fleet-dispatch/allowed_tools.json" in committed
    assert "unrelated.py" not in committed
    # The unrelated file stays staged, uncommitted -- untouched by the self-edit.
    still_staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "unrelated.py" in still_staged


# --- resume / recover loop -------------------------------------------------


def test_parse_blocker_extracts_ref_else_none() -> None:
    assert fd._parse_blocker("done\nFLEET-DISPATCH-BLOCKED: MyThingsLab/core#9") == "MyThingsLab/core#9"
    assert fd._parse_blocker("  FLEET-DISPATCH-BLOCKED: org/repo#12  ") == "org/repo#12"
    assert fd._parse_blocker("no marker here") is None
    assert fd._parse_blocker("FLEET-DISPATCH-BLOCKED:") is None


def test_default_allowed_tools_can_create_issues_for_blockers() -> None:
    # Filing a cross-repo blocker issue is part of the loop -> gh issue create.
    assert "Bash(gh issue create*)" in fd.DEFAULT_ALLOWED_TOOLS


@pytest.mark.parametrize(
    ("attempt", "blocker_open", "expected"),
    [
        (None, False, "fresh"),
        (fd.Attempt("i#1", "success", "b", 1), False, "skip:done"),
        (fd.Attempt("i#1", "needs_human", "b", 3), False, "skip:needs_human"),
        (fd.Attempt("i#1", "blocked", "b", 1, blocker="o/r#2"), True, "skip:blocked"),
        (fd.Attempt("i#1", "blocked", "b", 1, blocker="o/r#2"), False, "resume"),
        (fd.Attempt("i#1", "needs_review", "b", 1), False, "resume"),
        (fd.Attempt("i#1", "no_changes", "b", 2), False, "resume"),
        (fd.Attempt("i#1", "failed", "b", 3), False, "skip:needs_human"),  # hit the cap
    ],
)
def test_dispatch_decision(attempt, blocker_open: bool, expected: str) -> None:
    assert fd._dispatch_decision(attempt, blocker_open, max_attempts=3) == expected


def test_last_attempt_reads_latest_terminal_and_counts_attempts(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.jsonl")
    led.record("fleet_dispatch", "dispatch", "started", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "no_changes", candidate="r#1", branch="b",
               final_message="stuck on ls")
    led.record("fleet_dispatch", "dispatch", "started", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "needs_review", candidate="r#1", branch="b", commits=1)
    led.record("fleet_dispatch", "dispatch", "success", candidate="other#2", branch="b2")

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.outcome == "needs_review"
    assert a.attempt_number == 2  # two terminal entries; "started" doesn't count
    assert a.branch == "b"
    assert fd._last_attempt(led, "nope#9") is None


def test_resume_prompt_carries_prior_context_and_blocker_protocol() -> None:
    candidate = fd.Candidate(id="r#1", repo="r", tool="", title="t", kind="issue", created_at="")
    prior = fd.Attempt("r#1", "needs_review", "b", 1, final_message="got halfway")
    prompt = fd._prompt_for(candidate, prior)
    assert "RESUMED ATTEMPT" in prompt
    assert "Do NOT start over" in prompt
    assert "got halfway" in prompt
    # Blocker protocol present on every prompt (fresh too).
    assert "FLEET-DISPATCH-BLOCKED:" in fd._prompt_for(candidate)


def test_resume_prompt_wording_matches_whether_a_branch_exists() -> None:
    candidate = fd.Candidate(id="r#1", repo="r", tool="", title="t", kind="issue", created_at="")
    prior = fd.Attempt("r#1", "failed", "b", 1)
    with_branch = fd._prompt_for(candidate, prior, has_branch=True)
    without_branch = fd._prompt_for(candidate, prior, has_branch=False)
    assert "branch it left behind" in with_branch
    # A failed run that left no commits (e.g. a session limit) must not promise a
    # branch that isn't there.
    assert "branch it left behind" not in without_branch
    assert "starting from main" in without_branch


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("You've hit your session limit · resets 6pm", True),
        ("Error: usage limit reached", True),
        ("overloaded_error: server busy", True),
        ("Traceback: AssertionError in test_foo", False),
        ("could not find the file", False),
    ],
)
def test_is_transient_failure(message: str, expected: bool) -> None:
    assert fd._is_transient_failure(message) is expected


def test_transient_failures_do_not_count_toward_attempt_cap(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "l.jsonl")
    # Two transient (deferred) runs and one real failure.
    led.record("fleet_dispatch", "dispatch", "deferred", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "failed", candidate="r#1", branch="b")
    led.record("fleet_dispatch", "dispatch", "deferred", candidate="r#1", branch="b")

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.outcome == "deferred"  # latest
    assert a.attempt_number == 1  # only the real "failed" counts, not the two deferred


def test_failed_entry_with_transient_message_does_not_count(tmp_path: Path) -> None:
    # Defends against "failed" entries recorded before transient classification
    # existed (exactly the two rate-limited #17 runs in the live ledger): a
    # failure whose message is transient must not count toward the cap.
    led = Ledger(tmp_path / "l.jsonl")
    led.record("fleet_dispatch", "dispatch", "failed", candidate="r#1", branch="b",
               final_message="You've hit your session limit · resets 6pm")
    led.record("fleet_dispatch", "dispatch", "failed", candidate="r#1", branch="b",
               final_message="You've hit your session limit · resets 6pm")

    a = fd._last_attempt(led, "r#1")
    assert a is not None
    assert a.attempt_number == 0  # both transient -> neither counts


def test_dispatch_decision_deferred_always_resumes() -> None:
    # Even a long string of transient deferrals never escalates to a human,
    # because attempt_number excludes them (here it's 0).
    deferred = fd.Attempt("r#1", "deferred", "b", 0)
    assert fd._dispatch_decision(deferred, blocker_open=False, max_attempts=3) == "resume"


def test_main_resumes_or_skips_by_prior_attempt(tmp_path: Path, monkeypatch) -> None:
    got: dict[str, object] = {}

    def fake_dispatch_one(
        account, candidate, *, execute, max_budget_usd, max_turns, ledger, org, prior=None, rtk=False, ready_timeout=0.0
    ):
        got[candidate.id] = prior

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")

    candidates = [
        fd.Candidate(id="r#1", repo="r", tool="", title="resume", kind="issue", created_at="2020-01-01"),
        fd.Candidate(id="r#2", repo="r", tool="", title="blocked", kind="issue", created_at="2020-01-02"),
        fd.Candidate(id="r#3", repo="r", tool="", title="capped", kind="issue", created_at="2020-01-03"),
        fd.Candidate(id="r#4", repo="r", tool="", title="fresh", kind="issue", created_at="2020-01-04"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])

    attempts = {
        "r#1": fd.Attempt("r#1", "needs_review", "b1", 1),
        "r#2": fd.Attempt("r#2", "blocked", "b2", 1, blocker="MyThingsLab/core#9"),
        "r#3": fd.Attempt("r#3", "failed", "b3", 3),  # at the attempt cap
    }
    monkeypatch.setattr(fd, "_last_attempt", lambda ledger, cid: attempts.get(cid))
    monkeypatch.setattr(fd, "_issue_is_open", lambda ref: True)  # r#2's blocker still open

    rc = fd.main(["--accounts", f"{tmp_path / 'a'},{tmp_path / 'b'}"])

    assert rc == 0
    # r#2 (blocked, still open) and r#3 (hit cap) skipped; r#1 resumed, r#4 fresh.
    assert set(got) == {"r#1", "r#4"}
    assert got["r#1"] is attempts["r#1"]  # resumed with its prior attempt
    assert got["r#4"] is None  # fresh
    # r#3 hitting the cap is recorded as needs_human so it stays skipped.
    outcomes = [e.outcome for e in Ledger(tmp_path / "ledger.jsonl") if e.data.get("candidate") == "r#3"]
    assert "needs_human" in outcomes


# --- deny-reads shrink what a worker may read ------------------------------


def test_default_deny_reads_cover_noise_dirs_not_source() -> None:
    joined = " ".join(fd.DEFAULT_DENY_READS)
    assert "Read(**/.venv/**)" in fd.DEFAULT_DENY_READS
    assert "Read(**/__pycache__/**)" in fd.DEFAULT_DENY_READS
    assert "Read(**/dev-ledger/**)" in fd.DEFAULT_DENY_READS
    # Source and tests must never be denied -- the worker needs to read them.
    assert "src" not in joined
    assert "tests" not in joined


def test_prompt_requires_draft_pr_and_checklist() -> None:
    candidate = fd.Candidate(
        id="myrepo#7", repo="myrepo", tool="", title="t", kind="issue", created_at=""
    )
    prompt = fd._prompt_for(candidate)
    assert "--draft" in prompt
    assert "Closes #7" in prompt
    assert "do NOT mark it ready" in prompt


# --- PR merge-readiness: draft promoted only on checklist + green CI --------


def test_pr_body_ok_requires_closes_and_checked_box() -> None:
    ok, _ = fd._pr_body_ok("Closes #7\n- [x] pytest passes", "7")
    assert ok is True


def test_pr_body_ok_rejects_missing_closes() -> None:
    ok, why = fd._pr_body_ok("- [x] pytest passes", "7")
    assert ok is False
    assert "Closes #7" in why


def test_pr_body_ok_rejects_unchecked_checklist() -> None:
    ok, why = fd._pr_body_ok("Closes #7\n- [ ] pytest passes", "7")
    assert ok is False
    assert "checklist" in why


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        ("", "none"),
        ("pass\npass", "pass"),
        ("pass\nskipping", "pass"),
        ("pass\npending", "pending"),
        ("pass\nfail", "fail"),
        ("cancel", "fail"),
    ],
)
def test_checks_state_collapses_buckets(monkeypatch, buckets: str, expected: str) -> None:
    monkeypatch.setattr(
        fd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=buckets, stderr=""),
    )
    assert fd._checks_state("org", "repo", 1) == expected


def test_wait_for_checks_returns_pending_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_checks_state", lambda *a, **k: "pending")
    # timeout=0 -> a single check, no sleeping; still-running stays 'pending'.
    assert fd._wait_for_checks("org", "repo", 1, timeout=0) == "pending"


def test_finalize_pr_promotes_and_succeeds_when_body_ok_and_ci_green(monkeypatch) -> None:
    promoted: list[int] = []
    monkeypatch.setattr(fd, "_pr_body", lambda *a, **k: "Closes #7\n- [x] pytest passes")
    monkeypatch.setattr(fd, "_wait_for_checks", lambda *a, **k: "pass")
    monkeypatch.setattr(fd, "_promote_pr", lambda org, repo, n: promoted.append(n))

    outcome, _ = fd._finalize_pr("org", "repo", "7", 42, ready_timeout=0)

    # Success -- the only path that maps to a mergeable, promoted PR.
    assert outcome == "success"
    assert promoted == [42]


def test_finalize_pr_needs_review_and_stays_draft_when_ci_fails(monkeypatch) -> None:
    promoted: list[int] = []
    monkeypatch.setattr(fd, "_pr_body", lambda *a, **k: "Closes #7\n- [x] pytest passes")
    monkeypatch.setattr(fd, "_wait_for_checks", lambda *a, **k: "fail")
    monkeypatch.setattr(fd, "_promote_pr", lambda org, repo, n: promoted.append(n))

    outcome, msg = fd._finalize_pr("org", "repo", "7", 42, ready_timeout=0)

    assert outcome == "needs_review"
    assert "CI failing" in msg
    assert promoted == []


def test_finalize_pr_needs_review_when_body_incomplete(monkeypatch) -> None:
    promoted: list[int] = []
    monkeypatch.setattr(fd, "_pr_body", lambda *a, **k: "no closes line here")
    monkeypatch.setattr(fd, "_wait_for_checks", lambda *a, **k: "pass")
    monkeypatch.setattr(fd, "_promote_pr", lambda org, repo, n: promoted.append(n))

    outcome, _ = fd._finalize_pr("org", "repo", "7", 42, ready_timeout=0)

    assert outcome == "needs_review"
    assert promoted == []  # a green CI never promotes a PR whose body is incomplete


def test_finalize_pr_needs_review_when_no_ci_checks(monkeypatch) -> None:
    monkeypatch.setattr(fd, "_pr_body", lambda *a, **k: "Closes #7\n- [x] pytest passes")
    monkeypatch.setattr(fd, "_wait_for_checks", lambda *a, **k: "none")

    outcome, _ = fd._finalize_pr("org", "repo", "7", 42, ready_timeout=0)

    assert outcome == "needs_review"


def test_abort_arms_halt_marker_without_needing_accounts(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "HALT"
    monkeypatch.setattr(fd, "HALT_MARKER", marker)

    rc = fd.main(["--abort"])

    assert rc == 0
    assert marker.exists()


def test_clear_halt_removes_marker(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "HALT"
    marker.write_text("halted at some point\n")
    monkeypatch.setattr(fd, "HALT_MARKER", marker)

    rc = fd.main(["--clear-halt"])

    assert rc == 0
    assert not marker.exists()


def test_clear_halt_when_not_set_is_a_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")

    rc = fd.main(["--clear-halt"])

    assert rc == 0


def test_abort_and_clear_halt_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        fd.main(["--abort", "--clear-halt"])


def _dispatch_stub(calls: list) -> callable:
    def fake_dispatch_one(*a, **k):
        calls.append(k)

    return fake_dispatch_one


def _wire_single_candidate_orchestrator(monkeypatch) -> None:
    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)


def test_main_refuses_execute_when_halted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    (tmp_path / "HALT").write_text("halted\n")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a"), "--execute"])

    assert rc == 1
    assert calls == [], "the kill switch must stop launch before any session starts"


def test_main_dry_run_still_reports_when_halted(tmp_path: Path, monkeypatch, capsys) -> None:
    # A dry run spends nothing, so the marker is informational there, not a
    # block -- symmetric with how --max-daily-usd treats dry runs.
    monkeypatch.setattr(fd, "HALT_MARKER", tmp_path / "HALT")
    (tmp_path / "HALT").write_text("halted\n")
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    calls: list = []
    monkeypatch.setattr(fd, "_dispatch_one", _dispatch_stub(calls))
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "HALT marker present" in out
    assert len(calls) == 1  # dry-run _dispatch_one still runs; it just prints and returns


def _usage_entry(ledger: Ledger, *, cost_usd: float, ts: str) -> None:
    ledger.append(
        LedgerEntry(tool="fleet_dispatch", kind="usage", outcome="success", ts=ts, data={"cost_usd": cost_usd})
    )


def test_today_spend_usd_sums_only_todays_usage_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    _usage_entry(ledger, cost_usd=1.5, ts="2026-07-10T01:00:00Z")
    _usage_entry(ledger, cost_usd=2.25, ts="2026-07-10T02:00:00Z")
    _usage_entry(ledger, cost_usd=99.0, ts="2026-07-09T23:59:59Z")  # yesterday, excluded

    assert fd._today_spend_usd(ledger, today="2026-07-10") == pytest.approx(3.75)


def test_today_spend_usd_ignores_non_usage_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record(tool="fleet_dispatch", kind="dispatch", outcome="success", cost_usd=50.0)

    assert fd._today_spend_usd(ledger, today="2026-07-10") == 0.0


def test_main_refuses_to_dispatch_over_daily_cap(tmp_path: Path, monkeypatch) -> None:
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=19.0, ts=f"{today}T00:00:00Z")

    called = False

    def fake_dispatch_one(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(
        [
            "--accounts", str(tmp_path / "a"),
            "--execute",
            "--max-budget-usd", "3.0",
            "--max-daily-usd", "20.0",
        ]
    )

    assert rc == 1
    assert called is False, "must refuse before spend, not launch and hope"


def test_main_dry_run_ignores_daily_cap(tmp_path: Path, monkeypatch) -> None:
    # A dry run spends nothing, so it must not be blocked by the cap -- the
    # whole point of --dry-run is to report safely regardless of budget state.
    dispatch_ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", dispatch_ledger_path)
    ledger = Ledger(dispatch_ledger_path)
    today = datetime.now(fd.UTC).strftime("%Y-%m-%d")
    _usage_entry(ledger, cost_usd=999.0, ts=f"{today}T00:00:00Z")

    def fake_dispatch_one(*a, **k):
        pass

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)
    monkeypatch.setattr(fd, "_last_attempt", lambda *a, **k: None)

    candidates = [
        fd.Candidate(id="repo#1", repo="repo", tool="", title="t1", kind="issue", created_at="2020-01-01"),
    ]

    class FakeRecommendation:
        def __init__(self, chosen: fd.Candidate) -> None:
            self.chosen = chosen

    class FakeOrchestrator:
        def __init__(self, **_kwargs) -> None:
            pass

        def next_n(self, _n: int) -> list[FakeRecommendation]:
            return [FakeRecommendation(c) for c in candidates]

    monkeypatch.setattr(fd, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(fd, "_preflight_distinct_accounts", lambda accounts: [])
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    rc = fd.main(["--accounts", str(tmp_path / "a")])

    assert rc == 0


def test_dispatch_one_passes_max_turns_and_max_budget_to_claude(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    subprocess.run(["git", "-C", str(repo_path), "branch", "-M", "main"], check=True)

    monkeypatch.setattr(fd, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(fd, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    captured: dict = {}
    real_run = fd.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "claude":
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t", kind="issue", created_at="2020-01-01"
    )
    account = _account(tmp_path / "cfg", {"model": "sonnet"})

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.5,
        max_turns=7,
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        org="MyThingsLab",
    )

    argv = captured["argv"]
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"


def test_main_requires_all_three_app_flags_together(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(SystemExit):
        fd.main(["--accounts", str(tmp_path / "a"), "--app-id", "4260739"])


def test_main_mints_app_token_and_sets_gh_token_env(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")

    minted = []

    def fake_token(app_id, installation_id, private_key_path):
        minted.append((app_id, installation_id, private_key_path))
        return "ghs_minted_token"

    monkeypatch.setattr(fd, "github_app_token", fake_token)
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: None)
    _wire_single_candidate_orchestrator(monkeypatch)

    rc = fd.main(
        [
            "--accounts", str(tmp_path / "a"),
            "--app-id", "4260739",
            "--app-installation-id", "145558758",
            "--app-private-key", "/path/to/key.pem",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert minted == [("4260739", "145558758", "/path/to/key.pem")]
    assert os.environ["GH_TOKEN"] == "ghs_minted_token"
    assert "authenticating as the GitHub App" in out


def test_main_without_app_flags_does_not_touch_gh_token_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(fd, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fd, "_dispatch_one", lambda *a, **k: None)
    _wire_single_candidate_orchestrator(monkeypatch)

    fd.main(["--accounts", str(tmp_path / "a")])

    assert "GH_TOKEN" not in os.environ


def test_dispatch_one_worker_env_inherits_gh_token_from_process(
    tmp_path: Path, monkeypatch
) -> None:
    # The whole point of setting os.environ["GH_TOKEN"] once in main(): the
    # spawned worker's `env = {**os.environ, ...}` picks it up with no
    # separate wiring. Prove that inheritance directly against _dispatch_one.
    monkeypatch.setenv("GH_TOKEN", "ghs_from_app")

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    subprocess.run(["git", "-C", str(repo_path), "branch", "-M", "main"], check=True)

    monkeypatch.setattr(fd, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(fd, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(fd, "_open_pr_number", lambda *a, **k: None)

    captured: dict = {}
    real_run = fd.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "claude":
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(fd.subprocess, "run", fake_run)

    candidate = fd.Candidate(
        id="repo#1", repo="repo", tool="", title="t", kind="issue", created_at="2020-01-01"
    )
    account = _account(tmp_path / "cfg", {"model": "sonnet"})

    fd._dispatch_one(
        account,
        candidate,
        execute=True,
        max_budget_usd=1.5,
        max_turns=7,
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        org="MyThingsLab",
    )

    assert captured["env"]["GH_TOKEN"] == "ghs_from_app"
