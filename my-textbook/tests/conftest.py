from __future__ import annotations

import json

import pytest

# Shared fakes come from mythings.testing; only the payload tables and the
# bibliography-issue wiring stay local.
from mythings.testing import FakeGh, ScriptedEngine
from mythings.testing import fake_fetch as _fake_fetch

from mytextbook.retrieval import (
    GOOGLE_BOOKS_ENDPOINT,
    OPENLIBRARY_SEARCH,
    OPENLIBRARY_WORKS,
    TAVILY_ENDPOINT,
)

__all__ = ["ScriptedEngine"]

OPENLIBRARY_JSON = {
    "docs": [
        {
            "key": "/works/OL1W",
            "title": "Introduction to Electrodynamics",
            "author_name": ["David J. Griffiths"],
            "first_publish_year": 1981,
            "edition_count": 30,
            "ratings_count": 12,
            "isbn": ["9780521809269", "0521809266"],
            "subject": ["Electrodynamics", "Physics"],
        },
        {
            "key": "/works/OL2W",
            "title": "Classical Electrodynamics",
            "author_name": ["John David Jackson"],
            "first_publish_year": 1962,
            "edition_count": 10,
            "isbn": ["9780471309321"],
            "subject": ["Electrodynamics"],
        },
    ]
}

GOOGLE_JSON = {
    "items": [
        {
            "id": "gb-jackson",
            "volumeInfo": {
                "title": "Classical Electrodynamics",
                "authors": ["John David Jackson"],
                "publishedDate": "1998",
                "description": "The standard graduate reference on electromagnetism.",
                "categories": ["Science"],
                "ratingsCount": 40,
                "infoLink": "https://books.google.com/books?id=gb-jackson",
                "industryIdentifiers": [
                    {"type": "ISBN_10", "identifier": "047130932X"},
                    {"type": "ISBN_13", "identifier": "9780471309321"},
                ],
            },
        }
    ]
}

TAVILY_JSON = {
    "results": [
        {
            "title": "What is the best E&M textbook? — Physics Stack Exchange",
            "url": "https://physics.stackexchange.com/q/12345",
            "content": "Griffiths for undergrad, Jackson for graduate work.",
        }
    ]
}

WORK_JSON = {
    "table_of_contents": [
        {"label": "1", "title": "Vector Analysis"},
        {"label": "2", "title": "Electrostatics"},
        {"label": "3", "title": "Potentials"},
        {"label": "4", "title": "Magnetostatics"},
    ]
}

fake_fetch = _fake_fetch(
    {
        OPENLIBRARY_SEARCH: OPENLIBRARY_JSON,
        GOOGLE_BOOKS_ENDPOINT: GOOGLE_JSON,
        TAVILY_ENDPOINT: TAVILY_JSON,
        OPENLIBRARY_WORKS: WORK_JSON,
    }
)

empty_fetch = _fake_fetch(
    {TAVILY_ENDPOINT: {"results": []}, OPENLIBRARY_WORKS: {}},
    default=json.dumps({"docs": [], "items": []}).encode(),
)


def fake_gh(*, open_bibliography_issues: list[dict] | None = None) -> FakeGh:
    issues = open_bibliography_issues or []
    state = {"next_issue": 200}

    def issue_list(argv: list[str]) -> str:
        return json.dumps(
            [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "body": i.get("body", ""),
                    "labels": [{"name": "my-bibliography"}],
                    "url": f"https://github.com/owner/name/issues/{i['number']}",
                }
                for i in issues
            ]
        )

    def issue_create(argv: list[str]) -> str:
        state["next_issue"] += 1
        return f"https://github.com/owner/name/issues/{state['next_issue']}\n"

    return FakeGh(
        {
            ("issue", "list"): issue_list,
            ("issue", "create"): issue_create,
            ("issue", "edit"): "",
        }
    )


@pytest.fixture
def ledger(tmp_path):
    from mythings.ledger import Ledger

    return Ledger(tmp_path / "ledger.jsonl")
