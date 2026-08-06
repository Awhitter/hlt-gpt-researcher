"""Product-owner tools over Linear, for the Slack agent (Cleo).

Why these are here rather than reusing what exists:

* ``hlt_brain._linear_graphql`` is too thin to build on — no pagination, no
  retry, and it sends the raw key so an OAuth token fails. Its shipped-work query
  is ``first: 15`` on a 21-day window ordered by ``updatedAt``, which returns the
  15 most recently *touched* completed issues rather than the most recently
  shipped ones.
* ``create_linear_change_request`` files project-less, label-less issues into
  triage, and its ``confirmed: true`` gate is caller-asserted — the real human
  step lives only in a React component. Not safe or useful from Slack.
* The Linear MCP is OAuth-only and unavailable in headless runs.

The lessons from ``nursing-mastery/scripts/linear-program/linear-client.mjs`` are
ported here rather than depended on across repos: page size 40 (larger returns
"Query too complex"), ``$teamId: ID!`` not ``String!``, honour ``Retry-After``.

**"What shipped" is deliberately not here.** One NUR team holds both
nursing-mastery and ScraperVault work, the repos merge hundreds of PRs a
fortnight, and both keep a coverage-gated CHANGELOG. The codegraph service has
the clones and answers that question far better; see its ``recent_changes`` tool.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

LINEAR_URL = "https://api.linear.app/graphql"
DEFAULT_TEAM = "NUR"
PAGE_SIZE = 40  # larger and Linear answers "Query too complex"
MAX_ATTEMPTS = 4

# One team holds both products; these labels are the only thing separating them.
REPO_LABELS = ("repo:nursing-mastery", "repo:scrapervault", "repo:katailyst2")

_ISSUE_FIELDS = """
identifier title priority url createdAt updatedAt completedAt
state { name type }
assignee { name }
project { name }
parent { identifier title }
labels(first: 10) { nodes { name } }
"""


class LinearError(RuntimeError):
    """Raised so a tool can say what went wrong instead of returning nothing."""


def _api_key() -> str:
    key = (os.getenv("LINEAR_NURSING_KEY") or os.getenv("LINEAR_API_KEY") or "").strip()
    if not key:
        raise LinearError("Linear is not configured (LINEAR_API_KEY is unset).")
    return key


def _auth_header(key: str) -> str:
    # Personal API keys go raw; OAuth tokens need the Bearer prefix.
    return f"Bearer {key}" if key.startswith("lin_oauth_") else key


def linear(query: str, variables: dict[str, Any] | None = None, timeout: int = 15) -> dict[str, Any]:
    """One GraphQL round trip, with the retry behaviour Linear actually needs."""
    key = _api_key()
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")

    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(LINEAR_URL, data=payload, method="POST")
        request.add_header("Authorization", _auth_header(key))
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last = error
            if error.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                retry_after = error.headers.get("Retry-After") if error.headers else None
                time.sleep(float(retry_after) if retry_after else 2**attempt * 0.25)
                continue
            raise LinearError(f"Linear returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            last = error
            if attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt * 0.25)
                continue
            raise LinearError(f"Could not reach Linear ({type(error).__name__}).") from error

        if body.get("errors"):
            raise LinearError(f"Linear rejected the query: {json.dumps(body['errors'])[:300]}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearError("Linear returned an unexpected body.")
        return data

    raise LinearError(f"Linear failed after {MAX_ATTEMPTS} attempts ({last}).")


def _shape(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten one issue into what a person in Slack needs."""
    return {
        "id": node.get("identifier"),
        "title": node.get("title"),
        "state": (node.get("state") or {}).get("name"),
        "stateType": (node.get("state") or {}).get("type"),
        "assignee": (node.get("assignee") or {}).get("name"),
        "project": (node.get("project") or {}).get("name"),
        "parent": (node.get("parent") or {}).get("identifier"),
        "labels": [n["name"] for n in ((node.get("labels") or {}).get("nodes") or [])],
        "priority": node.get("priority"),
        "url": node.get("url"),
        "updatedAt": node.get("updatedAt"),
    }


def _matches_repo(issue: dict[str, Any], repo_label: str | None) -> bool:
    return not repo_label or repo_label in issue["labels"]


def _count_by(issues: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        key = issue.get(field) or f"(no {field})"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _open_issues(team: str = DEFAULT_TEAM) -> list[dict[str, Any]]:
    """Every non-closed issue on the team, paginated properly."""
    query = f"""
    query OpenIssues($team: String!, $after: String) {{
      issues(
        first: {PAGE_SIZE}
        after: $after
        filter: {{
          team: {{ key: {{ eq: $team }} }}
          state: {{ type: {{ nin: ["completed", "canceled"] }} }}
        }}
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {_ISSUE_FIELDS} }}
      }}
    }}
    """
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = linear(query, {"team": team, "after": cursor})["issues"]
        out.extend(_shape(n) for n in page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


# --- read tools -------------------------------------------------------------


def work_in_flight(team: str = DEFAULT_TEAM, repo_label: str | None = None) -> dict[str, Any]:
    """What is actually being worked on right now."""
    issues = [i for i in _open_issues(team) if i["stateType"] == "started"]
    issues = [i for i in issues if _matches_repo(i, repo_label)]
    issues.sort(key=lambda i: i["updatedAt"] or "", reverse=True)
    return {"team": team, "repo_label": repo_label, "count": len(issues), "issues": issues}


def work_upcoming(
    team: str = DEFAULT_TEAM, project: str | None = None, repo_label: str | None = None, limit: int = 40
) -> dict[str, Any]:
    """Backlog, grouped by project, most-recently-touched first."""
    issues = [i for i in _open_issues(team) if i["stateType"] in {"backlog", "unstarted", "triage"}]
    issues = [i for i in issues if _matches_repo(i, repo_label)]
    if project:
        issues = [i for i in issues if (i["project"] or "").lower() == project.lower()]
    issues.sort(key=lambda i: i["updatedAt"] or "", reverse=True)

    return {
        "team": team,
        "project": project,
        "count": len(issues),
        "by_project": _count_by(issues, "project"),
        "issues": issues[:limit],
    }


def board_health(team: str = DEFAULT_TEAM) -> dict[str, Any]:
    """Where the board itself needs work.

    Reported honestly because it is real context for "what's the plan": most
    issues carry no project or priority, and there are no estimates anywhere.
    """
    issues = _open_issues(team)
    return {
        "team": team,
        "open": len(issues),
        "in_progress": sum(1 for i in issues if i["stateType"] == "started"),
        "no_project": sum(1 for i in issues if not i["project"]),
        "no_priority": sum(1 for i in issues if not i["priority"]),
        "no_assignee": sum(1 for i in issues if not i["assignee"]),
        "no_repo_label": sum(
            1 for i in issues if not any(lbl in i["labels"] for lbl in REPO_LABELS)
        ),
        "by_project": _count_by(issues, "project"),
    }


def issue_get(identifier: str) -> dict[str, Any]:
    """One issue in full, including the description — the what and why."""
    data = linear(
        f"""
        query Issue($id: String!) {{
          issue(id: $id) {{ {_ISSUE_FIELDS} description }}
        }}
        """,
        {"id": identifier.strip().upper()},
    )
    node = data.get("issue")
    if not node:
        raise LinearError(f"No issue {identifier}.")
    shaped = _shape(node)
    shaped["description"] = node.get("description")
    return shaped


# --- write tools ------------------------------------------------------------
#
# One issue per call. There is deliberately no bulk verb: a wrong sweep across
# a 250-issue board is painful to unwind, and the agent is reachable by a whole
# Slack workspace.


def _team_id(team: str) -> str:
    nodes = linear(
        "query Team($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id key } } }",
        {"key": team},
    )["teams"]["nodes"]
    if not nodes:
        raise LinearError(f"No Linear team {team}.")
    return nodes[0]["id"]


def _lookup(name: str, query: str, variables: dict[str, Any], path: str) -> str | None:
    for node in linear(query, variables)[path]["nodes"]:
        if node["name"].lower() == name.lower():
            return node["id"]
    return None


def _state_id(team: str, state: str) -> str:
    """Workflow states hang off the team, a level deeper than projects/labels."""
    nodes = linear(
        "query Team($key: String!) { teams(filter: { key: { eq: $key } }) "
        "{ nodes { states(first: 30) { nodes { id name } } } } }",
        {"key": team},
    )["teams"]["nodes"]
    if not nodes:
        raise LinearError(f"No Linear team {team}.")
    states = nodes[0]["states"]["nodes"]
    match = next((s for s in states if s["name"].lower() == state.lower()), None)
    if not match:
        raise LinearError(
            f"No state named {state!r}. Available: {', '.join(s['name'] for s in states)}."
        )
    return match["id"]


def issue_create(
    title: str,
    what: str,
    why: str,
    done_when: str,
    project: str | None = None,
    repo_label: str | None = None,
    team: str = DEFAULT_TEAM,
) -> dict[str, Any]:
    """File one issue that a new dev can read cold.

    what/why/done_when are required on purpose. An issue without them is the
    thing that rots in triage, and this board already has 76 like it.
    """
    for field, value in (("title", title), ("what", what), ("why", why), ("done_when", done_when)):
        if not (value or "").strip():
            raise LinearError(f"{field} is required — an issue without it cannot be picked up cold.")

    body = f"## What\n\n{what}\n\n## Why\n\n{why}\n\n## Done when\n\n{done_when}\n"
    payload: dict[str, Any] = {"teamId": _team_id(team), "title": title.strip(), "description": body}

    if project:
        pid = _lookup(project, "query { projects(first: 50) { nodes { id name } } }", {}, "projects")
        if not pid:
            raise LinearError(f"No project named {project!r}; not filing into the wrong one.")
        payload["projectId"] = pid
    if repo_label:
        lid = _lookup(
            repo_label,
            "query { issueLabels(first: 100) { nodes { id name } } }",
            {},
            "issueLabels",
        )
        if not lid:
            raise LinearError(f"No label named {repo_label!r}.")
        payload["labelIds"] = [lid]

    created = linear(
        """
        mutation Create($input: IssueCreateInput!) {
          issueCreate(input: $input) { success issue { identifier url title } }
        }
        """,
        {"input": payload},
    )["issueCreate"]
    if not created.get("success"):
        raise LinearError("Linear refused the create.")
    return {"action": "created", "issue": created["issue"], "project": project, "label": repo_label}


def issue_update(
    identifier: str,
    title: str | None = None,
    state: str | None = None,
    project: str | None = None,
    team: str = DEFAULT_TEAM,
) -> dict[str, Any]:
    """Change one issue, and report before → after so the thread is the receipt."""
    before = issue_get(identifier)
    payload: dict[str, Any] = {}

    if title:
        payload["title"] = title.strip()
    if state:
        payload["stateId"] = _state_id(team, state)
    if project:
        pid = _lookup(project, "query { projects(first: 50) { nodes { id name } } }", {}, "projects")
        if not pid:
            raise LinearError(f"No project named {project!r}.")
        payload["projectId"] = pid

    if not payload:
        raise LinearError("Nothing to change — pass a title, state, or project.")

    result = linear(
        """
        mutation Update($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) { success }
        }
        """,
        {"id": before["id"], "input": payload},
    )["issueUpdate"]
    if not result.get("success"):
        raise LinearError("Linear refused the update.")

    return {"action": "updated", "before": before, "after": issue_get(before["id"])}


# --- MCP registration -------------------------------------------------------


def register_product_tools(mcp: Any) -> None:
    """Expose the product-owner tools on the hosted MCP the Slack agent mounts."""

    def _guard(fn, **kwargs) -> dict[str, Any]:
        try:
            return fn(**kwargs)
        except LinearError as error:
            # Say what went wrong. A tool that returns nothing teaches the model
            # to invent an answer.
            return {"error": str(error)}

    @mcp.tool()
    def linear_in_flight(team: str = DEFAULT_TEAM, repo_label: str | None = None) -> dict[str, Any]:
        """What is being worked on right now, newest activity first.

        One Linear team (NUR) holds BOTH Nursing Mastery and ScraperVault work.
        Pass repo_label="repo:nursing-mastery" for a Nursing Mastery answer, or
        you will silently include the other product's issues.
        """
        return _guard(work_in_flight, team=team, repo_label=repo_label)

    @mcp.tool()
    def linear_upcoming(
        team: str = DEFAULT_TEAM,
        project: str | None = None,
        repo_label: str | None = None,
    ) -> dict[str, Any]:
        """The backlog: what is queued, grouped by project.

        Note there are no cycles or sprints on this board, so "this sprint" has
        no meaning — talk in projects (e.g. "Wave 2 — Personal layer") instead.
        """
        return _guard(work_upcoming, team=team, project=project, repo_label=repo_label)

    @mcp.tool()
    def linear_board_health(team: str = DEFAULT_TEAM) -> dict[str, Any]:
        """How much of the board is unlabelled, unprojected or unprioritised.

        Real context when someone asks "what's the plan": this board was run by
        one person, so most issues carry no project, priority or assignee.
        """
        return _guard(board_health, team=team)

    @mcp.tool()
    def linear_issue(identifier: str) -> dict[str, Any]:
        """One issue in full, including its description — the what and the why.

        Use this before answering "what is NUR-123" or before updating anything.
        """
        return _guard(issue_get, identifier=identifier)

    @mcp.tool()
    def linear_file_issue(
        title: str,
        what: str,
        why: str,
        done_when: str,
        project: str | None = None,
        repo_label: str | None = None,
        team: str = DEFAULT_TEAM,
    ) -> dict[str, Any]:
        """File ONE Linear issue. Confirm with the human in Slack before calling.

        what/why/done_when are required: an issue that cannot be picked up cold
        by someone who was not in the conversation is the kind that rots in
        triage, and this board already has dozens like that. Always set project
        and repo_label unless the human says otherwise.
        """
        return _guard(
            issue_create,
            title=title,
            what=what,
            why=why,
            done_when=done_when,
            project=project,
            repo_label=repo_label,
            team=team,
        )

    @mcp.tool()
    def linear_update_issue(
        identifier: str,
        title: str | None = None,
        state: str | None = None,
        project: str | None = None,
        team: str = DEFAULT_TEAM,
    ) -> dict[str, Any]:
        """Change ONE Linear issue and return before → after. Confirm first.

        There is no bulk verb on purpose. If asked to change many issues, do
        them one at a time and say what you are doing between each.
        """
        return _guard(
            issue_update,
            identifier=identifier,
            title=title,
            state=state,
            project=project,
            team=team,
        )
