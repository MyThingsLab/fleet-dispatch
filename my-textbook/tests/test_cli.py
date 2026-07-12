from __future__ import annotations

import mytextbook.cli as cli
from conftest import fake_fetch


def _patch_engine_and_fetch(monkeypatch):
    # Keep the CLI end-to-end but mock the one network boundary and force noop.
    import mytextbook.textbook as tb

    monkeypatch.setattr(tb, "_http", fake_fetch)
    monkeypatch.setattr("mytextbook.retrieval._http", fake_fetch)


def test_find_writes_markdown_to_out(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_make", lambda args, repo=None: _textbook(args, tmp_path))
    out = tmp_path / "rec.md"
    rc = cli.main(
        ["find", "electrodynamics", "--out", str(out), "--ledger", str(tmp_path / "l.jsonl")]
    )
    assert rc == 0
    assert "# Textbooks for: electrodynamics" in out.read_text()
    err = capsys.readouterr().err
    assert "success (find)" in err


def test_plan_prints_to_stdout(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_make", lambda args, repo=None: _textbook(args, tmp_path))
    rc = cli.main(
        [
            "plan",
            "Griffiths",
            "--olid",
            "olid:OL1W",
            "--weeks",
            "2",
            "--ledger",
            str(tmp_path / "l.jsonl"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Reading plan: Griffiths" in out


def _textbook(args, tmp_path):
    from mythings.engine import NoopEngine
    from mythings.ledger import Ledger

    from mytextbook.textbook import Textbook

    return Textbook(
        ledger=Ledger(tmp_path / "l.jsonl"),
        engine=NoopEngine(),
        fetch=fake_fetch,
        web_api_key="k",
    )
