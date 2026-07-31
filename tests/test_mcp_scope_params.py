"""Unit tests for MCP tool scope/depth helpers."""
from __future__ import annotations

from mcp_server.tools import _build_research_scope, _scope_summary


def test_build_research_scope_defaults_to_auto():
    assert _build_research_scope(None, "balanced") == {"auto": True, "depth": "balanced"}
    assert _build_research_scope("auto", "fast") == {"auto": True, "depth": "fast"}


def test_build_research_scope_none_forces_web_only():
    assert _build_research_scope("none", "deep") == {"depth": "deep"}


def test_build_research_scope_pins_valid_keys():
    scope = _build_research_scope(["codebase", "cms", "nope"], "balanced")
    assert scope == {"codebase": True, "cms": True, "depth": "balanced"}


def test_build_research_scope_accepts_single_string_key():
    scope = _build_research_scope("metrics", "bogus")
    assert scope == {"metrics": True, "depth": "balanced"}


def test_scope_summary_exposes_auto_fields():
    summary = _scope_summary(
        {
            "active_sources": ["codebase"],
            "degraded_sources": [],
            "mcp_server_count": 2,
            "depth": "fast",
            "auto_scope": {
                "requested": True,
                "applied": ["codebase"],
                "reasons": {"codebase": ["mentions ScraperVault"]},
            },
        }
    )
    assert summary == {
        "active_sources": ["codebase"],
        "degraded_sources": [],
        "auto": {
            "requested": True,
            "applied": ["codebase"],
            "reasons": {"codebase": ["mentions ScraperVault"]},
        },
        "mcp_server_count": 2,
        "depth": "fast",
    }
