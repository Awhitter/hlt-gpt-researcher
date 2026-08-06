"""Tests for the product-owner tools the Slack agent uses.

Two properties carry most of the weight here.

**Pagination.** One unpaginated query capped at 250 issues; paginating found
345. A product owner reporting on a truncated board is worse than one that says
it cannot see.

**The repo label.** A single Linear team (`NUR`) holds both Nursing Mastery and
ScraperVault work, and `repo:*` labels are the only thing separating them.
Against the live board, filtering "in flight" to nursing-mastery took it from 25
issues to 7 — so an unfiltered answer would have been 72% other-product work
presented as Nursing Mastery's.
"""
from __future__ import annotations

import pytest

from mcp_server import product_tools as pt


def _issue(ident, state="In Progress", state_type="started", project=None, labels=(), priority=0):
    return {
        "identifier": ident,
        "title": f"title {ident}",
        "priority": priority,
        "url": f"https://linear.app/x/issue/{ident}",
        "updatedAt": f"2026-08-0{ident[-1]}T00:00:00Z",
        "state": {"name": state, "type": state_type},
        "assignee": None,
        "project": {"name": project} if project else None,
        "parent": None,
        "labels": {"nodes": [{"name": n} for n in labels]},
    }


@pytest.fixture
def board(monkeypatch):
    """A two-page board, so pagination is actually exercised."""
    pages = [
        {
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [
                    _issue("NUR-1", labels=["repo:nursing-mastery"], project="Wave 2"),
                    _issue("NUR-2", labels=["repo:scrapervault"], project="SV Intelligence"),
                ],
            }
        },
        {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    _issue("NUR-3", "Backlog", "backlog", labels=["repo:nursing-mastery"]),
                    _issue("NUR-4", "Backlog", "backlog", priority=2, project="Wave 2"),
                ],
            }
        },
    ]
    calls = {"n": 0}

    # Key off the cursor rather than a counter, so a test may walk the board
    # more than once — exactly what comparing filtered vs unfiltered does.
    def fake(query, variables=None, timeout=15):
        calls["n"] += 1
        cursor = (variables or {}).get("after")
        return pages[0] if cursor is None else pages[1]

    monkeypatch.setattr(pt, "linear", fake)
    return calls


# --- the two properties that matter -----------------------------------------


def test_pagination_walks_every_page(board):
    """An unpaginated query silently truncated the board at 250 of 345."""
    assert len(pt._open_issues()) == 4
    assert board["n"] == 2, "second page was never fetched"


def test_repo_label_separates_two_products_on_one_board(board):
    everything = pt.work_in_flight()
    just_nm = pt.work_in_flight(repo_label="repo:nursing-mastery")

    assert everything["count"] == 2
    assert just_nm["count"] == 1
    assert just_nm["issues"][0]["id"] == "NUR-1"


# --- shaping and rollups ----------------------------------------------------


def test_in_flight_is_only_started_work(board):
    assert {i["id"] for i in pt.work_in_flight()["issues"]} == {"NUR-1", "NUR-2"}


def test_upcoming_is_only_unstarted_work(board):
    assert {i["id"] for i in pt.work_upcoming()["issues"]} == {"NUR-3", "NUR-4"}


def test_upcoming_groups_by_project_including_the_unprojected(board):
    assert pt.work_upcoming()["by_project"] == {"(no project)": 1, "Wave 2": 1}


def test_board_health_counts_what_a_po_would_fix(board):
    health = pt.board_health()
    assert health["open"] == 4
    assert health["in_progress"] == 2
    assert health["no_project"] == 1
    assert health["no_priority"] == 3
    assert health["no_assignee"] == 4
    assert health["no_repo_label"] == 1


def test_issue_shape_flattens_linear_nesting():
    shaped = pt._shape(_issue("NUR-9", project="Wave 2", labels=["repo:nursing-mastery"]))
    assert shaped["project"] == "Wave 2"
    assert shaped["labels"] == ["repo:nursing-mastery"]
    assert shaped["state"] == "In Progress"


# --- auth and failure -------------------------------------------------------


def test_oauth_tokens_get_bearer_and_api_keys_do_not():
    """hlt_brain sends the raw key, so an OAuth token silently 401s there."""
    assert pt._auth_header("lin_oauth_abc") == "Bearer lin_oauth_abc"
    assert pt._auth_header("lin_api_abc") == "lin_api_abc"


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_NURSING_KEY", raising=False)
    with pytest.raises(pt.LinearError, match="not configured"):
        pt._api_key()


def test_tool_wrapper_reports_the_error_instead_of_returning_nothing(monkeypatch):
    """A tool that returns nothing teaches the model to invent an answer."""
    registered = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    pt.register_product_tools(FakeMCP())
    monkeypatch.setattr(
        pt, "board_health", lambda **_: (_ for _ in ()).throw(pt.LinearError("Linear is down"))
    )

    assert registered["linear_board_health"]() == {"error": "Linear is down"}


# --- write rails ------------------------------------------------------------


@pytest.mark.parametrize("missing", ["title", "what", "why", "done_when"])
def test_an_issue_nobody_can_pick_up_cold_is_refused(monkeypatch, missing):
    """76 open issues on this board have no project and rot in triage."""
    args = {"title": "t", "what": "w", "why": "y", "done_when": "d"}
    args[missing] = "   "
    monkeypatch.setattr(pt, "_team_id", lambda team: "team-id")

    with pytest.raises(pt.LinearError, match=missing):
        pt.issue_create(**args)


def test_a_wrong_project_name_refuses_rather_than_filing_anywhere(monkeypatch):
    monkeypatch.setattr(pt, "_team_id", lambda team: "team-id")
    monkeypatch.setattr(pt, "linear", lambda q, v=None, timeout=15: {"projects": {"nodes": []}})

    with pytest.raises(pt.LinearError, match="No project"):
        pt.issue_create(title="t", what="w", why="y", done_when="d", project="Wave 9")


def test_update_with_nothing_to_change_is_refused(monkeypatch):
    monkeypatch.setattr(pt, "issue_get", lambda identifier: {"id": "NUR-1"})
    with pytest.raises(pt.LinearError, match="Nothing to change"):
        pt.issue_update("NUR-1")


def test_update_returns_before_and_after_as_the_receipt(monkeypatch):
    states = iter([{"id": "NUR-1", "title": "old"}, {"id": "NUR-1", "title": "new"}])
    monkeypatch.setattr(pt, "issue_get", lambda identifier: next(states))
    monkeypatch.setattr(pt, "linear", lambda q, v=None, timeout=15: {"issueUpdate": {"success": True}})

    result = pt.issue_update("NUR-1", title="new")

    assert result["before"]["title"] == "old"
    assert result["after"]["title"] == "new"


def test_there_is_no_bulk_write_verb():
    """A wrong sweep across 345 issues is painful to unwind, so the capability
    simply does not exist rather than being guarded."""
    writes = [n for n in dir(pt) if n.startswith("issue_") and not n.startswith("issue_get")]
    assert sorted(writes) == ["issue_create", "issue_update"]
