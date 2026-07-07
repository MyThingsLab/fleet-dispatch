from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from mythings.ledger import Ledger

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
        account, candidate, *, execute, max_budget_usd, ledger, org, prior=None, rtk=False
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
        account, candidate, *, execute, max_budget_usd, ledger, org, prior=None, rtk=False
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
        account, candidate, *, execute, max_budget_usd, ledger, org, prior=None, rtk=False
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
        account, candidate, *, execute, max_budget_usd, ledger, org, prior=None, rtk=False
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
