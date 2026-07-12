from __future__ import annotations

import json

from mythings.engine import NoopEngine

from conftest import ScriptedEngine
from mytextbook.curation import (
    curate,
    plan_reading,
    render_plan,
    render_recommendation,
)
from mytextbook.retrieval import Book

BOOKS = [
    Book(book_id="olid:OL1W", title="Griffiths", origin="openlibrary", url="u1", popularity=42),
    Book(book_id="olid:OL2W", title="Jackson", origin="openlibrary", url="u2", popularity=10),
]


def test_curate_parses_and_drops_invented_books() -> None:
    reply = json.dumps(
        {
            "picks": [
                {"book_id": "olid:OL2W", "why": "graduate depth", "order": 2},
                {"book_id": "olid:OL1W", "why": "start here", "order": 1},
                {"book_id": "olid:INVENTED", "why": "hallucinated", "order": 3},
            ],
            "top_pick": "olid:OL1W",
            "reading_order": ["Read Griffiths", "Then Jackson"],
        }
    )
    rec = curate(ScriptedEngine(reply), "E&M", BOOKS)
    assert not rec.degraded
    assert [p.book_id for p in rec.picks] == ["olid:OL1W", "olid:OL2W"]  # sorted by order
    assert rec.top_pick == "olid:OL1W"
    assert rec.reading_order == ["Read Griffiths", "Then Jackson"]


def test_curate_rejects_invented_top_pick() -> None:
    reply = json.dumps({"picks": [{"book_id": "olid:OL2W", "order": 1}], "top_pick": "olid:X"})
    rec = curate(ScriptedEngine(reply), "E&M", BOOKS)
    assert rec.top_pick == "olid:OL2W"  # falls back to first valid pick


def test_curate_degrades_on_noop() -> None:
    rec = curate(NoopEngine(), "E&M", BOOKS)
    assert rec.degraded
    assert rec.top_pick == "olid:OL1W"  # popularity order
    assert [p.book_id for p in rec.picks] == ["olid:OL1W", "olid:OL2W"]


def test_plan_parses_units() -> None:
    reply = json.dumps(
        {
            "units": [
                {"title": "Week 1", "chapters": ["Ch 1"], "goal": "vectors"},
                {"title": "Week 2", "chapters": ["Ch 2", "Ch 3"], "goal": "fields"},
            ],
            "prerequisites": ["calculus"],
        }
    )
    plan = plan_reading(ScriptedEngine(reply), "Griffiths", ["Ch 1", "Ch 2", "Ch 3"], weeks=2)
    assert not plan.degraded
    assert plan.weeks == 2
    assert plan.units[1].chapters == ["Ch 2", "Ch 3"]
    assert plan.prerequisites == ["calculus"]


def test_plan_degrades_splits_toc_evenly() -> None:
    plan = plan_reading(NoopEngine(), "Griffiths", ["a", "b", "c", "d"], weeks=2)
    assert plan.degraded
    assert [u.chapters for u in plan.units] == [["a", "b"], ["c", "d"]]


def test_plan_degrades_without_toc() -> None:
    plan = plan_reading(NoopEngine(), "Griffiths", [], weeks=3)
    assert plan.degraded
    assert len(plan.units) == 3


def test_render_recommendation_includes_top_pick_and_links() -> None:
    rec = curate(NoopEngine(), "E&M", BOOKS)
    md = render_recommendation(rec)
    assert "# Textbooks for: E&M" in md
    assert "## Top pick" in md
    assert "(u1)" in md


def test_render_plan_lists_units() -> None:
    plan = plan_reading(NoopEngine(), "Griffiths", ["a", "b"], weeks=2)
    md = render_plan(plan)
    assert "# Reading plan: Griffiths" in md
    assert "### 1." in md and "### 2." in md
