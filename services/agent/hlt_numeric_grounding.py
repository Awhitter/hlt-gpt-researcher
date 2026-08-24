"""Fail-closed grounding for factual numeric business claims.

Hermes' ``/v1/runs`` surface is used by K2 for durable agent work.  The model's
reasoning and inherited conversation are useful context, but they are not
evidence for a factual count, rate, dollar value, or percentage.  This module
keeps a deliberately small evidence ledger from the current user message and
successful tool results from the current run, then reconciles the final answer
before the API is allowed to emit ``run.completed``.

The validator is intentionally deterministic.  It does not ask another model
to judge a model, and it never rewrites an answer silently.  Unsupported facts
produce a failed run; explicitly labelled future targets/scenarios and
arithmetic with grounded operands remain available.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_EVIDENCE_CHARS = 2_000_000
MAX_EVIDENCE_FACTS = 50_000

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<currency>[$£€])?"
    r"(?P<sign>[+-])?"
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<scale>[kKmMbB])?"
    r"(?P<percent>%)?"
    r"(?![A-Za-z0-9_])"
)

_METRIC_RE = re.compile(
    r"(?ix)\b(?:"
    r"metric|funnel|count|total|volume|rate|ratio|percent|percentage|"
    r"conversion|converted|retention|churn|growth|drop[- ]?off|"
    r"revenue|income|sales|cost|spend|budget|dollars?|usd|arr|mrr|"
    r"users?|people|persons?|nurses?|candidates?|applicants?|leads?|"
    r"jobs?|applications?|applies|applied|submissions?|submitted|received|"
    r"search(?:es|ed)?|views?|detail(?:s| views?)?|clicks?|sessions?|visitors?|"
    r"impressions?|opens?|responses?|hires?|placements?|outcomes?|handoffs?|"
    r"average|median|mean|p50|p90|p95|p99|latency|duration"
    r")\b"
)

_FUTURE_LABEL_RE = re.compile(
    r"(?ix)\b(?:"
    r"target|goal|scenario|forecast|projection|hypothetical|proposed|planned|"
    r"future\s+target"
    r")\b"
)

_NONFACT_LABEL_RE = re.compile(
    r"(?ix)\b(?:"
    r"unsupported\s+(?:claim|draft|value|metric)|unverified\s+(?:claim|value|metric)|"
    r"not\s+observed|not\s+grounded|unknown\s+(?:value|metric)|"
    r"fabricated\s+(?:claim|draft|value|metric)|incorrect\s+draft|previous\s+draft"
    r")\b"
)

_DATE_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_VERSION_RE = re.compile(r"(?i)(?<!\w)v\d+(?:\.\d+)*\b")
_URL_RE = re.compile(r"https?://\S+")
_ARITHMETIC_RE = re.compile(
    r"[+\-−*/×÷=]|\b(?:of|from|difference|sum)\b", re.IGNORECASE
)

_GENERIC_LABEL_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "compare",
        "count",
        "counts",
        "current",
        "for",
        "from",
        "in",
        "is",
        "metric",
        "number",
        "of",
        "on",
        "people",
        "persons",
        "rate",
        "the",
        "to",
        "total",
        "users",
        "value",
        "was",
        "were",
    }
)
_SEMANTIC_LABEL_KEYS = frozenset(
    {"event", "key", "label", "metric", "metric_name", "name", "step", "title"}
)
_LABEL_ALIASES = {
    "searched": "search",
    "searches": "search",
    "views": "view",
    "viewed": "view",
    "details": "detail",
    "clicks": "click",
    "clicked": "click",
    "applications": "application",
    "applied": "apply",
    "applies": "apply",
    "applicants": "application",
    "submissions": "received",
    "submitted": "received",
    "submission": "received",
    "receipts": "received",
    "responses": "response",
    "hires": "hire",
    "placements": "placement",
    "spent": "spend",
    "spending": "spend",
}
_UNLABELLED = "__unlabelled__"


@dataclass(frozen=True)
class _Number:
    raw: str
    value: Decimal
    kind: str
    places: int
    start: int
    end: int


@dataclass(frozen=True)
class UnsupportedClaim:
    """A bounded, safe description of one rejected numeric claim."""

    value: str
    line: int


@dataclass(frozen=True)
class GroundingVerdict:
    ok: bool
    checked_claims: int
    grounded_claims: int
    derived_claims: int
    future_claims: int
    labelled_nonfacts: int
    unsupported: tuple[UnsupportedClaim, ...]
    successful_tool_results: int
    evidence_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": "hlt.current_run_numeric_grounding.v1",
            "status": "passed" if self.ok else "failed",
            "checked_claims": self.checked_claims,
            "grounded_claims": self.grounded_claims,
            "derived_claims": self.derived_claims,
            "future_claims": self.future_claims,
            "labelled_nonfacts": self.labelled_nonfacts,
            "unsupported": [
                {"value": claim.value, "line": claim.line}
                for claim in self.unsupported
            ],
            "successful_tool_results": self.successful_tool_results,
            "evidence_truncated": self.evidence_truncated,
        }

    def failure_message(self) -> str:
        values = ", ".join(claim.value for claim in self.unsupported[:8])
        suffix = "" if len(self.unsupported) <= 8 else ", …"
        return (
            "Numeric grounding check failed before delivery: factual business "
            f"metrics were not found in the current request or successful current-run "
            f"tool results ({values}{suffix}). Re-run after reconciling the draft "
            "against the exact source values, or label future targets explicitly."
        )


def _decimal_places(number_text: str) -> int:
    return len(number_text.rsplit(".", 1)[1]) if "." in number_text else 0


def _number_kind(match: re.Match[str], line: str) -> str:
    before = line[max(0, match.start() - 40) : match.start()]
    after = line[match.end() : match.end() + 24]
    if match.group("percent") or re.search(
        r"(?ix)(?:percent(?:age)?|conversion[_ -]?rate|"
        r"click[_ -]?through[_ -]?rate|ctr|cvr)\s*[:=]\s*$",
        before,
    ) or re.match(r"(?ix)^\s*(?:percent|percentage)\b", after):
        return "percent"
    if match.group("currency") or re.search(
        r"(?ix)(?:usd|dollars?|revenue|income|sales|cost|spend|budget|arr|mrr)\s*[:=]\s*$",
        before,
    ) or re.match(r"(?ix)^\s*(?:usd|dollars?)\b", after):
        return "currency"
    return "plain"


def _parse_number(match: re.Match[str], line: str) -> _Number | None:
    number_text = match.group("number").replace(",", "")
    try:
        value = Decimal(number_text)
    except InvalidOperation:
        return None
    if match.group("sign") == "-":
        value = -value
    scale = (match.group("scale") or "").lower()
    if scale:
        value *= {"k": Decimal(1_000), "m": Decimal(1_000_000), "b": Decimal(1_000_000_000)}[scale]
    return _Number(
        raw=match.group(0),
        value=value,
        kind=_number_kind(match, line),
        places=_decimal_places(number_text),
        start=match.start(),
        end=match.end(),
    )


def _masked_line(line: str) -> str:
    """Remove common technical numeric forms that are not business claims."""
    def spaces(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    # Preserve offsets so each extracted number can be associated with the
    # label immediately around it after technical forms are removed.
    masked = _URL_RE.sub(spaces, line)
    masked = _DATE_RE.sub(spaces, masked)
    masked = _TIME_RE.sub(spaces, masked)
    masked = _VERSION_RE.sub(spaces, masked)
    # Markdown list ordinals describe structure, not a metric.
    masked = re.sub(r"^\s*\d+[.)](?=\s)", spaces, masked)
    return masked


def _numbers_in_line(line: str) -> list[_Number]:
    masked = _masked_line(line)
    numbers: list[_Number] = []
    for match in _NUMBER_RE.finditer(masked):
        parsed = _parse_number(match, masked)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def _canonical_label_tokens(value: Any) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", text.replace("_", "-").replace("-", " "))
    tokens: set[str] = set()
    for raw in raw_tokens:
        token = raw.lower()
        token = _LABEL_ALIASES.get(token, token)
        if token in _GENERIC_LABEL_TOKENS or len(token) < 2:
            continue
        tokens.add(token)

    # Canonical product-funnel aliases keep equivalent source/final labels
    # together without making an unrelated metric with the same value pass.
    if "universal" in tokens and "apply" in tokens and "started" in tokens:
        tokens.add("apply")
    if "nurse" in tokens and "confirmed" in tokens and "submit" in tokens:
        tokens.add("received")
    if "job" in tokens and "view" in tokens:
        tokens.add("detail")
    if {"profile", "milestone", "reached"} <= tokens:
        # Nursing Mastery's maintained funnel uses the source event name for
        # the human-facing "apply start" stage.
        tokens.add("apply")
    return tokens


def _claim_labels(line: str, number: _Number) -> set[str]:
    """Return the nearest human/JSON/table label for one numeric claim."""
    prefix = line[: number.start]
    suffix = line[number.end :]

    if "|" in line:
        cells = [cell.strip() for cell in prefix.split("|") if cell.strip()]
        local_before = cells[-1] if cells else ""
    else:
        boundary = max(
            prefix.rfind(marker)
            for marker in ("→", ";", ",", "•")
        )
        local_before = prefix[boundary + 1 :]

    next_boundaries = [
        position
        for marker in ("→", "|", ";", ",", "•")
        if (position := suffix.find(marker)) >= 0
    ]
    local_after = suffix[: min(next_boundaries)] if next_boundaries else suffix[:80]
    return _canonical_label_tokens(f"{local_before[-120:]} {local_after[:80]}")


def _label_applies_before_claim(
    line: str,
    number: _Number,
    pattern: re.Pattern[str],
) -> bool:
    """Require an explicit label in the same clause before this exact claim."""
    prefix = line[: number.start]
    if "|" in prefix:
        cells = [cell.strip() for cell in prefix.split("|") if cell.strip()]
        if cells and pattern.search(cells[-1]):
            return True
    clause_start = max(
        prefix.rfind(marker)
        for marker in (".", "!", "?", ",", ";", "|")
    )
    clause = prefix[clause_start + 1 :]
    return bool(pattern.search(clause))


def _structured_context_labels(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    labels: set[str] = set()
    for key, nested in value.items():
        if str(key).lower() in _SEMANTIC_LABEL_KEYS and isinstance(nested, str):
            labels.update(_canonical_label_tokens(nested))
    return labels


def _fact_key(number: _Number) -> tuple[str, Decimal]:
    return number.kind, number.value.normalize()


def _is_grounded(
    number: _Number,
    line: str,
    grounded: dict[tuple[str, Decimal], set[str]],
) -> bool:
    evidence_labels = grounded.get(_fact_key(number))
    if not evidence_labels:
        return False
    claim_labels = _claim_labels(line, number)
    if not claim_labels or _UNLABELLED in evidence_labels:
        return True
    return bool(claim_labels & evidence_labels)


def _close_enough(candidate: Decimal, expected: Decimal, places: int) -> bool:
    # A displayed value rounded to N decimal places may differ by at most half
    # a unit in the last displayed place.
    tolerance = Decimal("0.5") * (Decimal(10) ** -places)
    return abs(candidate - expected) <= tolerance


def _is_derived(
    claim: _Number,
    line_numbers: Iterable[_Number],
    grounded: dict[tuple[str, Decimal], set[str]],
    line: str,
) -> bool:
    if not _ARITHMETIC_RE.search(line):
        return False

    operands = [
        number
        for number in line_numbers
        if _is_grounded(number, line, grounded) and number is not claim
    ]
    if len(operands) < 2:
        return False

    for left in operands:
        for right in operands:
            if left is right:
                continue
            a, b = left.value, right.value
            candidates: list[Decimal] = []
            if "+" in line or re.search(r"\bsum\b", line, re.IGNORECASE):
                candidates.append(a + b)
            if "-" in line or "−" in line or re.search(
                r"\b(?:from|difference)\b", line, re.IGNORECASE
            ):
                candidates.append(a - b)
                candidates.append(abs(a - b))
            if "*" in line or "×" in line:
                candidates.append(a * b)
            if b != 0 and (
                "/" in line
                or "÷" in line
                or re.search(r"\bof\b", line, re.IGNORECASE)
            ):
                ratio = a / b
                candidates.append(ratio)
                if claim.kind == "percent":
                    candidates.append(ratio * Decimal(100))
            if claim.kind == "percent" and b != 0 and re.search(
                r"\b(?:change|growth|increase|decrease|drop)\b",
                line,
                re.IGNORECASE,
            ):
                candidates.append(((a - b) / b) * Decimal(100))

            if any(_close_enough(claim.value, candidate, claim.places) for candidate in candidates):
                return True
    return False


class NumericGroundingLedger:
    """Current-run numeric evidence plus deterministic final-output validation."""

    def __init__(self, user_input: Any) -> None:
        self._lock = threading.RLock()
        self._facts: dict[tuple[str, Decimal], set[str]] = {}
        self._evidence_chars = 0
        self._truncated = False
        self._successful_tool_results = 0
        self._add_evidence(user_input)

    def _record_fact(self, number: _Number, labels: set[str]) -> None:
        key = _fact_key(number)
        if key not in self._facts and len(self._facts) >= MAX_EVIDENCE_FACTS:
            self._truncated = True
            return
        self._facts.setdefault(key, set()).update(labels or {_UNLABELLED})

    def _add_text_evidence(
        self,
        text: str,
        inherited_labels: set[str],
        *,
        depth: int = 0,
    ) -> None:
        table_headers: list[str] | None = None
        for line in text.splitlines() or [text]:
            stripped = line.strip()

            # Hosted MCP results are sometimes wrapped as untrusted text with a
            # complete JSON result object on its own line. Decode that object so
            # escaped newlines become real rows before extracting evidence.
            if stripped[:1] in {"{", "["}:
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    self._walk_structured(
                        parsed,
                        inherited_labels,
                        depth=depth + 1,
                    )
                    table_headers = None
                    continue

            cells = [cell.strip() for cell in line.split("|")]
            if line.lstrip().startswith("|"):
                cells = cells[1:]
            if line.rstrip().endswith("|"):
                cells = cells[:-1]

            if len(cells) >= 2:
                is_separator = all(
                    not cell or bool(re.fullmatch(r":?-{3,}:?", cell))
                    for cell in cells
                )
                if is_separator:
                    continue

                cell_numbers = [_numbers_in_line(cell) for cell in cells]
                if not any(cell_numbers):
                    table_headers = cells
                    continue

                if table_headers is not None and len(table_headers) == len(cells):
                    row_labels = _canonical_label_tokens(cells[0])
                    for index, numbers in enumerate(cell_numbers):
                        for number in numbers:
                            labels = set(inherited_labels)
                            labels.update(row_labels)
                            labels.update(_canonical_label_tokens(table_headers[index]))
                            labels.update(_claim_labels(cells[index], number))
                            self._record_fact(number, labels)
                    continue
            else:
                table_headers = None

            for number in _numbers_in_line(line):
                labels = set(inherited_labels)
                labels.update(_claim_labels(line, number))
                self._record_fact(number, labels)

    @staticmethod
    def _scalar_number(value: float | Decimal, labels: set[str]) -> _Number | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        raw = str(value)
        try:
            decimal_value = Decimal(raw)
        except InvalidOperation:
            return None
        kind = "plain"
        if labels & {"percent", "percentage", "conversion", "ctr", "cvr"}:
            kind = "percent"
        elif labels & {
            "arr",
            "budget",
            "cost",
            "dollar",
            "income",
            "mrr",
            "revenue",
            "sale",
            "spend",
            "usd",
        }:
            kind = "currency"
        return _Number(
            raw=raw,
            value=decimal_value,
            kind=kind,
            places=_decimal_places(raw),
            start=0,
            end=len(raw),
        )

    def _walk_structured(
        self,
        value: Any,
        inherited_labels: set[str] | None = None,
        *,
        depth: int = 0,
    ) -> None:
        if depth > 12:
            self._truncated = True
            return
        labels = set(inherited_labels or ())
        if isinstance(value, dict):
            labels.update(_structured_context_labels(value))
            for key, nested in value.items():
                child_labels = labels | _canonical_label_tokens(key)
                self._walk_structured(nested, child_labels, depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                self._walk_structured(nested, labels, depth=depth + 1)
            return
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            number = self._scalar_number(value, labels)
            if number is not None:
                self._record_fact(number, labels)
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in {"{", "["}:
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    self._walk_structured(parsed, labels, depth=depth + 1)
                    return
            self._add_text_evidence(value, labels, depth=depth)

    def _add_evidence(self, value: Any) -> None:
        if isinstance(value, str):
            serialized = value
        else:
            try:
                serialized = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError, OverflowError):
                serialized = str(value)

        with self._lock:
            remaining = MAX_EVIDENCE_CHARS - self._evidence_chars
            if remaining <= 0:
                self._truncated = True
                return
            self._evidence_chars += min(len(serialized), remaining)
            if len(serialized) > remaining:
                self._truncated = True
                self._add_text_evidence(serialized[:remaining], set())
                return
            self._walk_structured(value)

    def observe_tool_result(
        self,
        tool_name: str | None,
        result: Any,
        *,
        is_error: bool = False,
    ) -> None:
        del tool_name  # Names are routing labels; successful result content is evidence.
        if is_error:
            return
        with self._lock:
            self._successful_tool_results += 1
        self._add_evidence(result)

    def observe_tool_event(
        self,
        event_type: str,
        tool_name: str | None = None,
        *,
        result: Any = None,
        is_error: bool = False,
    ) -> None:
        if event_type == "tool.completed":
            self.observe_tool_result(tool_name, result, is_error=is_error)

    def validate(self, final_response: Any) -> GroundingVerdict:
        text = str(final_response or "")
        with self._lock:
            grounded = {key: set(labels) for key, labels in self._facts.items()}
            tool_results = self._successful_tool_results
            truncated = self._truncated

        checked = 0
        grounded_count = 0
        derived_count = 0
        future_count = 0
        labelled_nonfacts = 0
        unsupported: list[UnsupportedClaim] = []
        metric_section = False
        future_section = False
        nonfact_section = False
        metric_table = False
        in_code_fence = False

        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if stripped.startswith(("```", "~~~")):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                continue
            if not stripped:
                metric_table = False
                continue

            if stripped.startswith("#"):
                metric_section = bool(_METRIC_RE.search(stripped))
                future_section = bool(_FUTURE_LABEL_RE.search(stripped))
                nonfact_section = bool(_NONFACT_LABEL_RE.search(stripped))

            is_table = stripped.startswith("|") and stripped.endswith("|")
            if is_table and _METRIC_RE.search(stripped):
                metric_table = True

            line_numbers = _numbers_in_line(raw_line)
            if not line_numbers:
                continue

            explicit_metric = any(number.kind in {"percent", "currency"} for number in line_numbers)
            is_metric_line = (
                bool(_METRIC_RE.search(raw_line))
                or explicit_metric
                or metric_section
                or metric_table
            )
            if not is_metric_line:
                continue

            for claim in line_numbers:
                if future_section or _label_applies_before_claim(
                    raw_line, claim, _FUTURE_LABEL_RE
                ):
                    future_count += 1
                    continue
                if nonfact_section or _label_applies_before_claim(
                    raw_line, claim, _NONFACT_LABEL_RE
                ):
                    labelled_nonfacts += 1
                    continue
                checked += 1
                if _is_grounded(claim, raw_line, grounded):
                    grounded_count += 1
                    continue
                if _is_derived(claim, line_numbers, grounded, raw_line):
                    derived_count += 1
                    continue
                unsupported.append(
                    UnsupportedClaim(value=claim.raw[:32], line=line_number)
                )

        return GroundingVerdict(
            ok=not unsupported,
            checked_claims=checked,
            grounded_claims=grounded_count,
            derived_claims=derived_count,
            future_claims=future_count,
            labelled_nonfacts=labelled_nonfacts,
            unsupported=tuple(unsupported),
            successful_tool_results=tool_results,
            evidence_truncated=truncated,
        )
