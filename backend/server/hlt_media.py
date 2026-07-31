"""Cloudinary media search for the HLT `media` research scope.

Credentials stay server-side; callers only ever receive safe asset metadata
(public id, folder, dimensions, secure URL, tags).
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any
import urllib.error
import urllib.request

from .hlt_text import task_terms

logger = logging.getLogger(__name__)

_CLOUDINARY_RESOURCE_TYPES = ("image", "video", "raw")
_CLOUDINARY_MAX_ASSETS = 8


def _cloudinary_readiness() -> dict[str, Any]:
    missing = [
        key
        for key in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
        if not os.getenv(key)
    ]
    return {
        "status": "ready" if not missing else "unavailable",
        "configured": not missing,
        "missing": missing,
        "cloud_configured": bool(os.getenv("CLOUDINARY_CLOUD_NAME")),
    }


def _cloudinary_request(
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: int = 8,
) -> dict[str, Any]:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if not cloud_name or not api_key or not api_secret:
        raise RuntimeError("Cloudinary credentials are not configured.")

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method)
    basic_token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    request.add_header("Authorization", f"Basic {basic_token}")
    if payload is not None:
        request.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Cloudinary host
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _cloudinary_list_assets() -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    resources: list[dict[str, Any]] = []

    try:
        body = _cloudinary_request(
            method="POST",
            path="/resources/search",
            body={
                "expression": "resource_type:image OR resource_type:video OR resource_type:raw",
                "max_results": 60,
                "with_field": ["context", "tags", "metadata"],
            },
        )
        raw_resources = body.get("resources")
        if isinstance(raw_resources, list):
            resources.extend(item for item in raw_resources if isinstance(item, dict))
    except Exception as error:  # pragma: no cover - exercised by integration smoke
        warnings.append(
            "Cloudinary search API unavailable; used resource-list fallback. "
            f"({type(error).__name__})"
        )

    if resources:
        return resources, warnings

    for resource_type in _CLOUDINARY_RESOURCE_TYPES:
        try:
            body = _cloudinary_request(
                method="GET",
                path=f"/resources/{resource_type}/upload?max_results=25",
            )
            raw_resources = body.get("resources")
            if isinstance(raw_resources, list):
                resources.extend(item for item in raw_resources if isinstance(item, dict))
        except urllib.error.HTTPError as error:
            warnings.append(f"Cloudinary {resource_type} assets unavailable: HTTP {error.code}.")
        except Exception as error:  # pragma: no cover - network/runtime dependent
            warnings.append(f"Cloudinary {resource_type} assets unavailable: {type(error).__name__}.")

    return resources, warnings


def _stringify_asset_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_stringify_asset_field(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key} {_stringify_asset_field(item)}" for key, item in value.items())
    return str(value)


def _score_cloudinary_asset(asset: dict[str, Any], terms: list[str]) -> int:
    if not terms:
        return 1
    haystack = " ".join(
        _stringify_asset_field(asset.get(key))
        for key in (
            "public_id",
            "asset_folder",
            "folder",
            "filename",
            "tags",
            "context",
            "metadata",
        )
    ).lower()
    return sum(1 for term in terms if term in haystack)


def _summarize_cloudinary_asset(asset: dict[str, Any]) -> dict[str, Any]:
    tags = asset.get("tags")
    safe_tags = [tag for tag in tags if isinstance(tag, str)][:8] if isinstance(tags, list) else []
    return {
        "public_id": asset.get("public_id"),
        "resource_type": asset.get("resource_type"),
        "format": asset.get("format"),
        "asset_folder": asset.get("asset_folder") or asset.get("folder"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "secure_url": asset.get("secure_url") or asset.get("url"),
        "tags": safe_tags,
    }


def search_cloudinary_assets(task: str) -> dict[str, Any]:
    """Search Cloudinary with server-side credentials and return safe asset metadata."""

    readiness = _cloudinary_readiness()
    if readiness["status"] != "ready":
        return {
            "status": "unavailable",
            "assets": [],
            "warnings": ["Cloudinary media search was requested but credentials are not configured."],
        }

    terms = task_terms(task)
    try:
        resources, warnings = _cloudinary_list_assets()
    except Exception as error:  # pragma: no cover - defensive boundary
        return {
            "status": "degraded",
            "assets": [],
            "warnings": [f"Cloudinary media search failed: {type(error).__name__}."],
        }

    ranked = [
        (score, index, asset)
        for index, asset in enumerate(resources)
        for score in [_score_cloudinary_asset(asset, terms)]
        if score > 0 or not terms
    ]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    assets = [_summarize_cloudinary_asset(asset) for _, _, asset in ranked[:_CLOUDINARY_MAX_ASSETS]]
    return {
        "status": "ready" if assets else "empty",
        "terms": terms,
        "assets": assets,
        "warnings": warnings,
    }
