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

_GITHUB_PERMALINK_CANDIDATE = re.compile(
    r"https://github\.com/(?P<org>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"blob/(?P<sha>[0-9a-fA-F]{40})/[^\s<>]+"
)
_GITHUB_PERMALINK_EXACT = re.compile(
    r"^https://github\.com/(?P<org>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"blob/(?P<sha>[0-9a-fA-F]{40})/(?P<path>.+?)"
    r"(?:#L(?P<line>\d+)(?:-L(?P<end_line>\d+))?)?$"
)
_GITHUB_CODE_LINK = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(?P<kind>blob|tree)/(?P<ref>[^/\s)#?]+)/(?P<path>[^\s)#?]+)"
)
_SCOPE_INSTRUCTION_MARKER = "HLT research scope instructions:"
_REPORT_RECEIPT_KEYS = (
    "sourceRefs",
    "verificationStatus",
    "verificationReason",
    "unsupportedClaims",
    "deliveryBlocked",
    "hlt_research_scope",
)


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


def _report_complete_receipt(ordered_data: Any) -> dict[str, Any]:
    if not isinstance(ordered_data, list):
        return {}
    for item in reversed(ordered_data):
        if not isinstance(item, dict) or item.get("type") != "report_complete":
            continue
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            return {
                key: metadata[key]
                for key in _REPORT_RECEIPT_KEYS
                if key in metadata
            }
    return {}


def merge_report_delivery_receipt(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Keep backend validation evidence during the frontend's richer upsert.

    The browser adds ordered progress data after the backend has already saved
    the validated report. Replacing the record with only browser fields erased
    the source receipt and made a verified report look unverified in history.
    """

    merged = dict(incoming)
    merged.update(_report_complete_receipt(incoming.get("orderedData")))
    existing_has_backend_receipt = isinstance(existing, dict) and (
        bool(existing.get("sourceRefs"))
        or bool(existing.get("unsupportedClaims"))
        or (
            isinstance(existing.get("deliveryBlocked"), bool)
            and isinstance(existing.get("hlt_research_scope"), dict)
        )
    )
    if existing_has_backend_receipt:
        merged.update(
            {
                key: existing[key]
                for key in _REPORT_RECEIPT_KEYS
                if key in existing
            }
        )
    return merged


def extract_source_refs(answer: str) -> list[dict[str, Any]]:
    """Extract immutable GitHub evidence links from a report body."""
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int | None]] = set()
    for candidate_match in _GITHUB_PERMALINK_CANDIDATE.finditer(answer or ""):
        candidate = candidate_match.group(0).rstrip(".,;:!?\"'")
        # Markdown closes a link with `)`, but Next.js route-group paths also
        # contain balanced parentheses (`app/(site)/jobs/(board)/page.tsx`).
        # Remove only unmatched trailing closers so those real path segments
        # survive extraction and repository validation.
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        match = _GITHUB_PERMALINK_EXACT.fullmatch(candidate)
        if not match:
            continue
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
                "url": candidate,
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


def _source_ref_from_exact_url(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    match = _GITHUB_PERMALINK_EXACT.fullmatch(value.strip())
    if not match:
        return None
    return {
        "repo": f"{match.group('org')}/{match.group('repo')}",
        "commitSha": match.group("sha").lower(),
        "path": match.group("path"),
        "line": int(match.group("line")) if match.group("line") else None,
        "endLine": int(match.group("end_line")) if match.group("end_line") else None,
        "url": value.strip(),
        "exists": None,
    }


def source_refs_from_research_sources(
    sources: Any,
    *,
    max_refs: int = 12,
) -> list[dict[str, Any]]:
    """Recover exact refs from file-reading MCP results.

    Search and existence-check results are discovery, not claim evidence.
    Only read_source payloads are promoted into the delivery receipt.
    """

    candidates: list[dict[str, Any]] = []

    def visit(value: Any, *, trusted: bool = False, depth: int = 0) -> None:
        if depth > 8 or len(candidates) >= max_refs * 4:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, trusted=trusted, depth=depth + 1)
            return
        if isinstance(value, dict):
            tool_name = str(value.get("tool_name") or value.get("tool") or "")
            normalized_tool = tool_name.split("__")[-1].split(".")[-1].split("/")[-1]
            is_file_evidence = trusted or normalized_tool == "read_source"
            if is_file_evidence:
                direct = {
                    "repo": value.get("repo"),
                    "commitSha": value.get("commitSha") or value.get("commit_sha"),
                    "path": value.get("path"),
                    "line": (
                        value.get("line")
                        or value.get("startLine")
                        or value.get("start_line")
                    ),
                    "endLine": value.get("endLine") or value.get("end_line"),
                    "url": value.get("url") or value.get("href"),
                    "indexedAt": value.get("indexedAt") or value.get("indexed_at"),
                    "exists": value.get("exists"),
                }
                normalized = normalize_source_refs([direct])
                if normalized:
                    candidates.extend(normalized)
                exact_url = _source_ref_from_exact_url(
                    value.get("url") or value.get("href")
                )
                if exact_url:
                    candidates.append(exact_url)
            for key in (
                "structured_content",
                "results",
                "result",
                "content",
                "body",
                "text",
            ):
                if key in value:
                    visit(value[key], trusted=is_file_evidence, depth=depth + 1)
            return
        if not isinstance(value, str) or not trusted:
            return
        exact_url = _source_ref_from_exact_url(value)
        if exact_url:
            candidates.append(exact_url)
        candidate = value.strip()
        if candidate.startswith(("{", "[")):
            try:
                visit(json.loads(candidate), trusted=True, depth=depth + 1)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    visit(sources)
    return normalize_source_refs(candidates)[:max_refs]


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
    scope_metadata = normalized.get("hlt_research_scope")
    if _requires_code_grounding(scope_metadata):
        normalized["deliveryBlocked"] = (
            status != "verified" or bool(normalized["unsupportedClaims"])
        )
    elif "deliveryBlocked" in normalized:
        normalized["deliveryBlocked"] = bool(normalized["deliveryBlocked"])
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


def _code_report_authority_hazards(
    answer: str,
    source_refs: list[dict[str, Any]],
) -> list[str]:
    """Catch high-risk authority leaps before a code report is delivered.

    Path validation proves that a cited file exists, not that every conclusion
    drawn from it is valid. These checks cover the most damaging recurring
    errors while the writer's source-authority audit handles normal nuance.
    """

    text = str(answer or "")
    hazards: list[str] = []
    if re.search(
        r"\binterface\b[\s\S]{0,700}\b(?:indicat(?:e|es|ing)|therefore|so)\b"
        r"[\s\S]{0,350}\b(?:owns?|owned by|system of record|orchestrat(?:e|es|ed|ing))\b",
        text,
        flags=re.IGNORECASE,
    ):
        hazards.append(
            "A declared interface was used to infer ownership, persistence, or orchestration."
        )

    if re.search(
        r"\b(?:definitions?|implementation|write path|ownership|system of record)\b"
        r"[^.\n]{0,120}\b(?:likely|probably|presumably)\b"
        r"|\b(?:likely|probably|presumably)\b[^.\n]{0,100}"
        r"\b(?:file|path|route|schema|blueprint|repository)\b",
        text,
        flags=re.IGNORECASE,
    ):
        hazards.append(
            "A speculative file, schema, route, or ownership location was presented "
            "without opened source evidence."
        )

    marketo_absolute = re.search(
        r"\b(?:marketo (?:is not used|does not (?:store|send))|"
        r"emails? (?:are|is) not being sent(?: or leads added)? in marketo|"
        r"we do not (?:store|send)[^.\n]{0,100}\bmarketo)\b",
        text,
        flags=re.IGNORECASE,
    )
    if marketo_absolute:
        marketo_authority_paths = [
            str(ref.get("path") or "").lower()
            for ref in source_refs
            if any(
                marker in str(ref.get("path") or "").lower()
                for marker in ("marketo", "integrations", "providers", "destinations")
            )
            and not any(
                weak in str(ref.get("path") or "").lower()
                for weak in (
                    "/tests/",
                    "/test/",
                    "/scripts/",
                    "/docs/",
                    "/components/",
                    "/app/(dashboard)/",
                    "/page.tsx",
                )
            )
        ]
        if not marketo_authority_paths:
            hazards.append(
                "An estate-wide negative Marketo claim lacks an opened provider, integration, "
                "destination, or live-readback source."
            )

    # Repository code can establish that a Marketo operation is implemented,
    # but not that the integration is configured and executing right now. The
    # writer normally makes that distinction; fail closed when it instead emits
    # an unqualified operational statement from code evidence.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if "marketo" not in sentence.lower():
            continue
        positive_operation = _marketo_positive_operation(sentence)
        implementation_boundary = _has_implementation_boundary(sentence)
        if positive_operation and not implementation_boundary:
            hazards.append(
                "An unqualified live Marketo operation was inferred from implementation "
                "code without runtime readback."
            )
            break
    return hazards


def _marketo_positive_operation(sentence: str) -> bool:
    normalized = sentence.strip()
    if normalized.endswith("?") or re.search(
        r"\b(?:do|does|did|are|is|can|could|should|would)\s+we\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:"
            r"emails?\s+(?:are|is|were)\s+(?:being\s+)?(?:sent|stored)|"
            r"we\s+(?:currently\s+)?(?:send|store)\b|"
            r"marketo\s+(?:currently\s+)?(?:receives|stores|sends)\b"
            r")",
            sentence,
            flags=re.IGNORECASE,
        )
    )


def _has_implementation_boundary(sentence: str) -> bool:
    return bool(
        re.search(
            r"\b(?:code|implementation|implemented|function|method|code path|"
            r"capability|can|would|when invoked|if configured|not verified)\b",
            sentence,
            flags=re.IGNORECASE,
        )
    )


def _calibrate_code_runtime_claims(answer: str) -> str:
    """Turn an evidenced code path into a code claim, not a live-state claim."""

    sentence_pattern = re.compile(r"[^.!?\n]*\bmarketo\b[^.!?\n]*[.!?]?", re.IGNORECASE)

    def calibrate(match: re.Match[str]) -> str:
        sentence = match.group(0)
        if not _marketo_positive_operation(sentence) or _has_implementation_boundary(sentence):
            return sentence
        if re.search(r"lead[ -]?upsert", sentence, flags=re.IGNORECASE):
            return (
                "The opened code implements a Marketo lead-upsert path that uses email; "
                "this run did not include live Marketo readback proving that the path is "
                "configured or executing."
            )
        return (
            "The opened code implements a Marketo-related email path; this run did not "
            "include live Marketo readback proving that the path is configured or executing."
        )

    return sentence_pattern.sub(calibrate, str(answer or ""))


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
    delivery_answer = str(answer or "")
    if _requires_code_grounding(scope_metadata):
        delivery_answer = _calibrate_code_runtime_claims(delivery_answer)
    record = prepare_report_record(
        {"answer": delivery_answer, "sourceRefs": source_refs or []},
        validate_sources=validate_sources,
    )
    if _requires_code_grounding(scope_metadata) and not delivery_answer.strip():
        record["verificationStatus"] = "unverified"
        record["verificationReason"] = (
            "Repository evidence was gathered, but report writing returned no answer."
        )
    mutable_links = _mutable_github_code_links(delivery_answer)
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

    if _requires_code_grounding(scope_metadata):
        for claim in _code_report_authority_hazards(
            delivery_answer,
            record["sourceRefs"],
        ):
            if claim not in record["unsupportedClaims"]:
                record["unsupportedClaims"].append(claim)
        if record["unsupportedClaims"] and record["verificationStatus"] == "verified":
            record["verificationStatus"] = "partial"
            record["verificationReason"] = (
                "Repository paths were validated, but the report made an unsupported "
                "source-authority claim."
            )

    if (
        _requires_code_grounding(scope_metadata)
        and record["verificationStatus"] == "verified"
    ):
        missing_sources = [
            ref for ref in record["sourceRefs"]
            if str(ref.get("url") or "") not in record["answer"]
        ]
        if missing_sources:
            source_lines = [
                f"- [{ref['repo']} · {ref['path']}]({ref['url']})"
                for ref in missing_sources[:8]
            ]
            record["answer"] = (
                record["answer"].rstrip()
                + "\n\n## Sources\n\n"
                + "\n".join(source_lines)
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
