"""Deterministic source-policy, manifest, and report-acceptance contracts.

The research model may propose queries, summarize evidence, and write a report.
It does not decide whether the evidence contract passed.  This module keeps that
decision in ordinary Python so an answer cannot approve itself by printing
"PASS" in its own prose.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SOURCE_POLICY_VERSION = "source_policy.v1"
SOURCE_MANIFEST_VERSION = "source_manifest.v1"
REPORT_QUALITY_VERSION = "report_quality.v1"
MAX_REQUIRED_SOURCES = 32
MAX_SOURCE_DOMAINS = 64
MAX_SOURCE_ID_CHARS = 128
MAX_SOURCE_FAMILY_CHARS = 256
MAX_SOURCE_URL_CHARS = 2_048
MAX_SOURCE_DOMAIN_CHARS = 253
MAX_STRICT_REPORT_CHARS = 40_000

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)


class SourcePolicyError(ValueError):
    """Raised when a caller supplies an invalid source-policy contract."""


@dataclass(frozen=True)
class RequiredSource:
    id: str
    url: str
    family: str

    @classmethod
    def from_value(cls, value: Any, index: int) -> "RequiredSource":
        if isinstance(value, str):
            url = _bounded_text(
                value,
                field="required_sources.url",
                maximum=MAX_SOURCE_URL_CHARS,
            )
            generated = f"required-{index + 1}"
            return cls(id=generated, url=url, family=generated)
        if not isinstance(value, dict):
            raise SourcePolicyError("required_sources entries must be URLs or objects")
        source_id = _bounded_text(
            value.get("id") or f"required-{index + 1}",
            field="required_sources.id",
            maximum=MAX_SOURCE_ID_CHARS,
        )
        family = _bounded_text(
            value.get("family") or source_id,
            field="required_sources.family",
            maximum=MAX_SOURCE_FAMILY_CHARS,
        )
        url = _bounded_text(
            value.get("url"),
            field="required_sources.url",
            maximum=MAX_SOURCE_URL_CHARS,
        )
        return cls(id=source_id, url=url, family=family)


@dataclass(frozen=True)
class SourcePolicy:
    enforcement: Literal["advisory", "strict"] = "advisory"
    discovery_mode: Literal["open", "allowed_domains", "required_only"] = "open"
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    required_sources: tuple[RequiredSource, ...] = ()
    min_accepted_sources: int = 1
    min_content_chars: int = 100
    require_title: bool = True
    require_required_sources_cited: bool = True
    independent_judge_required: bool = True

    @property
    def is_strict(self) -> bool:
        return self.enforcement == "strict"

    @property
    def required_urls(self) -> list[str]:
        return [source.url for source in self.required_sources]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["version"] = SOURCE_POLICY_VERSION
        data["allowed_domains"] = list(self.allowed_domains)
        data["denied_domains"] = list(self.denied_domains)
        data["required_sources"] = [asdict(source) for source in self.required_sources]
        return data

    @classmethod
    def from_value(cls, value: Any | None) -> "SourcePolicy":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise SourcePolicyError("source_policy must be an object")
        version = value.get("version")
        if version is not None and version != SOURCE_POLICY_VERSION:
            raise SourcePolicyError(
                f"source_policy.version must be {SOURCE_POLICY_VERSION}"
            )

        enforcement = str(value.get("enforcement") or "advisory").lower()
        if enforcement not in {"advisory", "strict"}:
            raise SourcePolicyError("source_policy.enforcement must be advisory or strict")

        required_values = _bounded_sequence(
            value.get("required_sources"),
            field="required_sources",
            maximum=MAX_REQUIRED_SOURCES,
        )
        allowed_values = _bounded_sequence(
            value.get("allowed_domains"),
            field="allowed_domains",
            maximum=MAX_SOURCE_DOMAINS,
        )
        denied_values = _bounded_sequence(
            value.get("denied_domains"),
            field="denied_domains",
            maximum=MAX_SOURCE_DOMAINS,
        )
        required_sources = tuple(
            RequiredSource.from_value(item, index)
            for index, item in enumerate(required_values)
        )
        required_ids = [source.id for source in required_sources]
        if len(required_ids) != len(set(required_ids)):
            raise SourcePolicyError("source_policy.required_sources ids must be unique")
        required_canonical_urls = [
            canonicalize_url(source.url) for source in required_sources
        ]
        if any(not url for url in required_canonical_urls):
            raise SourcePolicyError("source_policy.required_sources must contain valid HTTP URLs")
        if len(required_canonical_urls) != len(set(required_canonical_urls)):
            raise SourcePolicyError(
                "source_policy.required_sources URLs must be unique after normalization"
            )
        allowed_domains = tuple(
            dict.fromkeys(
                domain
                for domain in (
                    _normalize_domain(
                        _bounded_text(
                            item,
                            field="allowed_domains",
                            maximum=MAX_SOURCE_DOMAIN_CHARS,
                        )
                    )
                    for item in allowed_values
                )
                if domain
            )
        )
        if required_sources and not allowed_domains:
            allowed_domains = tuple(
                dict.fromkeys(
                    domain
                    for domain in (url_domain(item.url) for item in required_sources)
                    if domain
                )
            )
            if any(len(domain) > MAX_SOURCE_DOMAIN_CHARS for domain in allowed_domains):
                raise SourcePolicyError(
                    "source_policy.required_sources contain an oversized domain"
                )
        denied_domains = tuple(
            dict.fromkeys(
                domain
                for domain in (
                    _normalize_domain(
                        _bounded_text(
                            item,
                            field="denied_domains",
                            maximum=MAX_SOURCE_DOMAIN_CHARS,
                        )
                    )
                    for item in denied_values
                )
                if domain
            )
        )

        default_mode = (
            "required_only"
            if required_sources
            else "allowed_domains"
            if allowed_domains
            else "open"
        )
        discovery_mode = str(value.get("discovery_mode") or default_mode).lower()
        if discovery_mode not in {"open", "allowed_domains", "required_only"}:
            raise SourcePolicyError(
                "source_policy.discovery_mode must be open, allowed_domains, or required_only"
            )
        if discovery_mode == "required_only" and not required_sources:
            raise SourcePolicyError("required_only source policy needs required_sources")
        if discovery_mode == "allowed_domains" and not allowed_domains:
            raise SourcePolicyError("allowed_domains source policy needs allowed_domains")

        min_accepted_sources = int(value.get("min_accepted_sources", 1))
        if min_accepted_sources < 1 or min_accepted_sources > 1_000:
            raise SourcePolicyError(
                "source_policy.min_accepted_sources must be between 1 and 1000"
            )
        if (
            discovery_mode == "required_only"
            and min_accepted_sources > len(required_sources)
        ):
            raise SourcePolicyError(
                "source_policy.min_accepted_sources cannot exceed required_sources "
                "in required_only mode"
            )
        min_content_chars = int(value.get("min_content_chars", 100))
        if min_content_chars < 1 or min_content_chars > 100_000:
            raise SourcePolicyError("source_policy.min_content_chars must be between 1 and 100000")
        require_title = bool(value.get("require_title", True))
        require_required_sources_cited = bool(
            value.get("require_required_sources_cited", True)
        )
        independent_judge_required = bool(
            value.get("independent_judge_required", True)
        )
        if enforcement == "strict" and (
            min_content_chars < 100
            or not require_title
            or not require_required_sources_cited
            or not independent_judge_required
        ):
            raise SourcePolicyError(
                "strict source policies require titled content of at least 100 "
                "characters, required-source citations, and an independent judge"
            )

        policy = cls(
            enforcement=enforcement,
            discovery_mode=discovery_mode,
            allowed_domains=allowed_domains,
            denied_domains=denied_domains,
            required_sources=required_sources,
            min_accepted_sources=min_accepted_sources,
            min_content_chars=min_content_chars,
            require_title=require_title,
            require_required_sources_cited=require_required_sources_cited,
            independent_judge_required=independent_judge_required,
        )
        for source in policy.required_sources:
            canonical = canonicalize_url(source.url)
            if not canonical:
                raise SourcePolicyError(f"required source {source.id!r} has an invalid URL")
            allowed, reason = source_url_allowed(policy, source.url)
            if not allowed:
                raise SourcePolicyError(
                    f"required source {source.id!r} violates its source policy: {reason}"
                )
        return policy


def _bounded_sequence(value: Any, *, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SourcePolicyError(f"source_policy.{field} must be an array")
    if len(value) > maximum:
        raise SourcePolicyError(
            f"source_policy.{field} must contain at most {maximum} entries"
        )
    return list(value)


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SourcePolicyError(f"source_policy.{field} must be a string")
    text = value.strip()
    if not text:
        raise SourcePolicyError(f"source_policy.{field} must be non-empty")
    if len(text) > maximum:
        raise SourcePolicyError(
            f"source_policy.{field} must contain at most {maximum} characters"
        )
    return text


def _normalize_domain(value: Any) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    if "://" in domain:
        domain = url_domain(domain)
    return domain[4:] if domain.startswith("www.") else domain


def url_domain(url: str) -> str:
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def canonicalize_url(url: str) -> str:
    """Canonicalize an HTTP URL for deterministic deduplication and comparison."""

    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    return urlunsplit((scheme, host, path, query, ""))


def _domain_matches(host: str, candidate: str) -> bool:
    return host == candidate or host.endswith(f".{candidate}")


def public_source_url_allowed(
    url: str,
    *,
    resolve_dns: bool = False,
    resolver: Any = socket.getaddrinfo,
) -> tuple[bool, str | None]:
    """Reject URLs that could reach local/private infrastructure."""

    canonical = canonicalize_url(url)
    if not canonical:
        return False, "invalid_url"
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return False, "invalid_url"
    if parsed.username is not None or parsed.password is not None:
        return False, "url_credentials_not_allowed"
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        host == "localhost"
        or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))
        or host in {"metadata.google.internal", "metadata.google"}
    ):
        return False, "non_public_host"
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        return False, "non_public_ip"
    if not resolve_dns or literal is not None:
        return True, None
    try:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        addresses = resolver(
            host, parsed.port or default_port, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError):
        return False, "dns_resolution_failed"
    resolved_ips = set()
    for address in addresses:
        try:
            resolved_ips.add(ipaddress.ip_address(address[4][0].split("%", 1)[0]))
        except (IndexError, ValueError):
            return False, "invalid_dns_result"
    if not resolved_ips:
        return False, "dns_resolution_failed"
    if any(not address.is_global for address in resolved_ips):
        return False, "dns_resolved_non_public_ip"
    return True, None


def require_public_source_url(url: str, *, resolve_dns: bool = True) -> None:
    allowed, reason = public_source_url_allowed(url, resolve_dns=resolve_dns)
    if not allowed:
        raise SourcePolicyError(f"source URL is not public: {reason}")


def require_policy_source_url(
    policy: SourcePolicy, url: str, *, resolve_dns: bool = True
) -> None:
    allowed, reason = source_url_allowed(policy, url)
    if not allowed:
        raise SourcePolicyError(f"source URL violates policy: {reason}")
    if policy.is_strict:
        require_public_source_url(url, resolve_dns=resolve_dns)


def source_url_allowed(policy: SourcePolicy, url: str) -> tuple[bool, str | None]:
    canonical = canonicalize_url(url)
    if not canonical:
        return False, "invalid_url"
    if policy.is_strict:
        public, public_reason = public_source_url_allowed(url)
        if not public:
            return False, public_reason
    host = url_domain(canonical)
    if any(_domain_matches(host, domain) for domain in policy.denied_domains):
        return False, "denied_domain"
    if policy.allowed_domains and not any(
        _domain_matches(host, domain) for domain in policy.allowed_domains
    ):
        return False, "outside_allowed_domains"
    if policy.discovery_mode == "required_only":
        required = {canonicalize_url(item.url) for item in policy.required_sources}
        if canonical not in required:
            return False, "outside_required_sources"
    return True, None


def source_content(source: dict[str, Any]) -> str:
    candidates = [
        str(source.get(key) or "") for key in ("raw_content", "content", "body")
    ]
    return max(candidates, key=len, default="")


def merge_source_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep one source row per canonical URL, preferring richer metadata."""

    merged = dict(existing)
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        current = merged.get(key)
        if key == "title":
            current_title = str(current or "").strip()
            incoming_title = str(value or "").strip()
            current_valid = bool(current_title) and current_title.lower() != "unknown"
            incoming_valid = bool(incoming_title) and incoming_title.lower() != "unknown"
            if incoming_valid and (
                not current_valid or len(incoming_title) > len(current_title)
            ):
                merged[key] = incoming_title
            continue
        if current in (None, "", [], {}) or (
            key in {"raw_content", "content", "body"}
            and len(str(value)) > len(str(current or ""))
        ):
            merged[key] = value
    if incoming.get("url"):
        merged["url"] = canonicalize_url(str(incoming["url"])) or str(incoming["url"])
    return merged


def _source_manifest_entry(
    source: dict[str, Any],
    policy: SourcePolicy,
    required_by_url: dict[str, list[RequiredSource]],
) -> tuple[dict[str, Any], list[str]]:
    raw_url = str(source.get("url") or source.get("href") or "")
    canonical = canonicalize_url(raw_url)
    title = str(source.get("title") or "").strip()[:500]
    content = source_content(source)
    reasons: list[str] = []
    allowed, policy_reason = source_url_allowed(policy, raw_url)
    if not allowed and policy_reason:
        reasons.append(policy_reason)
    if policy.require_title and (not title or title.lower() == "unknown"):
        reasons.append("missing_title")
    if len(content.strip()) < policy.min_content_chars:
        reasons.append("content_too_short")
    required = required_by_url.get(canonical, [])
    entry = {
        "url": raw_url,
        "canonical_url": canonical,
        "domain": url_domain(canonical),
        "title": title,
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        if content
        else None,
        "required_ids": [item.id for item in required],
        "families": list(dict.fromkeys(item.family for item in required)),
        "status": "accepted" if not reasons else "rejected",
        "reasons": reasons,
    }
    return entry, reasons


def build_source_manifest(
    policy_value: SourcePolicy | dict[str, Any] | None,
    sources: list[dict[str, Any]],
    *,
    blocked_candidates: list[dict[str, Any]] | None = None,
    images: list[Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized, deduplicated manifest and deterministic gate result."""

    policy = SourcePolicy.from_value(policy_value)
    required_by_url: dict[str, list[RequiredSource]] = {}
    for required in policy.required_sources:
        required_by_url.setdefault(canonicalize_url(required.url), []).append(required)

    merged_by_url: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    duplicate_count = 0
    for value in sources or []:
        source = value if isinstance(value, dict) else {"title": str(value)}
        canonical = canonicalize_url(str(source.get("url") or source.get("href") or ""))
        if not canonical:
            anonymous.append(source)
            continue
        if canonical in merged_by_url:
            duplicate_count += 1
            merged_by_url[canonical] = merge_source_records(merged_by_url[canonical], source)
        else:
            merged_by_url[canonical] = merge_source_records({}, source)

    entries: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for source in [*merged_by_url.values(), *anonymous]:
        entry, reasons = _source_manifest_entry(source, policy, required_by_url)
        entries.append(entry)
        if reasons:
            blockers.append(
                {
                    "code": "source_rejected",
                    "url": entry["url"],
                    "reasons": reasons,
                }
            )

    accepted_by_url = {
        entry["canonical_url"]: entry
        for entry in entries
        if entry["status"] == "accepted" and entry["canonical_url"]
    }
    if len(accepted_by_url) < policy.min_accepted_sources:
        blockers.append(
            {
                "code": "insufficient_accepted_sources",
                "accepted": len(accepted_by_url),
                "required": policy.min_accepted_sources,
            }
        )
    required_checks = []
    for required in policy.required_sources:
        canonical = canonicalize_url(required.url)
        present = canonical in accepted_by_url
        required_checks.append(
            {
                "id": required.id,
                "family": required.family,
                "url": required.url,
                "canonical_url": canonical,
                "status": "present" if present else "missing",
            }
        )
        if not present:
            blockers.append(
                {
                    "code": "required_source_missing",
                    "source_id": required.id,
                    "family": required.family,
                    "url": required.url,
                }
            )

    image_entries: list[dict[str, Any]] = []
    try:
        from gpt_researcher.scraper.utils import is_likely_content_image
    except Exception:  # pragma: no cover - import is stable in the runtime
        is_likely_content_image = lambda _url, _alt="": False
    for value in images or []:
        image = value if isinstance(value, dict) else {"url": str(value)}
        image_url = str(image.get("url") or "")
        source_url = canonicalize_url(str(image.get("source_url") or ""))
        generated = str(image.get("kind") or "").lower() == "generated"
        reasons: list[str] = []
        if generated:
            reasons.extend(_generated_image_rejection_reasons(image))
        else:
            if not source_url:
                reasons.append("missing_source_attribution")
            content_image = is_likely_content_image(
                image_url, str(image.get("alt_text") or "")
            )
            if not content_image:
                reasons.append("non_content_or_tracking_asset")
            else:
                image_public, _image_public_reason = public_source_url_allowed(
                    image_url, resolve_dns=policy.is_strict
                )
                if not image_public:
                    reasons.append("non_public_image_url")
            if source_url and source_url not in accepted_by_url:
                reasons.append("source_not_admitted")
        image_entry = {
            "url": image_url,
            "source_url": str(image.get("source_url") or ""),
            "kind": "generated" if generated else "source",
            "status": "accepted" if not reasons else "rejected",
            "reasons": reasons,
        }
        image_entries.append(image_entry)
        if reasons:
            blockers.append(
                {"code": "image_rejected", "url": image_url, "reasons": reasons}
            )

    strict_blockers = blockers if policy.is_strict else []
    return {
        "version": SOURCE_MANIFEST_VERSION,
        "policy": policy.to_dict(),
        "status": "failed" if strict_blockers else "passed",
        "accepted_sources": [entry for entry in entries if entry["status"] == "accepted"],
        "rejected_sources": [entry for entry in entries if entry["status"] == "rejected"],
        "blocked_candidates": blocked_candidates or [],
        "required_sources": required_checks,
        "images": image_entries,
        "duplicate_count": duplicate_count,
        "blockers": strict_blockers,
    }


def _generated_image_rejection_reasons(image: dict[str, Any]) -> list[str]:
    """Validate built-in generated files without applying web tracker rules."""

    url = str(image.get("url") or "")
    parsed = urlsplit(url)
    path = PurePosixPath(parsed.path)
    reasons: list[str] = []
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/outputs/images/")
        or ".." in path.parts
    ):
        reasons.append("invalid_generated_image_url")
    file_value = str(image.get("path") or image.get("absolute_url") or "")
    try:
        file_path = Path(file_value).resolve(strict=True) if file_value else None
    except (OSError, RuntimeError):
        file_path = None
    if file_path is None:
        reasons.append("generated_image_file_missing")
    if file_path is not None:
        expected_suffix = parsed.path.lstrip("/")
        if not file_path.is_file() or not file_path.as_posix().endswith(expected_suffix):
            reasons.append("generated_image_path_mismatch")
    return list(dict.fromkeys(reasons))


def extract_report_urls(report: str) -> list[str]:
    urls = []
    seen = set()
    for match in _URL_RE.findall(report or ""):
        canonical = canonicalize_url(match.rstrip(".,;:!?)"))
        if canonical and canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)
    return urls


def build_report_quality(
    policy_value: SourcePolicy | dict[str, Any] | None,
    manifest: dict[str, Any],
    report: str,
    independent_judgment: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine deterministic citation checks with a separate judge result."""

    policy = SourcePolicy.from_value(policy_value)
    accepted = {
        entry.get("canonical_url"): entry
        for entry in manifest.get("accepted_sources") or []
        if entry.get("canonical_url")
    }
    cited_urls = extract_report_urls(report)
    unadmitted = [url for url in cited_urls if url not in accepted]
    missing_required_citations = []
    if policy.require_required_sources_cited:
        for required in policy.required_sources:
            canonical = canonicalize_url(required.url)
            if canonical not in cited_urls:
                missing_required_citations.append(
                    {
                        "id": required.id,
                        "family": required.family,
                        "url": required.url,
                        "canonical_url": canonical,
                    }
                )

    if not policy.is_strict:
        return {
            "version": REPORT_QUALITY_VERSION,
            "status": "not_applicable",
            "publishable": True,
            "cited_urls": cited_urls,
            "unadmitted_citations": [],
            "missing_required_citations": [],
            "independent_judgment": {"verdict": "not_required", "findings": []},
            "findings": [],
            "note": "Strict source acceptance was not requested for this run.",
        }

    findings: list[dict[str, Any]] = []
    if len(report) > MAX_STRICT_REPORT_CHARS:
        findings.append(
            {
                "code": "strict_report_too_long",
                "severity": "high",
                "characters": len(report),
                "maximum": MAX_STRICT_REPORT_CHARS,
            }
        )
    if manifest.get("status") != "passed":
        findings.append({"code": "source_manifest_failed", "severity": "high"})
    if unadmitted:
        findings.append(
            {
                "code": "unadmitted_report_citation",
                "severity": "high",
                "urls": unadmitted,
            }
        )
    if missing_required_citations:
        findings.append(
            {
                "code": "required_source_not_cited",
                "severity": "high",
                "sources": missing_required_citations,
            }
        )

    judge = independent_judgment or {
        "verdict": "not_run",
        "findings": [{"code": "independent_judge_not_run", "severity": "high"}],
    }
    judge_verdict = str(judge.get("verdict") or "").lower()
    judge_blocking_findings = [
        finding
        for finding in (judge.get("findings") or [])
        if isinstance(finding, dict)
        and str(finding.get("severity") or "").lower() in {"high", "critical"}
    ]
    if policy.independent_judge_required and judge_verdict != "pass":
        findings.append(
            {
                "code": "independent_judge_failed",
                "severity": "high",
                "judge_verdict": judge_verdict or "invalid",
            }
        )
    elif policy.independent_judge_required and judge_blocking_findings:
        findings.append(
            {
                "code": "independent_judge_blocking_findings",
                "severity": "high",
                "findings": judge_blocking_findings,
            }
        )

    passed = not findings if policy.is_strict else True
    return {
        "version": REPORT_QUALITY_VERSION,
        "status": "passed" if passed else "failed",
        "publishable": passed,
        "cited_urls": cited_urls,
        "unadmitted_citations": unadmitted,
        "missing_required_citations": missing_required_citations,
        "independent_judgment": judge,
        "findings": findings,
        "note": "Report prose, including any PASS label, is not an acceptance signal.",
    }
