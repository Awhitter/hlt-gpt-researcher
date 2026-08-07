"""Grounding metadata for reports stored by Mastery Research.

The research model can write prose, but it cannot declare its own work
verified. This module derives the most conservative status the stored
evidence supports and keeps legacy/unverified reports out of research memory.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

VERIFICATION_STATUSES = {"verified", "partial", "unverified"}

_GITHUB_PERMALINK = re.compile(
    r"https://github\.com/(?P<org>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"blob/(?P<sha>[0-9a-fA-F]{40})/(?P<path>[^\s)#?]+)"
    r"(?:#L(?P<line>\d+)(?:-L(?P<end_line>\d+))?)?"
)
_GITHUB_CODE_LINK = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(?P<kind>blob|tree)/(?P<ref>[^/\s)#?]+)/(?P<path>[^\s)#?]+)"
)
_SCOPE_INSTRUCTION_MARKER = "HLT research scope instructions:"


def sanitize_user_visible_research_data(value: Any) -> Any:
    """Remove internal prompt suffixes from user-facing events and logs."""
    if isinstance(value, str):
        return value.split(_SCOPE_INSTRUCTION_MARKER, 1)[0].rstrip()
    if isinstance(value, list):
        return [sanitize_user_visible_research_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_user_visible_research_data(item) for item in value)
    if isinstance(value, dict):
        return {
            key: sanitize_user_visible_research_data(item)
            for key, item in value.items()
        }
    return value


def extract_source_refs(answer: str) -> list[dict[str, Any]]:
    """Extract immutable GitHub evidence links from a report body."""
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int | None]] = set()
    for match in _GITHUB_PERMALINK.finditer(answer or ""):
        line = int(match.group("line")) if match.group("line") else None
        key = (
            match.group("org"),
            match.group("repo"),
            match.group("sha").lower(),
            match.group("path"),
            line,
        )
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "repo": f"{key[0]}/{key[1]}",
                "commitSha": key[2],
                "path": key[3],
                "line": line,
                "endLine": int(match.group("end_line")) if match.group("end_line") else None,
                "url": match.group(0),
                # A model-authored link is evidence-shaped, not proof. Only a
                # repository/codegraph validator may set this to true.
                "exists": None,
            }
        )
    return refs


def normalize_source_refs(value: Any, answer: str = "") -> list[dict[str, Any]]:
    """Normalize caller-provided refs and merge immutable links from prose."""
    refs: list[dict[str, Any]] = []
    if isinstance(value, list):
        for raw in value:
            if not isinstance(raw, dict):
                continue
            repo = str(raw.get("repo") or "").strip()
            sha = str(raw.get("commitSha") or "").strip().lower()
            path = str(raw.get("path") or "").strip().lstrip("/")
            url = str(raw.get("url") or "").strip()
            if not repo or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                continue
            if not re.fullmatch(r"[0-9a-f]{40}", sha) or not path:
                continue
            refs.append(
                {
                    "repo": repo,
                    "commitSha": sha,
                    "path": path,
                    "line": raw.get("line") if isinstance(raw.get("line"), int) else None,
                    "endLine": raw.get("endLine") if isinstance(raw.get("endLine"), int) else None,
                    "symbol": str(raw.get("symbol") or "").strip() or None,
                    "url": url or f"https://github.com/{repo}/blob/{sha}/{path}",
                    "indexedAt": str(raw.get("indexedAt") or "").strip() or None,
                    "exists": raw.get("exists") if isinstance(raw.get("exists"), bool) else None,
                }
            )
    refs.extend(extract_source_refs(answer))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int | None]] = set()
    for ref in refs:
        key = (ref["repo"], ref["commitSha"], ref["path"], ref.get("line"))
        if key not in seen:
            seen.add(key)
            deduped.append(ref)
    return deduped


def _validate_source_ref_with_codegraph(ref: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the authenticated source checkout before calling GitHub directly."""
    base_url = str(os.getenv("CODEGRAPH_MCP_URL") or "").strip().rstrip("/")
    token = str(os.getenv("CODEGRAPH_MCP_TOKEN") or "").strip()
    if not base_url or not token:
        return None
    if base_url.endswith("/mcp"):
        base_url = base_url[:-4]
    request = urllib.request.Request(
        f"{base_url}/verify-source",
        data=json.dumps(
            {
                "repo": ref["repo"],
                "path": ref["path"],
                "commitSha": ref["commitSha"],
            }
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:  # noqa: S310 - operator-configured internal service
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("exists"), bool):
        return None
    if (
        str(body.get("repo") or "").lower() != str(ref["repo"]).lower()
        or str(body.get("commitSha") or "").lower() != ref["commitSha"]
        or str(body.get("path") or "").lstrip("/") != ref["path"]
    ):
        return None
    validated = dict(ref)
    validated["exists"] = body["exists"]
    validated["indexedAt"] = body.get("indexedAt") or validated.get("indexedAt")
    validated["validationMethod"] = "codegraph"
    validated["validatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return validated


def _validate_source_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """Validate one immutable repository source without trusting model prose."""
    codegraph_result = _validate_source_ref_with_codegraph(ref)
    if codegraph_result is not None:
        return codegraph_result
    repo = ref["repo"]
    path = urllib.parse.quote(ref["path"], safe="/")
    sha = urllib.parse.quote(ref["commitSha"], safe="")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{path}?ref={sha}",
        method="GET",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or os.getenv("GITHUB_MCP_TOKEN")
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    validated = dict(ref)
    validated["validationMethod"] = "github"
    validated["validatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with urllib.request.urlopen(request, timeout=6) as response:  # noqa: S310 - fixed GitHub API host
            body = json.loads(response.read().decode("utf-8"))
        validated["exists"] = isinstance(body, dict) and body.get("type") in {"file", "dir", "symlink", "submodule"}
    except urllib.error.HTTPError as error:
        validated["exists"] = False if error.code == 404 else None
        validated["validationError"] = "Path not found at commit." if error.code == 404 else "GitHub validation was unavailable."
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        validated["exists"] = None
        validated["validationError"] = "GitHub validation was unavailable."
    return validated


def prepare_report_record(
    report: dict[str, Any],
    *,
    validate_sources: bool = False,
) -> dict[str, Any]:
    """Return a stored report with conservative, backward-compatible metadata."""
    normalized = dict(report)
    answer = str(normalized.get("answer") or "")
    source_refs = normalize_source_refs(normalized.get("sourceRefs"), answer)
    if validate_sources:
        # GitHub path checks are independent. Keep report persistence bounded
        # when an answer cites several files instead of waiting six seconds per
        # source serially during a transient provider outage.
        worker_count = min(8, len(source_refs))
        if worker_count:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                source_refs = list(executor.map(_validate_source_ref, source_refs))
    validated = [ref for ref in source_refs if ref.get("exists") is True]

    if source_refs and len(validated) == len(source_refs):
        status = "verified"
        reason = "Every attached repository source was validated at its exact commit."
    elif validated:
        status = "partial"
        reason = "Some repository sources were validated, but others were missing or unavailable."
    elif source_refs:
        status = "partial"
        reason = "Exact source links are present but were not validated by a repository validator."
    else:
        status = "unverified"
        reason = "No validated exact repository source is attached."

    normalized["sourceRefs"] = source_refs
    normalized["unsupportedClaims"] = (
        normalized.get("unsupportedClaims")
        if isinstance(normalized.get("unsupportedClaims"), list)
        else []
    )
    for ref in source_refs:
        if ref.get("exists") is False:
            claim = f"Referenced path does not exist at the cited commit: {ref['repo']}/{ref['path']}"
            if claim not in normalized["unsupportedClaims"]:
                normalized["unsupportedClaims"].append(claim)
    if normalized["unsupportedClaims"] and status == "verified":
        status = "partial"
        reason = "Sources were validated, but the report still contains unsupported claims."
    normalized["verificationStatus"] = status
    normalized["verificationReason"] = reason
    normalized["repositories"] = sorted(
        {
            ref["repo"]
            for ref in source_refs
            if isinstance(ref.get("repo"), str) and ref.get("repo")
        }
    )
    indexed_times = [
        ref.get("indexedAt") or ref.get("validatedAt")
        for ref in source_refs
        if ref.get("indexedAt") or ref.get("validatedAt")
    ]
    normalized["sourceFreshness"] = max(indexed_times) if indexed_times else None
    return normalized


def _mutable_github_code_links(answer: str) -> list[str]:
    links: list[str] = []
    for match in _GITHUB_CODE_LINK.finditer(answer or ""):
        if match.group("kind") == "blob" and re.fullmatch(
            r"[0-9a-fA-F]{40}", match.group("ref")
        ):
            continue
        links.append(match.group(0))
    return links


def _requires_code_grounding(scope_metadata: dict[str, Any] | None) -> bool:
    if not isinstance(scope_metadata, dict):
        return False
    active = scope_metadata.get("active_sources")
    return isinstance(active, list) and "codebase" in active


def _blocked_delivery_answer(record: dict[str, Any]) -> str:
    reason = str(
        record.get("verificationReason")
        or "Exact repository evidence was unavailable."
    )
    return (
        "# I couldn't verify this answer yet\n\n"
        "I reached the research service, but it did not return enough exact, current "
        "repository evidence to support a trustworthy answer. I will not turn a plausible "
        "guess into a current fact.\n\n"
        f"**What failed:** {reason}\n\n"
        "**What to do:** Try the question again. If this repeats, the Code source needs "
        "attention before this assistant can answer implementation questions safely."
    )


def prepare_report_delivery(
    answer: str,
    scope_metadata: dict[str, Any] | None,
    *,
    source_refs: Any = None,
    validate_sources: bool = False,
) -> dict[str, Any]:
    """Prepare the exact answer that may reach the UI and report artifacts.

    Stored-report quarantine is not enough: a live code answer must fail closed
    before websocket delivery and before Markdown/PDF/DOCX generation.
    """
    record = prepare_report_record(
        {"answer": str(answer or ""), "sourceRefs": source_refs or []},
        validate_sources=validate_sources,
    )
    mutable_links = _mutable_github_code_links(str(answer or ""))
    for link in mutable_links:
        claim = (
            "Repository source is not an immutable 40-character commit permalink: "
            f"{link}"
        )
        if claim not in record["unsupportedClaims"]:
            record["unsupportedClaims"].append(claim)
    if mutable_links:
        record["verificationStatus"] = "partial"
        record["verificationReason"] = (
            "One or more repository links use a branch, tag, tree, or short "
            "commit instead of an exact file commit."
        )

    blocked = _requires_code_grounding(scope_metadata) and (
        record["verificationStatus"] != "verified" or bool(record["unsupportedClaims"])
    )
    record["deliveryBlocked"] = blocked
    if blocked:
        record["answer"] = _blocked_delivery_answer(record)
    return record


def report_is_memory_eligible(report: dict[str, Any]) -> bool:
    """Only validator-backed work may influence a future research answer."""
    return prepare_report_record(report).get("verificationStatus") == "verified"
