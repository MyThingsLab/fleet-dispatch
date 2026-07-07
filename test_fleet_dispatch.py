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

    def fake_dispatch_one(account, candidate, *, execute, max_budget_usd, ledger, org, rtk=False):
        start = time.monotonic()
        time.sleep(0.2)
        end = time.monotonic()
        with calls_lock:
            calls.append((account.name, start, end))

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)

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
    def fake_dispatch_one(account, candidate, *, execute, max_budget_usd, ledger, org, rtk=False):
        raise RuntimeError(f"boom-{account.name}")

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)

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

    def fake_dispatch_one(account, candidate, *, execute, max_budget_usd, ledger, org, rtk=False):
        dispatched.append(candidate.id)

    monkeypatch.setattr(fd, "_dispatch_one", fake_dispatch_one)

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
