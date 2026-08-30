"""Versioned contracts and identity primitives for research automations.

This module deliberately contains no durable-store or execution behavior.  It
keeps the HTTP request surface, contract descriptor, admission configuration,
and deterministic identities independently importable while
``mcp_server.automation_research`` remains the compatibility facade.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gpt_researcher.source_policy import (
    SourcePolicy,
    SourcePolicyError,
    canonicalize_url,
)
from mcp_server.tools import SourcePolicyInput

REQUEST_SCHEMA_VERSION = "research_automation_request.v1"
RESULT_SCHEMA_VERSION = "research_automation_result.v1"
OPERATION_SCHEMA_VERSION = "research_automation_operation.v1"
STATUS_SCHEMA_VERSION = "research_automation_status.v1"
RESULT_READ_SCHEMA_VERSION = "research_automation_result_read.v1"
PROVENANCE_SCHEMA_VERSION = "research_automation_provenance.v1"
K2_HANDOFF_SCHEMA_VERSION = "k2_knowledge_refine_preview_handoff.v1"
CONTRACT_DESCRIPTOR_SCHEMA_VERSION = "mastery_research_http_contract.v1"
CONTRACT_MANIFEST_SCHEMA_VERSION = "mastery_research_contract_manifest.v1"
DURABLE_RECEIPT_SCHEMA_VERSION = "automation_research_receipt.v2"
LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION = (
    "automation_research_receipt.legacy_pre_lineage.v1"
)
ASYNC_JOB_BASE_PATH = "/automation/research/jobs/v1"
K2_INLINE_MAX_CHARS = 128_000
K2_INLINE_MAX_BYTES = 128_000
ASYNC_CONTRACT_DESCRIPTOR: dict[str, Any] = {
    "schemaVersion": CONTRACT_DESCRIPTOR_SCHEMA_VERSION,
    "provider": "mastery-research",
    "capabilityRef": "tool:mastery-research",
    "authentication": {"type": "bearer"},
    "operations": [
        {
            "name": "mastery_research_start",
            "kind": "effect",
            "idempotency": "provider_keyed",
            "method": "POST",
            "path": f"{ASYNC_JOB_BASE_PATH}/start",
            "requestSchema": REQUEST_SCHEMA_VERSION,
            "responseSchema": OPERATION_SCHEMA_VERSION,
        },
        {
            "name": "mastery_research_status",
            "kind": "read",
            "idempotency": "read_only",
            "method": "GET",
            "path": f"{ASYNC_JOB_BASE_PATH}/{{request_id}}/status",
            "responseSchema": STATUS_SCHEMA_VERSION,
        },
        {
            "name": "mastery_research_result",
            "kind": "read",
            "idempotency": "read_only",
            "method": "GET",
            "path": f"{ASYNC_JOB_BASE_PATH}/{{request_id}}/result",
            "responseSchema": RESULT_READ_SCHEMA_VERSION,
        },
    ],
    "terminalHandoff": {
        "tool": "knowledge.refine.preview",
        "receiptSchema": "upstream_execution_receipt.v1",
        "inlineContentMaxChars": K2_INLINE_MAX_CHARS,
        "inlineContentMaxBytes": K2_INLINE_MAX_BYTES,
    },
}
_ASYNC_CONTRACT_CANONICAL_JSON = json.dumps(
    ASYNC_CONTRACT_DESCRIPTOR,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
ASYNC_CONTRACT_FINGERPRINT = "sha256:" + hashlib.sha256(
    _ASYNC_CONTRACT_CANONICAL_JSON.encode("utf-8")
).hexdigest()
ASYNC_CONTRACT_MANIFEST: dict[str, Any] = {
    "schemaVersion": CONTRACT_MANIFEST_SCHEMA_VERSION,
    "canonicalization": "json.sort_keys.compact_utf8.v1",
    "contractFingerprint": ASYNC_CONTRACT_FINGERPRINT,
    "descriptor": ASYNC_CONTRACT_DESCRIPTOR,
}
_RESEARCH_ID_NAMESPACE = uuid.UUID("a18f98c2-9a6f-49e8-a654-d826588b7a55")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


class StrictSourcePolicyInput(SourcePolicyInput):
    """The strict-only source contract accepted by the automation route."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["source_policy.v1"]
    enforcement: Literal["strict"]

    @model_validator(mode="after")
    def validate_source_policy_semantics(self) -> "StrictSourcePolicyInput":
        try:
            SourcePolicy.from_value(self.model_dump(exclude_none=True))
        except (SourcePolicyError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return self


class AutomationResearchRequest(BaseModel):
    """Versioned request shared by blocking and nonblocking research routes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["research_automation_request.v1"]
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    query: str = Field(min_length=1, max_length=12_000)
    report_prompt: str = Field(min_length=1, max_length=20_000)
    report_type: Literal["research_report"] = "research_report"
    report_source: Literal["web"] = "web"
    tone: Literal["Objective"] = "Objective"
    scope: Literal["none"] = "none"
    depth: Literal["fast", "balanced", "deep"] = "balanced"
    max_sources_per_query: int | None = Field(default=None, ge=3, le=20)
    include_generated_images: bool = False
    source_policy: StrictSourcePolicyInput

    @field_validator("request_id", "query", "report_prompt", mode="before")
    @classmethod
    def normalize_text_fields(cls, value: Any) -> Any:
        return _normalized_text(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class AutomationHTTPResult:
    """Transport response returned by the adapter executor."""

    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AutomationCapacity:
    """Generous, configurable process-independent admission limits."""

    max_concurrent: int
    max_queued: int


@dataclass(frozen=True)
class AutomationDeadlines:
    """Attempt deadlines persisted with the durable operation receipt."""

    overall_seconds: int
    deep_research_seconds: int
    report_seconds: int


class AutomationAdmissionSaturated(RuntimeError):
    """Both the durable running allowance and bounded queue are full."""


class AutomationDeadlineExceeded(TimeoutError):
    """One typed automation phase exceeded its persisted deadline."""

    def __init__(self, phase: str, timeout_seconds: float):
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{phase} exceeded {timeout_seconds:g} seconds")


class AutomationReceiptReadError(RuntimeError):
    """A pure receipt read could not safely inspect the SQLite ledger."""

    def __init__(self, code: str, message: str, status_code: int):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _bounded_env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _automation_capacity() -> AutomationCapacity:
    return AutomationCapacity(
        max_concurrent=_bounded_env_int(
            "AUTOMATION_RESEARCH_MAX_CONCURRENT",
            default=8,
            minimum=1,
            maximum=128,
        ),
        max_queued=_bounded_env_int(
            "AUTOMATION_RESEARCH_MAX_QUEUED",
            default=256,
            minimum=0,
            maximum=10_000,
        ),
    )


def _automation_deadlines() -> AutomationDeadlines:
    overall = _bounded_env_int(
        "AUTOMATION_RESEARCH_OVERALL_TIMEOUT_SECONDS",
        default=7_200,
        minimum=1,
        maximum=86_400,
    )
    deep = _bounded_env_int(
        "AUTOMATION_RESEARCH_DEEP_TIMEOUT_SECONDS",
        default=5_400,
        minimum=1,
        maximum=86_400,
    )
    report = _bounded_env_int(
        "AUTOMATION_RESEARCH_REPORT_TIMEOUT_SECONDS",
        default=1_800,
        minimum=1,
        maximum=86_400,
    )
    return AutomationDeadlines(
        overall_seconds=overall,
        deep_research_seconds=min(deep, overall),
        report_seconds=min(report, overall),
    )


def _queue_recovery_poll_seconds() -> float:
    return float(
        _bounded_env_int(
            "AUTOMATION_RESEARCH_QUEUE_RECOVERY_POLL_SECONDS",
            default=30,
            minimum=1,
            maximum=60,
        )
    )


def _blocking_admission_poll_seconds() -> float:
    return float(
        _bounded_env_int(
            "AUTOMATION_RESEARCH_BLOCKING_ADMISSION_POLL_SECONDS",
            default=2,
            minimum=1,
            maximum=30,
        )
    )


def _deadline_iso(started_at: str, timeout_seconds: int) -> str:
    started = datetime.fromisoformat(started_at)
    return (started + timedelta(seconds=timeout_seconds)).isoformat()


def canonical_request(request: AutomationResearchRequest) -> dict[str, Any]:
    """Return normalized semantic fields used for request identity.

    ``request_id`` is the caller's idempotency key, not part of the semantic
    payload. URL/domain collections are normalized so harmless ordering and
    tracking-parameter differences do not create new work.
    """

    policy = SourcePolicy.from_value(
        request.source_policy.model_dump(exclude_none=True)
    ).to_dict()
    policy["allowed_domains"] = sorted(set(policy["allowed_domains"]))
    policy["denied_domains"] = sorted(set(policy["denied_domains"]))
    policy["required_sources"] = sorted(
        (
            {
                "id": source["id"],
                "family": source["family"],
                "url": canonicalize_url(source["url"]),
            }
            for source in policy["required_sources"]
        ),
        key=lambda source: (source["id"], source["family"], source["url"]),
    )
    return {
        "schema_version": request.schema_version,
        "query": request.query,
        "report_prompt": request.report_prompt,
        "report_type": request.report_type,
        "report_source": request.report_source,
        "tone": request.tone,
        "scope": request.scope,
        "depth": request.depth,
        "max_sources_per_query": request.max_sources_per_query,
        "include_generated_images": request.include_generated_images,
        "source_policy": policy,
    }


def request_fingerprint(request: AutomationResearchRequest) -> str:
    """SHA-256 of the canonical effective request, excluding request_id."""

    encoded = json.dumps(
        canonical_request(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_research_id(request_id: str, fingerprint: str) -> str:
    """Derive a stable UUID for one caller idempotency key and payload."""

    return str(
        uuid.uuid5(
            _RESEARCH_ID_NAMESPACE,
            f"{REQUEST_SCHEMA_VERSION}\n{request_id}\n{fingerprint}",
        )
    )


def attempt_research_id(
    research_id: str,
    lease_generation: int,
    lease_token: str,
) -> str:
    """Return a core-run identity fenced to one execution attempt.

    Every attempt, including generation one, uses its persisted lease token so
    an orphaned core row cannot collide with a newly recreated adapter receipt.
    """

    return str(
        uuid.uuid5(
            _RESEARCH_ID_NAMESPACE,
            (
                f"{REQUEST_SCHEMA_VERSION}\n{research_id}\n"
                f"attempt:{lease_generation}\nlease:{lease_token}"
            ),
        )
    )


__all__ = [
    "ASYNC_CONTRACT_DESCRIPTOR",
    "ASYNC_CONTRACT_FINGERPRINT",
    "ASYNC_CONTRACT_MANIFEST",
    "ASYNC_JOB_BASE_PATH",
    "AutomationAdmissionSaturated",
    "AutomationCapacity",
    "AutomationDeadlineExceeded",
    "AutomationDeadlines",
    "AutomationHTTPResult",
    "AutomationReceiptReadError",
    "AutomationResearchRequest",
    "CONTRACT_DESCRIPTOR_SCHEMA_VERSION",
    "CONTRACT_MANIFEST_SCHEMA_VERSION",
    "DURABLE_RECEIPT_SCHEMA_VERSION",
    "K2_HANDOFF_SCHEMA_VERSION",
    "K2_INLINE_MAX_BYTES",
    "K2_INLINE_MAX_CHARS",
    "LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION",
    "OPERATION_SCHEMA_VERSION",
    "PROVENANCE_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "RESULT_READ_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "STATUS_SCHEMA_VERSION",
    "StrictSourcePolicyInput",
    "_ASYNC_CONTRACT_CANONICAL_JSON",
    "_RESEARCH_ID_NAMESPACE",
    "_REQUEST_ID_PATTERN",
    "_automation_capacity",
    "_automation_deadlines",
    "_blocking_admission_poll_seconds",
    "_bounded_env_int",
    "_deadline_iso",
    "_normalized_text",
    "_queue_recovery_poll_seconds",
    "attempt_research_id",
    "canonical_request",
    "deterministic_research_id",
    "request_fingerprint",
]
