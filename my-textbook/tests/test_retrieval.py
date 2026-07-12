from __future__ import annotations

from conftest import empty_fetch, fake_fetch
from mytextbook.retrieval import (
    discover,
    fetch_toc,
    search_google_books,
    search_openlibrary,
    search_web,
)


def test_openlibrary_parses_books_with_popularity_signal() -> None:
    books = search_openlibrary("electrodynamics", fetch=fake_fetch)
    assert [b.book_id for b in books] == ["olid:OL1W", "olid:OL2W"]
    griffiths = books[0]
    assert griffiths.title == "Introduction to Electrodynamics"
    assert griffiths.authors == ["David J. Griffiths"]
    assert griffiths.isbn == "9780521809269"
    # edition_count (30) + ratings_count (12)
    assert griffiths.popularity == 42


def test_google_books_prefers_isbn13() -> None:
    books = search_google_books("electrodynamics", fetch=fake_fetch)
    assert len(books) == 1
    assert books[0].book_id == "gbid:gb-jackson"
    assert books[0].isbn == "9780471309321"
    assert books[0].popularity == 40


def test_web_requires_api_key() -> None:
    assert search_web("electrodynamics", api_key=None, fetch=fake_fetch) == []
    hits = search_web("electrodynamics", api_key="k", fetch=fake_fetch)
    assert hits[0].origin == "web"
    assert hits[0].popularity >= 1


def test_discover_dedups_and_ranks_by_popularity() -> None:
    books = discover("electrodynamics", fetch=fake_fetch, web_api_key="k")
    # Jackson appears in both OpenLibrary and Google — deduped to one entry.
    jackson = [b for b in books if "electrodynamics" in b.title.lower() and "Classical" in b.title]
    assert len(jackson) == 1
    # Most popular first: Griffiths (42) > Jackson (10, OL kept over Google's 40).
    assert books[0].book_id == "olid:OL1W"


def test_discover_empty_when_nothing_found() -> None:
    assert discover("nonsense", fetch=empty_fetch, web_api_key="k") == []


def test_fetch_toc_reads_table_of_contents() -> None:
    toc = fetch_toc("olid:OL1W", fetch=fake_fetch)
    assert toc == ["1 Vector Analysis", "2 Electrostatics", "3 Potentials", "4 Magnetostatics"]


def test_fetch_toc_absent_returns_empty() -> None:
    assert fetch_toc("olid:OL9W", fetch=empty_fetch) == []
