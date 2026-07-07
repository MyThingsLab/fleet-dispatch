from __future__ import annotations

import json
from pathlib import Path

import pytest
from mythings.ledger import Ledger

import fleet_dispatch as fd
from fleet_usage import UsageReport


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
