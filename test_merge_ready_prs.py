from __future__ import annotations

import pytest
from mythings.policy import Decision

import merge_ready_prs
from merge_ready_prs import PR, approve, merge_by_asking

# Merging is the one thing the fleet says only a human may do. It rides the ask
# channel: MyGuard answers the structured `pr-merge` Action with ASK, which is a
# real Allow/Deny prompt on the operator's phone -- and their tap *is* the merge.
#
# The property that matters: nothing merges without an explicit ALLOW.


def _pr(number: int = 1, repo: str = "my-idea") -> PR:
    return PR(
        repo=repo,
        number=number,
        title=f"a change to {repo}",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=[],
    )


class _Guard:
    # Stands in for MyGuard with an ask channel wired: whatever the human "taps".
    def __init__(self, *decisions: Decision) -> None:
        self.decisions = list(decisions)
        self.asked: list[str] = []

    def evaluate(self, action):
        from mythings.policy import PolicyResult

        self.asked.append(f"{action.payload['repo']}#{action.payload['number']}")
        return PolicyResult(self.decisions.pop(0), reason="human", rule="merge_needs_a_human")


@pytest.fixture
def merged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    done: list[str] = []
    monkeypatch.setattr(
        merge_ready_prs, "merge", lambda pr, **kw: done.append(f"{pr.repo}#{pr.number}")
    )
    return done


def test_the_action_carries_what_a_human_needs_to_decide() -> None:
    # The prompt is rendered from this payload. "Action: pr-merge, number: 12" with
    # no repo or title is not something anyone can approve responsibly.
    guard = _Guard(Decision.ALLOW)

    approve(_pr(12, "my-guard"), guard)

    (action,) = [guard.asked[0]]
    assert action == "MyThingsLab/my-guard#12"


def test_an_approved_pr_is_merged(merged: list[str]) -> None:
    assert merge_by_asking([_pr(1)], _Guard(Decision.ALLOW), budget_s=60) == 0
    assert merged == ["my-idea#1"]


def test_a_refused_pr_is_not_merged(merged: list[str]) -> None:
    merge_by_asking([_pr(1)], _Guard(Decision.DENY), budget_s=60)

    assert merged == []


def test_an_unanswered_prompt_is_a_no(merged: list[str]) -> None:
    # A timeout comes back as DENY, indistinguishable from a tap on Deny. Both are
    # a "no", and fail-closed is the only safe reading of silence.
    merge_by_asking([_pr(1)], _Guard(Decision.DENY), budget_s=60)

    assert merged == []


def test_each_pr_is_asked_about_separately(merged: list[str]) -> None:
    # Approving one merge must never approve the next. One tap, one PR.
    guard = _Guard(Decision.ALLOW, Decision.DENY, Decision.ALLOW)

    merge_by_asking([_pr(1), _pr(2), _pr(3)], guard, budget_s=60)

    assert len(guard.asked) == 3
    assert merged == ["my-idea#1", "my-idea#3"]


def test_the_budget_stops_the_pass_rather_than_timing_out_pr_after_pr(
    merged: list[str],
) -> None:
    # Each unanswered ask blocks for the full timeout. A queue of PRs with nobody
    # home would spend the entire pass timing out, one prompt at a time, so the
    # budget caps it -- and the rest are reported *unasked*, not silently denied.
    guard = _Guard(Decision.ALLOW)

    merge_by_asking([_pr(1), _pr(2), _pr(3)], guard, budget_s=0)

    assert guard.asked == []  # the budget was already gone
    assert merged == []


def test_a_merge_that_fails_after_approval_does_not_strand_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[str] = []

    def flaky(pr: PR, **kw: object) -> None:
        if pr.number == 1:
            raise RuntimeError("base branch moved")
        done.append(f"{pr.repo}#{pr.number}")

    monkeypatch.setattr(merge_ready_prs, "merge", flaky)

    code = merge_by_asking([_pr(1), _pr(2)], _Guard(Decision.ALLOW, Decision.ALLOW), budget_s=60)

    assert done == ["my-idea#2"]  # one stuck PR must not strand the queue
    assert code == 1  # but the run is honest about having failed
