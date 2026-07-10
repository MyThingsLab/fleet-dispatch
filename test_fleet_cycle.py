from __future__ import annotations

from pathlib import Path

import pytest

import fleet_cycle as fc


def test_select_brief_issues_skips_issues_with_pending_brief_pr() -> None:
    picked = fc._select_brief_issues(
        [4, 5, 6, 11],
        ["my-researcher/11", "my-researcher/5", "feat/unrelated"],
        count=2,
    )
    assert picked == [4, 6]


def test_select_brief_issues_caps_at_count_oldest_first() -> None:
    assert fc._select_brief_issues([9, 7, 8], [], count=2) == [7, 8]


def test_select_brief_issues_ignores_non_research_branches() -> None:
    assert fc._select_brief_issues([3], ["my-researcher/study-plan", "fix/3"], count=1) == [3]


def test_select_brief_issues_empty_when_all_pending() -> None:
    assert fc._select_brief_issues([2], ["my-researcher/2"], count=1) == []


def _capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict | None]]:
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd: list[str], *, check: bool = False, env: dict | None = None) -> int:
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(fc, "_run", fake_run)
    return calls


def test_main_dry_run_does_not_invoke_myresearcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path)
    monkeypatch.setattr(fc, "_brief_candidates", lambda count: [4])
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)


def test_main_execute_briefs_candidates_under_first_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path)
    monkeypatch.setattr(fc, "_brief_candidates", lambda count: [4, 5][:count])
    fc.main([
        "--accounts", "/tmp/acct1,/tmp/acct2",
        "--skip-dispatch", "--execute",
        "--engine", "claude-cli",
        "--brief-count", "2",
    ])
    briefs = [(cmd, env) for cmd, env in calls if cmd[0] == "myresearcher"]
    assert [cmd[cmd.index("--issue") + 1] for cmd, _ in briefs] == ["4", "5"]
    for cmd, env in briefs:
        assert cmd[1] == "brief"
        assert cmd[cmd.index("--repo") + 1] == "MyThingsLab/study"
        assert cmd[cmd.index("--repo-root") + 1] == str(tmp_path)
        assert cmd[cmd.index("--engine") + 1] == "claude-cli"
        assert cmd[cmd.index("--sources") + 1] == "arxiv"
        assert env is not None and env["CLAUDE_CONFIG_DIR"] == "/tmp/acct1"


def test_main_brief_count_zero_never_queries_github(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_runs(monkeypatch)

    def boom(count: int) -> list[int]:
        raise AssertionError("should not query GitHub when --brief-count 0")

    monkeypatch.setattr(fc, "_brief_candidates", boom)
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--brief-count", "0", "--execute"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)


def test_main_skips_briefs_when_study_clone_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path / "missing")
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)
    assert "no study clone" in capsys.readouterr().out


def test_main_execute_runs_mydashboard_render_after_mydocs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "WORKSPACE_ROOT", tmp_path)
    docs_site_root = tmp_path / fc.DOCS_SITE_CLONE
    docs_site_root.mkdir()
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute", "--brief-count", "0"])
    tools = [cmd[0] for cmd, _ in calls]
    assert "mydashboard" in tools
    assert tools.index("mydocs") < tools.index("mydashboard") < tools.index("myprojector")
    dashboard_cmd, _ = next((cmd, env) for cmd, env in calls if cmd[0] == "mydashboard")
    assert dashboard_cmd[1] == "render"
    assert dashboard_cmd[dashboard_cmd.index("--repo-root") + 1] == str(docs_site_root)
    assert dashboard_cmd[dashboard_cmd.index("--workspace") + 1] == str(tmp_path)


def test_main_skips_mydashboard_when_docs_site_clone_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "WORKSPACE_ROOT", tmp_path)
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute", "--brief-count", "0"])
    assert not any(cmd[0] == "mydashboard" for cmd, _ in calls)
    assert "skipping mydashboard" in capsys.readouterr().out


def test_gh_json_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    assert fc._gh_json(["issue", "list"]) is None
