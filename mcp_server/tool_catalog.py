"""Names of tools on the hosted Mastery Research MCP.

Kept as data so Katailyst, the capability doc, and GET /api/hlt/capabilities
cannot drift from mcp_server/tools.py. Do not import hlt_extensions from here.
"""

from __future__ import annotations

RESEARCH_MCP_TOOLS: tuple[str, ...] = (
    "deep_research",
    "quick_search",
    "write_report",
    "get_research_sources",
    "get_research_context",
    "get_research_images",
)

PRODUCT_MCP_TOOLS: tuple[str, ...] = (
    "linear_in_flight",
    "linear_upcoming",
    "linear_board_health",
    "linear_issue",
    "linear_file_issue",
    "linear_update_issue",
)

MCP_URL = "https://gpt-researcher-mcp-production.up.railway.app/mcp"
API_URL = "https://gpt-researcher-api-production.up.railway.app"
UI_URL = "https://gpt-researcher-ui.vercel.app"


def research_capabilities() -> dict:
    """Machine-readable doors for Katailyst and other agents."""

    return {
        "service": "mastery-research",
        "bind": "mcp",
        "mcp": {
            "url": MCP_URL,
            "auth": "Authorization: Bearer $GPTR_MCP_TOKEN",
            "tools": list(RESEARCH_MCP_TOOLS),
            "product_tools": list(PRODUCT_MCP_TOOLS),
            "resource": "research://{topic}",
            "estate": True,
            "note": "deep_research and quick_search default scope=auto. That is the estate door.",
        },
        "doors": {
            "mcp": {"estate": True, "url": MCP_URL},
            "report": {
                "estate": True,
                "url": f"{API_URL}/report/",
                "auth": "X-API-Key",
            },
            "gather": {
                "estate": False,
                "url": f"{API_URL}/gather",
                "auth": "X-API-Key",
                "note": "Thin web findings adapter. Do not use for codegraph or Katailyst scopes.",
            },
            "quick_search": {
                "estate": False,
                "url": f"{API_URL}/api/quick_search",
                "auth": "X-API-Key",
                "note": "Web-only by design. Never bind estate research here.",
            },
            "ui": {"estate": True, "url": UI_URL},
        },
    }
