from __future__ import annotations

from mythings.engine import NoopEngine
from mythings.policy import ALLOW, Action, PolicyResult

from conftest import ScriptedEngine, empty_fetch, fake_fetch, fake_gh
from mytextbook.textbook import Textbook


class AllowAll:
    def evaluate(self, action: Action) -> PolicyResult:
        return ALLOW


def _make(ledger, runner=None, *, engine=None, repo=None, **kw):
    return Textbook(
        ledger=ledger,
        engine=engine or NoopEngine(),
        repo=repo,
        policy=AllowAll(),
        runner=runner or fake_gh(),
        fetch=fake_fetch,
        web_api_key="k",
        **kw,
    )


def test_find_returns_markdown_and_ledgers(ledger) -> None:
    tb = _make(ledger)
    result = tb.find("electrodynamics", no_handoff=True)
    assert result.outcome == "success"
    assert "# Textbooks for: electrodynamics" in result.markdown
    entries = ledger.read(tool="mytextbook", kind="recommendation")
    assert entries and entries[-1].outcome == "success"


def test_find_skips_when_no_books(ledger) -> None:
    tb = Textbook(ledger=ledger, engine=NoopEngine(), fetch=empty_fetch, web_api_key="k")
    result = tb.find("nonsense")
    assert result.outcome == "skipped"
    assert result.markdown == ""


def test_find_empty_subject_fails(ledger) -> None:
    result = _make(ledger).find("   ")
    assert result.outcome == "failure"


def test_find_files_bibliography_handoff_for_top_pick(ledger) -> None:
    runner = fake_gh()
    tb = _make(ledger, runner, repo="owner/name")
    result = tb.find("electrodynamics")
    assert len(result.handoff_issues) == 1
    assert result.handoff_issues[0]["isbn"] == "9780521809269"
    created = [c for c in runner.calls if c[:2] == ["issue", "create"]]
    assert len(created) == 1
    # The issue body carries the bare isbn locator my-bibliography understands.
    assert "isbn:9780521809269" in " ".join(created[0])


def test_find_no_handoff_without_repo(ledger) -> None:
    runner = fake_gh()
    tb = _make(ledger, runner)  # repo=None
    result = tb.find("electrodynamics")
    assert result.handoff_issues == []
    assert not any(c[:2] == ["issue", "create"] for c in runner.calls)


def test_find_dedups_existing_bibliography_issue(ledger) -> None:
    runner = fake_gh(
        open_bibliography_issues=[
            {"number": 5, "title": "bibliography: catalog isbn:9780521809269"}
        ]
    )
    tb = _make(ledger, runner, repo="owner/name")
    result = tb.find("electrodynamics")
    assert result.handoff_issues == []
    assert not any(c[:2] == ["issue", "create"] for c in runner.calls)


def test_plan_uses_toc_from_olid(ledger) -> None:
    reply = (
        '{"units": [{"title": "Week 1", "chapters": ["Vectors"], "goal": "g"}], '
        '"prerequisites": ["calculus"]}'
    )
    tb = _make(ledger, engine=ScriptedEngine(reply))
    result = tb.plan("Griffiths", olid="olid:OL1W", weeks=1)
    assert result.outcome == "success"
    assert "# Reading plan: Griffiths" in result.markdown
    entries = ledger.read(tool="mytextbook", kind="plan")
    assert entries[-1].data["toc_entries"] == 4


def test_plan_empty_title_fails(ledger) -> None:
    result = _make(ledger).plan("  ")
    assert result.outcome == "failure"
